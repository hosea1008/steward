"""Console-owned motion admission and serialized execution for one selected robot.

The loopback API uses a private connection token. A turn must be active; disconnect,
turn exit, peer disconnect and deadlines cancel pending work. Failed operations
latch a fault until operator inspection and an explicit software-fault clearance.
"""
from __future__ import annotations

import math
import secrets
import threading
import time

from steward.net import CancellableHandler, HttpError, JsonServer
from .drivers.navigation import Navigation, NavigationError, LINEAR_SPEED_M_S, ANGULAR_SPEED_RAD_S
from .drivers.lift import LiftController, LiftError


def validate_arguments(tool, args):
    """Validate model JSON before opening any hardware transport; never accept speed overrides."""
    if not isinstance(args, dict):
        raise HttpError(400, '工具参数必须为对象')
    fields = ({'x', 'y', 'heading_deg', 'map_name', 'timeout_s', 'position_tolerance_m', 'heading_tolerance_deg'}
              if tool == 'navigate_to_pose' else {'action', 'height_mm', 'timeout_s'})
    if tool not in ('navigate_to_pose', 'lift_move') or args.keys() - fields:
        raise HttpError(400, '未知工具或参数；速度不可覆盖')
    def number(key, low, high):
        value = args.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise HttpError(400, f'{key} 必须是 {low} 到 {high} 之间的有限数值')
    number('timeout_s', .1, 600)
    if tool == 'navigate_to_pose':
        for key in ('x', 'y'):
            number(key, -1e6, 1e6)
        number('heading_deg', -180, 180)
        number('position_tolerance_m', .001, 1)
        number('heading_tolerance_deg', .1, 30)
        if not isinstance(args.get('map_name'), str) or not args['map_name'].strip():
            raise HttpError(400, 'map_name 必须为非空字符串')
    elif args.get('action') == 'home':
        if 'height_mm' in args:
            raise HttpError(400, 'home 不接受 height_mm')
    elif args.get('action') == 'height':
        if type(args.get('height_mm')) is not int or not 0 <= args['height_mm'] <= 750:
            raise HttpError(400, 'height_mm 必须为 0–750 的整数')
    else:
        raise HttpError(400, 'action 必须为 home 或 height')
    return dict(args)


