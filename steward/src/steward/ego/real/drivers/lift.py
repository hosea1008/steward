"""850Pro Modbus RTU driver extracted from the validated NAVIS lift controller.

No ROS dependency. Home is explicit; opening reads no motion commands. The caller
owns cancellation and must not share the serial port with another controller.
"""

import fcntl
import glob
import math
import os
import select
import struct
import termios
import threading
import time


class LiftError(RuntimeError):
    """串口通信、电机故障、限位或运动中断。"""

    def __init__(self, message, *, feedback=None):
        super().__init__(message)
        self.feedback = feedback


def _crc(data):
    value = 0xFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xA001 if value & 1 else 0)
    return struct.pack('<H', value)


def _positive(value, name):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(name + ' 必须为有限正数')
    return value


class LiftController:
    """直接通过 Modbus RTU 操作 850Pro；构造函数只连接和读取，不运动。"""

    MIN_HEIGHT = 0
    MAX_HEIGHT = 750
    PULSES_PER_MM = 2000
    MOTOR_RPM = 400

    def __init__(self, port, motor_id=1, serial_timeout=0.3, cancelled=lambda: False):
        self._external_cancelled = cancelled
        if type(motor_id) is not int or not 1 <= motor_id <= 247:
            raise ValueError('motor_id 必须在 1–247 之间')
        self.motor_id = motor_id
        self.serial_timeout = _positive(serial_timeout, 'serial_timeout')
        self._io_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._cancel = threading.Event()
        self._fd = None
        self._exclusive = False
        candidates = [port] if port else sorted(glob.glob('/dev/ttyUSB*'))
        errors = []
        for candidate in candidates:
            try:
                self._open(candidate)
                if port is None:
                    self.status()
                self.port = candidate
                return
            except (OSError, LiftError) as exc:
                errors.append('{}: {}'.format(candidate, exc))
                self._disconnect()
        raise LiftError('无法连接升降柱；请检查串口并停止原控制节点。' + '; '.join(errors))

    def _open(self, port):
        self._fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.ioctl(self._fd, termios.TIOCEXCL)
        self._exclusive = True
        config = termios.tcgetattr(self._fd)
        config[0] = config[1] = config[3] = 0
        config[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        config[4] = config[5] = termios.B38400
        config[6][termios.VMIN] = config[6][termios.VTIME] = 0
        termios.tcsetattr(self._fd, termios.TCSANOW, config)

    def _disconnect(self):
        if self._fd is not None:
            try:
                if self._exclusive:
                    fcntl.ioctl(self._fd, termios.TIOCNXCL)
            finally:
                os.close(self._fd)
                self._fd = None
                self._exclusive = False

    def _exchange(self, payload):
        with self._io_lock:
            if self._fd is None:
                raise LiftError('串口已关闭')
            # 38400 baud 的 RTU 帧间隔；清掉上一帧残余字节。
            time.sleep(0.004)
            termios.tcflush(self._fd, termios.TCIFLUSH)
            request = bytes([self.motor_id]) + payload
            frame = request + _crc(request)
            deadline = time.monotonic() + self.serial_timeout
            sent = 0
            while sent < len(frame):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [self._fd], [], remaining)[1]:
                    raise LiftError('串口写入超时')
                sent += os.write(self._fd, frame[sent:])
            reply = bytearray()
            expected = 3
            while len(reply) < expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self._fd], [], [], remaining)[0]:
                    raise LiftError('串口响应超时')
                chunk = os.read(self._fd, expected - len(reply))
                if not chunk:
                    raise LiftError('串口连接断开')
                reply.extend(chunk)
                if len(reply) >= 3:
                    expected = (5 if reply[1] & 0x80 else
                                5 + reply[2] if reply[1] == 3 else 8)
                    if expected > 255:
                        raise LiftError('无效的串口响应长度')
            if reply[:1] != request[:1] or _crc(reply[:-2]) != reply[-2:]:
                raise LiftError('串口响应地址或 CRC 校验错误')
            if reply[1] == (payload[0] | 0x80):
                raise LiftError('Modbus 异常码 {}'.format(reply[2]))
            if reply[1] != payload[0]:
                raise LiftError('串口响应功能码错误')
            if payload[0] == 3:
                if reply[2] != struct.unpack('>H', payload[3:5])[0] * 2:
                    raise LiftError('串口响应数据长度错误')
                return bytes(reply[3:-2])
            if reply[:-2] != request[:6]:
                raise LiftError('寄存器写入应答不匹配')
            return b''

    def _read(self, register, count):
        return self._exchange(struct.pack('>BHH', 3, register, count))

    def _write(self, register, value):
        self._exchange(struct.pack('>BHH', 6, register, value & 0xFFFF))

    def status(self):
        """读取高度、转速、电流、故障位及限位；不会启动电机。"""
        with self._io_lock:
            electrical = self._read(0xE1, 2)
            motion = self._read(0xE3, 2)
            position = struct.unpack('>i', self._read(0xE8, 2))[0]
            limits = self._read(0x54, 1)
        return dict(height=position / self.PULSES_PER_MM,
                    speed_rpm=round(struct.unpack('>h', motion[2:])[0] * 3000 / 8192),
                    current=struct.unpack('>H', electrical[2:])[0] / 100,
                    fault_bits=motion[1] & 0x7E,
                    down_limit=not bool(limits[0]), up_limit=not bool(limits[1]))

    def _check(self, state):
        if self._cancel.is_set() or self._external_cancelled():
            raise LiftError('运动已停止')
        if state['fault_bits'] or state['current'] > 17:
            raise LiftError('电机故障或过流：{}'.format(state))
        if state['down_limit'] and state['up_limit']:
            raise LiftError('上下限位同时触发，请检查限位接线')

    def _command(self, callback):
        # 与 stop() 共用锁，避免停止后又下发一条运动命令。
        with self._io_lock:
            if self._cancel.is_set() or self._external_cancelled():
                raise LiftError('运动已停止')
            callback()

    def _configure(self):
        self._command(self._halt)
        for register, value in ((0x00, 1), (0x51, 0), (0x1C, 7),
                                (0x09, 0x0A0A),
                                (0x1D, round(self.MOTOR_RPM * 8192 / 3000))):
            self._command(lambda r=register, v=value: self._write(r, v))

    def _wait_stopped(self, deadline, *, height=None, check_cancel=True):
        """Require two zero-speed samples after halt; never resend a movement command.

        The deadline is bounded by the operation timeout. A stop-only confirmation
        ignores cancellation because cancellation is what requested the halt.
        """
        consecutive = 0
        state = None
        while time.monotonic() < deadline:
            state = self.status()
            if check_cancel:
                self._check(state)
            if time.monotonic() >= deadline:
                break
            consecutive = consecutive + 1 if state['speed_rpm'] == 0 else 0
            if consecutive >= 2:
                if height is not None and abs(state['height'] - height) >= 2:
                    raise LiftError('停稳后高度未达到目标容差', feedback=state)
                return state
            time.sleep(.02)
        raise LiftError('等待停稳反馈超时', feedback=state)

    def _run(self, homing, height, timeout):
        _positive(timeout, 'timeout')
        if not self._operation_lock.acquire(blocking=False):
            raise LiftError('已有运动正在执行；可调用 stop() 中断')
        try:
            self._cancel.clear()
            deadline = time.monotonic() + timeout
            state = self.status()
            self._check(state)
            self._configure()
            # 先把速度设为零，再切换到速度模式，避免沿用旧速度。
            if homing:
                self._command(lambda: self._write(0x06, 0))
                self._command(lambda: self._write(0x02, 0xC4))
            else:
                self._command(lambda: self._write(0x02, 0xD0))
            while True:
                state = self.status()
                self._check(state)
                if time.monotonic() >= deadline:
                    raise TimeoutError('升降柱运动超时')
                if homing and state['down_limit']:
                    self._halt()
                    self._wait_stopped(deadline)
                    for _ in range(3):
                        self._command(lambda: self._write(0x4C, 0))
                    return self._wait_stopped(deadline, height=0)
                if not homing and abs(state['height'] - height) < 2:
                    self._halt()
                    return self._wait_stopped(deadline, height=height)
                direction = -1 if homing or height < state['height'] else 1
                if (not homing and direction < 0 and state['down_limit']) or (
                        direction > 0 and state['up_limit']):
                    raise LiftError('目标尚未到达，但运动方向限位已触发')
                if homing:
                    self._command(lambda: self._write(
                        0x06, round(-self.MOTOR_RPM * 8192 / 3000)))
                else:
                    data = struct.pack('>BHHBi', 0x10, 0x50, 2, 4,
                                       height * self.PULSES_PER_MM)
                    self._command(lambda: self._exchange(data))
                self._cancel.wait(0.02)
        except BaseException as exc:
            try:
                self.stop()
            except (OSError, LiftError) as stop_exc:
                raise LiftError('运动失败：{}；停止反馈也失败：{}'.format(exc, stop_exc),
                                feedback=getattr(stop_exc, 'feedback', None) or getattr(exc, 'feedback', None)) from exc
            raise
        finally:
            self._operation_lock.release()

    def reset(self, timeout=60.0):
        """下行至下限位并清零，返回状态；超时或故障不会将当前位置当作零点。"""
        return self._run(True, 0, timeout)

    def set_height(self, height, timeout=60.0):
        """运行至指定绝对高度（整数 mm，0–750）；上电后须先完成 reset。"""
        if type(height) is not int or not self.MIN_HEIGHT <= height <= self.MAX_HEIGHT:
            raise ValueError('height 必须为 0–750 mm 的整数')
        return self._run(False, height, timeout)

    def _halt(self):
        # 即使速度写入失败，仍尝试发送急停寄存器。
        try:
            self._write(0x06, 0)
        finally:
            self._write(0x4D, 0)

    def stop(self, timeout=2.0):
        """发送停止后，限时等待连续零转速反馈；失败保留最近反馈，不假定停稳。"""
        _positive(timeout, 'timeout')
        self._cancel.set()
        with self._io_lock:
            self._halt()
            return self._wait_stopped(time.monotonic() + timeout, check_cancel=False)

    def close(self):
        """停止电机并关闭串口；不清除电机零点。"""
        self._cancel.set()
        with self._io_lock:
            if self._fd is not None:
                try:
                    self._halt()
                finally:
                    self._disconnect()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
