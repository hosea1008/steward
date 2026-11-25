"""NAVIS virtual joystick control, owned by one serialized Motion operation.

Derived from NAVIS/move.py: body-frame Twist at 10 Hz plus rosbridge heartbeat.
No map, material locations, HTTP login, navigation goals or runtime NAVIS imports.
The protocol has no motion acknowledgement; a zero write does not prove rest.
"""
from __future__ import annotations

import json
import math
import time
import uuid

from steward.net import HttpError

DEFAULT_SPEED = .2
MAX_SPEED = .5
MAX_DURATION_S = 60
PUBLISH_INTERVAL_S = .1
TOPIC = '/run_management/virtual_joy'
DIRECTIONS = {'forward': (1, 0, 0), 'backward': (-1, 0, 0),
              'turn_left': (0, 0, 1), 'turn_right': (0, 0, -1),
              'shift_left': (0, 1, 0), 'shift_right': (0, -1, 0)}


def resolve(args):
    """Resolve the default speed and reject unbounded or invalid model input before IO."""
    if not isinstance(args, dict) or args.keys() - {'direction', 'speed', 'duration_s'}:
        raise HttpError(400, '定时移动只接受 direction、speed 和 duration_s')
    direction = args.get('direction')
    if not isinstance(direction, str) or direction not in DIRECTIONS:
        raise HttpError(400, 'direction 必须为 forward/backward/turn_left/turn_right/shift_left/shift_right')
    result = {'speed': DEFAULT_SPEED, **args}
    for key, minimum, maximum in [('speed', 0, MAX_SPEED), ('duration_s', 0, MAX_DURATION_S)]:
        value = result.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum < value <= maximum:
            raise HttpError(400, f'{key} 必须大于 {minimum} 且不超过 {maximum}')
    if result['duration_s'] < .1:
        raise HttpError(400, 'duration_s 必须至少为 0.1 秒')
    return result


class TimedMoveError(RuntimeError):
    """Failure retains command evidence, not a claim about chassis position or speed."""

    def __init__(self, message, feedback):
        super().__init__(message)
        self.feedback = feedback


class TimedMove:
    """One socket services commands, heartbeat and cancellation without background senders.

    Motion owns stop-on-error and close. A partially sent first Twist still requires
    a stop attempt. No reconnect, navigation cancellation, or movement retry occurs.
    """

    def __init__(self, config, cancelled):
        self.config, self.cancelled = config, cancelled
        self.ws = None
        self.client_id = uuid.uuid4().hex
        self.next_ping = 0.
        self.last_pong = None
        self.motion_started = False
        self.commands_sent = 0

    def _check_cancel(self):
        if self.cancelled():
            raise InterruptedError('定时底盘移动已取消')

    def _send(self, message):
        self.ws.settimeout(.2)
        self.ws.send(json.dumps(message))

    def _ping(self):
        self._send({'op': 'ping', 'id': self.client_id, 'timeStamp': int(time.time() * 1000)})
        self.next_ping = time.monotonic() + 1

    def _receive(self, timeout):
        from websocket import WebSocketTimeoutException
        self.ws.settimeout(max(.001, timeout))
        try:
            raw = self.ws.recv()
        except WebSocketTimeoutException:
            return
        if not raw:
            raise ConnectionError('NAVIS 连接已关闭')
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise ValueError('NAVIS 响应不是 JSON 对象')
        if message.get('op') == 'pong':
            self.last_pong = time.monotonic()
        elif message.get('op') == 'status' and message.get('level') == 'error':
            raise RuntimeError('NAVIS 拒绝指令: ' + str(message.get('msg', ''))[:500])

    @staticmethod
    def _twist(x=0., y=0., yaw=0.):
        return {'op': 'publish', 'topic': TOPIC, 'type': 'geometry_msgs/Twist',
                'msg': {'linear': {'x': x, 'y': y, 'z': 0.},
                        'angular': {'x': 0., 'y': 0., 'z': yaw}}}

    def move(self, args):
        """Publish for a finite interval, then write zero; completion is command-level only."""
        from websocket import create_connection
        self._check_cancel()
        try:
            self.ws = create_connection(self.config['websocket_url'], timeout=2, http_no_proxy=['*'])
            self._check_cancel()
            self._ping()
            handshake_deadline = time.monotonic() + 2
            while self.last_pong is None:
                self._check_cancel()
                if time.monotonic() >= handshake_deadline:
                    raise TimeoutError('NAVIS 心跳握手超时，未发送移动指令')
                self._receive(.05)
            start = time.monotonic()
            deadline, next_command = start + args['duration_s'], start
            components = [value * args['speed'] for value in DIRECTIONS[args['direction']]]
            while True:
                self._check_cancel()
                now = time.monotonic()
                if now >= deadline:
                    break
                if now - self.last_pong >= 3:
                    raise TimeoutError('NAVIS 心跳响应超时')
                if now >= self.next_ping:
                    self._ping()
                # Do not replay missed ticks or send a late nonzero command after the deadline.
                now = time.monotonic()
                if now >= deadline:
                    break
                if now >= next_command:
                    self._check_cancel()
                    self.motion_started = True
                    self._send(self._twist(*components))
                    self.commands_sent += 1
                    next_command = time.monotonic() + PUBLISH_INTERVAL_S
                remaining = min(deadline, next_command) - time.monotonic()
                if remaining > 0:
                    self._receive(min(.05, remaining))
            self._check_cancel()
            stop = self.stop()
            return {'state': 'succeeded', 'command_stream_complete': True, **args,
                    'speed_unit': 'rad/s' if args['direction'].startswith('turn_') else 'm/s',
                    'commands_sent': self.commands_sent, 'stop': stop,
                    'message': '定时速度指令已发送并写入零速度；未测量位移或确认物理停稳'}
        except Exception as exc:
            raise TimedMoveError(str(exc), {'motion_started': self.motion_started,
                                           'commands_sent': self.commands_sent}) from exc

    def stop(self):
        """Write a zero Twist even after cancellation; never equate a socket write with rest."""
        if self.ws is None or not self.motion_started:
            return {'requested': False, 'physical_stop_confirmed': False}
        self._send(self._twist())
        return {'requested': True, 'write_acknowledged': True,
                'controller_acknowledged': False, 'physical_stop_confirmed': False}

    def close(self):
        """Close only after Motion has handled the movement outcome and stop attempt."""
        if self.ws is not None:
            self.ws.close(timeout=1)