class Motion:
    """Serialize both actuators and retain uncertain outcomes in visible state."""

    def __init__(self, config, emit, *, enabled=True, navigation_factory=Navigation, lift_factory=LiftController):
        self.config, self.emit, self.enabled = config, emit, enabled
        self.navigation_factory, self.lift_factory = navigation_factory, lift_factory
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.done.set()
        self.accepting = False
        self.closed = False
        self.active = None
        self.last_result = None
        self.fault = None
        self.fault_id = None
        self.last_fault_clear = None
        self.http = JsonServer({
            ('POST', '/state'): lambda q, b: self._request(b, lambda: self.state()),
            ('POST', '/execute'): CancellableHandler(lambda q, b, gone: self._request(
                b, lambda: self.execute(b.get('tool'), b.get('arguments'), gone))),
            ('POST', '/stop'): lambda q, b: self._request(b, self.stop),
            ('POST', '/clear-fault'): lambda q, b: self._request(b, lambda: self.clear_fault(b.get('arguments'))),
        }, name='real-motion').start()

    def _request(self, body, action):
        if not isinstance(body, dict) or not isinstance(body.get('token'), str) or not secrets.compare_digest(body['token'], self.token):
            raise HttpError(403, '无效的真机连接凭据')
        return action()

    def state(self):
        """Read cached execution state and deployment metadata without touching actuators."""
        with self.lock:
            return {'mode': 'real', 'motion_enabled': self.enabled, 'active': self.active,
                    'fault': self.fault, 'last_result': self.last_result,
                    'fault_id': self.fault_id, 'last_fault_clear': self.last_fault_clear,
                    'map_name': self.config['navigation']['map'],
                    'limits': {'linear_m_s': LINEAR_SPEED_M_S, 'angular_rad_s': ANGULAR_SPEED_RAD_S,
                               'lift_rpm': LiftController.MOTOR_RPM, 'lift_height_mm': [0, 750]},
                    'tools': ['navigate_to_pose', 'lift_move', 'clear_motion_fault'],
                    'lift_home': 'explicit; encoder origin must be established after each motor power cycle'}

    def _latch_fault(self, fault):
        """Assign a new fault identity under the runtime lock, rejecting stale recovery requests."""
        self.fault, self.fault_id = fault, secrets.token_urlsafe(12)

    def clear_fault(self, args):
        """Clear one inspected software fault without device IO, replay or cancellation reset.

        Operator confirmation is an assertion supplied by the caller, not sensor evidence.
        Keep the failed result and an audit record so clearance cannot turn failure into success.
        """
        fields = {'fault_id', 'operator_confirmed_stopped', 'reason'}
        if not isinstance(args, dict) or args.keys() != fields:
            raise HttpError(400, '清除故障需要 fault_id、operator_confirmed_stopped 和 reason')
        if not isinstance(args['fault_id'], str) or not args['fault_id'].strip():
            raise HttpError(400, 'fault_id 必须为当前状态返回的非空故障编号')
        if args['operator_confirmed_stopped'] is not True:
            raise HttpError(400, '需要操作员确认设备已停稳并检查现场；不能自动推断')
        if not isinstance(args['reason'], str) or not args['reason'].strip() or len(args['reason']) > 1000:
            raise HttpError(400, 'reason 必须为 1–1000 字符的现场检查或恢复说明')
        with self.lock:
            if self.closed or not self.done.is_set() or self.active is not None:
                raise HttpError(409, '动作尚未结束或运行时正在关闭，不能清除故障')
            if self.fault is None:
                return {'state': 'succeeded', 'cleared': False, 'message': '当前没有软件故障，无需清除'}
            if args['fault_id'] != self.fault_id:
                raise HttpError(409, '故障编号已变化，请重新读取 ego_state 并检查当前故障')
            record = {'fault_id': self.fault_id, 'previous_fault': self.fault,
                      'reason': args['reason'].strip(), 'confirmation_source': 'operator', 'time': time.time()}
            self.fault, self.fault_id = None, None
            self.last_fault_clear = record
            result = {'state': 'succeeded', 'cleared': True, 'record': record,
                      'motion_admitted': self.enabled and self.accepting and not self.cancel.is_set(),
                      'message': '软件故障已清除；未复位设备、未归零、未重试上次动作'}
        self.emit('INFO', 'motion', '已清除软件故障锁定；未发送设备指令', record)
        return result

    def begin(self):
        """Admit one harness turn after the previous turn's requests have settled."""
        with self.lock:
            if self.closed or not self.done.is_set():
                raise HttpError(409, '真机执行器正在关闭或仍有动作未结束')
            self.accepting = True
            self.cancel.clear()

    def execute(self, tool, args, peer_gone=lambda: False):
        """Execute one validated operation, with no automatic retry on uncertain results."""
        args = validate_arguments(tool, args)
        with self.lock:
            if not self.enabled or not self.accepting or self.closed:
                raise HttpError(409, '当前连接不接受运动命令')
            if self.fault or not self.done.is_set() or self.cancel.is_set():
                raise HttpError(409, '已有动作、已取消或存在未排除故障；请检查状态')
            self.done.clear()
            self.active = {'tool': tool, 'arguments': args}
        driver = None
        result = None
        cancelled = lambda: self.cancel.is_set() or peer_gone()
        try:
            if cancelled():
                raise InterruptedError('调用已取消')
            self.emit('INFO', 'motion', '开始执行 ' + tool, args)
            if tool == 'navigate_to_pose':
                driver = self.navigation_factory(self.config['navigation'], cancelled)
                result = driver.navigate(args)
            else:
                cfg = self.config['lift']
                driver = self.lift_factory(cfg['port'], motor_id=cfg['motor_id'],
                                           serial_timeout=cfg['serial_timeout_s'], cancelled=cancelled)
                state = (driver.reset(timeout=args['timeout_s']) if args['action'] == 'home'
                         else driver.set_height(args['height_mm'], timeout=args['timeout_s']))
                result = {'state': 'succeeded', 'action': args['action'], 'feedback': state}
        except Exception as exc:
            result = {'state': 'failed', 'error': str(exc), 'physical_result': 'unknown'}
            if isinstance(exc, (LiftError, NavigationError)) and exc.feedback is not None:
                result['feedback'] = exc.feedback
            if driver is not None:
                try:
                    acknowledgement = driver.stop()
                    result['stop'] = acknowledgement if tool == 'navigate_to_pose' else {
                        'requested': True, 'write_acknowledged': True,
                        'physical_stop_confirmed': acknowledgement['speed_rpm'] == 0,
                        'feedback': acknowledgement}
                except Exception as stop_error:
                    result['stop'] = {'error': str(stop_error), 'physical_stop_confirmed': False}
                    if isinstance(stop_error, (LiftError, NavigationError)) and stop_error.feedback is not None:
                        result['stop']['feedback'] = stop_error.feedback
        finally:
            if driver is not None:
                try:
                    driver.close()
                except Exception as close_error:
                    result = {'state': 'failed', 'physical_result': 'unknown',
                              'error': '设备关闭失败: ' + str(close_error), 'operation_result': result}
            with self.lock:
                self.last_result = result
                if result is None or result['state'] != 'succeeded':
                    self._latch_fault(result or {'error': '执行线程异常结束', 'physical_result': 'unknown'})
                self.active = None
                self.done.set()
        self.emit('INFO' if result['state'] == 'succeeded' else 'ERROR', 'motion', '执行结果 ' + tool, result)
        return result

    def stop(self):
        """Cancel this turn and wait for the driver to report its stop outcome."""
        with self.lock:
            self.cancel.set()
            self.accepting = False
        if not self.done.wait(20):
            with self.lock:
                self._latch_fault({'error': '停止等待超时，请现场检查或使用急停', 'physical_result': 'unknown'})
            raise HttpError(504, self.fault['error'])
        return self.state()

    def close(self):
        """Revoke admission before stopping the private server; retain failures for the caller."""
        with self.lock:
            self.closed = True
        self.stop()
        self.http.stop()
