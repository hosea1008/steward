"""NAVIS HTTP/rosbridge transport extracted from the validated navigation client.

Only a single map pose is dispatched. No routes, object locations, jog commands,
automatic retries or recovery movements are inferred here.
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

# Defaults are overridable within these fixed caps; deployment cannot raise the caps.
LINEAR_SPEED_M_S = 0.2
ANGULAR_SPEED_RAD_S = 0.2
MAX_LINEAR_SPEED_M_S = 0.5
MAX_ANGULAR_SPEED_RAD_S = 0.4
POSE_TOPIC = '/agx_smach_state'
STATUS_TOPIC = '/run_management/global_status'


class NavigationError(RuntimeError):
    """Navigation failure retaining controller feedback without credentials."""

    def __init__(self, message, feedback):
        super().__init__(message)
        self.feedback = feedback


class Navigation:
    """One operation owns one socket; all waits service its heartbeat and cancellation."""

    def __init__(self, config, cancelled):
        self.config, self.cancelled = config, cancelled
        self.ws = None
        self.token = None
        self.client_id = uuid.uuid4().hex
        self.next_ping = 0.
        self.dispatched = False
        self.cancel_acknowledged = False
        self.task = None
        self.subscriptions = set()
        self.feedback = {}
        self.feedback_received_at = {}

    def _http(self, method, path, body=None):
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = self.token
        request = Request(self.config['http_url'].rstrip('/') + path, method=method,
                          data=None if body is None else json.dumps(body).encode(), headers=headers)
        self._ping()
        with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get('code') != 0:
            # Never retain login response data, authorization headers or request bodies.
            detail = {key: result.get(key) for key in ('code', 'success', 'msg')} if isinstance(result, dict) else {}
            self.feedback['http_error'] = {'path': path.split('?')[0], **detail}
            self._fail(f'NAVIS {path.split("?")[0]} 拒绝请求: {detail.get("code")} {detail.get("msg", "响应格式无效")}')
        return result

    def _send(self, message):
        self.ws.send(json.dumps(message))

    def _ping(self):
        if self.ws is not None and time.monotonic() >= self.next_ping:
            self._send(dict(op='ping', id=self.client_id, timeStamp=int(time.time()*1000)))
            self.next_ping = time.monotonic() + 1

    def _wait(self, predicate, timeout=5, *, stopping=False):
        from websocket import WebSocketTimeoutException
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not stopping and self.cancelled():
                raise InterruptedError('导航已取消')
            self._ping()
            self.ws.settimeout(min(.2, max(.001, deadline-time.monotonic())))
            try:
                message = json.loads(self.ws.recv())
            except WebSocketTimeoutException:
                continue
            if message.get('op') == 'publish' and message.get('topic') in (POSE_TOPIC, STATUS_TOPIC):
                self.feedback[message['topic']] = message.get('msg', {})
                self.feedback_received_at[message['topic']] = time.time()
            if predicate(message):
                return message
        raise TimeoutError('导航反馈超时')

    def _topic(self, topic, timeout=5, *, stopping=False):
        self._subscribe(topic)
        return self._wait(lambda m: m.get('op') == 'publish' and m.get('topic') == topic,
                          timeout, stopping=stopping)['msg']

    def _subscribe(self, topic):
        # global_status is event-driven: retain its subscription across pose reads.
        if topic not in self.subscriptions:
            self._send(dict(op='subscribe', topic=topic))
            self.subscriptions.add(topic)

    def _task_event(self, timeout, *, stopping=False):
        return self._wait(lambda m: m.get('op') == 'publish' and m.get('topic') in (POSE_TOPIC, STATUS_TOPIC),
                          timeout, stopping=stopping)

    @staticmethod
    def _task_name(state):
        """NAVIS uses taskName=list_task as a type tag and btName as the list identity."""
        return state.get('btName') if state.get('taskName') == 'list_task' else state.get('taskName')

    def _fail(self, message):
        raise NavigationError(message, {'task': self.task, **self.feedback})

    def _service(self, service, args, field, *, stopping=False):
        identifier = uuid.uuid4().hex
        self._send(dict(op='call_service', service=service, args=args, id=identifier))
        response = self._wait(lambda m: m.get('op') == 'service_response' and m.get('id') == identifier,
                              stopping=stopping)
        if response.get('result') is not True or response.get('values', {}).get(field) is not True:
            self.feedback['service_error'] = response
            self._fail(f'导航服务未确认: {service}；{response.get("values", {}).get("message", "")}')

    def connect(self):
        """Authenticate and open rosbridge without issuing a movement command."""
        from websocket import create_connection
        password = os.environ.get(self.config['password_env'])
        if not password:
            raise RuntimeError('缺少导航凭据环境变量: ' + self.config['password_env'])
        self.token = self._http('POST', '/user/passport/login', {
            'username': self.config['username'], 'password': password})['data']
        self.ws = create_connection(self.config['websocket_url'], timeout=2, http_no_proxy=['*'])
        self._ping()

    def read_state(self, timeout_s):
        """Read both topics on a new connection; timestamps denote receipt, not sensor freshness.

        Like NAVIS get_position/get_nav_status, pose comes from agx_smach_state.
        Missing or malformed telemetry is an error, never a fabricated zero pose.
        No navigation configuration, goal, cancellation or device reset is sent.
        """
        self.connect()
        for topic in (POSE_TOPIC, STATUS_TOPIC):
            self._subscribe(topic)
        try:
            self._wait(lambda m: all(topic in self.feedback for topic in (POSE_TOPIC, STATUS_TOPIC)), timeout_s)
        except TimeoutError:
            self._fail('导航状态读取超时，未收到完整反馈')
        pose, status = self.feedback[POSE_TOPIC], self.feedback[STATUS_TOPIC]
        if not isinstance(pose, dict) or not isinstance(status, dict):
            self._fail('导航状态反馈格式无效')
        for key in ('x', 'y', 'rotation', 'percentage'):
            value = pose.get(key)
            if type(value) not in (float, int) or not math.isfinite(value):
                self._fail('导航状态缺少有效数值: ' + key)
        if (type(pose.get('locationLost')) is not bool or type(pose.get('smachStatus')) is not int
                or type(status.get('status')) is not int
                or any(not isinstance(pose.get(k), str) for k in ('mapName', 'taskName', 'btName'))
                or not isinstance(status.get('map_name'), str) or not isinstance(status.get('text'), str)):
            self._fail('导航定位、地图或任务状态字段无效')
        return {'state': 'succeeded', 'source': 'navis',
                'pose': {'x': pose['x'], 'y': pose['y'], 'heading_deg': pose['rotation']},
                'map_name': pose['mapName'], 'location_lost': pose['locationLost'],
                'pose_valid': not pose['locationLost'] and bool(pose['mapName']),
                'scheduler_status': pose['smachStatus'], 'navigation_status': status,
                'task': {'type': pose['taskName'], 'name': self._task_name(pose)},
                'progress_percent': pose['percentage'],
                'received_at_unix_s': {'pose': self.feedback_received_at[POSE_TOPIC],
                                       'navigation_status': self.feedback_received_at[STATUS_TOPIC]}}

    def navigate(self, args):
        """Dispatch validated, fully resolved arguments once; verify terminal state and measured pose."""
        self.connect()
        state = self._topic(POSE_TOPIC)
        status = self._topic(STATUS_TOPIC)
        if (status.get('status') not in (0, 2, 3) or state.get('smachStatus') not in (0, 2, 3)
                or state.get('locationLost') is not False):
            self._fail('导航未空闲或定位未确认；请先检查现场状态')
        previous_task = self._task_name(state)
        nav = self._topic('/slam_status').get('nav', {})
        if nav.get('state') is not True or nav.get('name') != args['map_name']:
            raise RuntimeError('导航地图未就绪或不匹配；请在导航系统加载所选地图')
        self._service('/run_management/set_navi_params', {'data': [
            {'name': 'navis_ctrl_max_vel_x', 'data': str(args['linear_speed_m_s'])},
            {'name': 'navis_ctrl_max_vel_theta', 'data': str(args['angular_speed_rad_s'])},
        ], 'params_type': 0}, 'result')
        self._service('/run_management/switch_obstacle_avoid_model', {'data': True}, 'success')
        task = '_steward_' + uuid.uuid4().hex
        self.task = task
        self._http('POST', '/set_task', {
            'mapName': args['map_name'], 'taskName': task, 'editedName': task, 'mode': 'point',
            'speed': args['linear_speed_m_s'], 'rotateSpeed': args['angular_speed_rad_s'], 'evadible': 1,
            'remark': '', 'personName': '', 'points': [{
                'isNew': True, 'id': 'task-' + uuid.uuid4().hex,
                'position': {'x': args['x'], 'y': args['y'], 'theta': args['heading_deg']},
                'cpx': 0, 'cpy': 0, 'pointType': 'navigation', 'pointName': '', 'actions': [],
            }],
        })
        if self.cancelled():
            raise InterruptedError('导航已取消，未发起运动')
        # A lost HTTP reply can still mean the robot accepted the goal.
        self.dispatched = True
        self._http('POST', '/run_list_task', {'mapName': args['map_name'], 'taskName': task, 'loopTime': 1})
        seen_running = False
        seen_task = False
        status = None
        deadline = time.monotonic() + args['timeout_s']
        while time.monotonic() < deadline:
            event = self._task_event(min(5, deadline-time.monotonic()))
            data = event['msg']
            if event['topic'] == STATUS_TOPIC:
                if data.get('map_name') and data['map_name'] != args['map_name']:
                    self._fail('导航地图已被其他控制端替换')
                status = data.get('status')
                if status == 1:
                    seen_running = True
                elif (seen_running or seen_task) and status not in (0, 3):
                    self._fail(f'导航异常状态: {status}；{data.get("text", "")}')
                continue
            state = data
            if state.get('locationLost') is not False:
                self._fail('导航定位丢失或未知')
            if state.get('mapName') and state['mapName'] != args['map_name']:
                self._fail('导航地图已被其他控制端替换')
            identity = self._task_name(state)
            if identity == task:
                seen_task = True
            elif identity and (seen_task or identity != previous_task):
                self._fail('当前导航任务已被其他控制端替换')
            # Lower-level completion can precede the scheduler's final pose. The
            # scheduler may clear its task identity when it reaches a terminal state.
            if (seen_running and seen_task and status == 3 and state.get('smachStatus') == 3
                    and identity in (task, '', None)):
                distance = math.hypot(state['x']-args['x'], state['y']-args['y'])
                heading = abs((state['rotation']-args['heading_deg']+180) % 360-180)
                if not (distance <= args['position_tolerance_m'] and heading <= args['heading_tolerance_deg']):
                    self._fail('任务已结束，但实测位姿未达到允许误差')
                self.dispatched = False
                result = {'state': 'succeeded', 'pose': state, 'distance_error_m': distance,
                          'heading_error_deg': heading, 'task': task, 'navigation_status': status}
                try:
                    self._http('DELETE', '/delete_task?' + urlencode({'mapName': args['map_name'], 'taskName': task}))
                except Exception as exc:
                    result['cleanup_error'] = str(exc)
                return result
        self._fail('导航任务超时')

    def stop(self):
        """Request cancellation after dispatch; acknowledge controller state, not physical rest."""
        if not self.dispatched:
            return {'requested': False, 'physical_stop_confirmed': False}
        self._subscribe(POSE_TOPIC)
        # NAVIS Board's End button uses SmachOperate=4. Cancelling navi_task alone
        # stops the lower-level goal but leaves a list task occupying the scheduler.
        self._service('/agx_smach_operate', {'operate': 4, 'mode': '', 'values': [], 'range': [],
                      'timestamp': '', 'mapName': '', 'taskList': [], 'id': '', 'taskName': ''},
                      'success', stopping=True)
        try:
            event = self._wait(lambda m: m.get('op') == 'publish' and m.get('topic') == POSE_TOPIC
                               and m.get('msg', {}).get('smachStatus') in (0, 2, 3), timeout=5, stopping=True)
        except TimeoutError:
            self._fail('导航取消未确认；请现场检查或使用急停')
        self.cancel_acknowledged = True
        self.dispatched = False
        return {'requested': True, 'controller_acknowledged': True, 'physical_stop_confirmed': False,
                'scheduler_status': event['msg']}

    def close(self):
        """Close transport; stopping an accepted goal is the operation owner's responsibility."""
        if self.ws is not None:
            self.ws.close(timeout=1)
