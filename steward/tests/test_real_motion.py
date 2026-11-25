"""No hardware: admission, cancellation, pose feedback and fixed speed enforcement."""
from copy import deepcopy
from collections import deque
from itertools import chain, repeat
import threading

import pytest

from steward.ego.real.console.config import DEFAULT_CONFIG_DIR, load_configs
from steward.ego.real.drivers.lift import LiftController, LiftError
from steward.ego.real.drivers.navigation import Navigation, POSE_TOPIC, STATUS_TOPIC
from steward.ego.real.motion import Motion, validate_arguments
from steward.net import HttpError, call

NAV = dict(x=1.2, y=-.4, heading_deg=90, map_name='fixture-map', timeout_s=2,
           position_tolerance_m=.05, heading_tolerance_deg=2)


def config():
    return deepcopy(load_configs(DEFAULT_CONFIG_DIR)['agilex-cobot-magic'])


@pytest.mark.parametrize('tool,args', [
    ('navigate_to_pose', {**NAV, 'speed': 1}),
    ('navigate_to_pose', {**NAV, 'x': float('nan')}),
    ('navigate_to_pose', {**NAV, 'timeout_s': True}),
    ('navigate_to_pose', {**NAV, 'position_tolerance_m': 5}),
    ('lift_move', dict(action='home', height_mm=0, timeout_s=1)),
    ('lift_move', dict(action='height', height_mm=-1, timeout_s=1)),
    ('lift_move', dict(action='height', height_mm=1.5, timeout_s=1)),
    ('lift_move', dict(action='height', height_mm=True, timeout_s=1)),
])
def test_reject_model_arguments_before_transport(tool, args):
    with pytest.raises(HttpError):
        validate_arguments(tool, args)


@pytest.mark.parametrize('key,cap', [('linear_speed_m_s', .5), ('angular_speed_rad_s', .4)])
@pytest.mark.parametrize('value', [None, True, float('nan'), float('inf'), 0, .09, '0.2', 'above_cap'])
def test_coordinate_speed_rejected_before_transport(key, cap, value):
    m = Motion(config(), lambda *a: None,
               navigation_factory=lambda *a: pytest.fail('invalid speed opened transport'))
    try:
        m.begin()
        with pytest.raises(HttpError):
            m.execute('navigate_to_pose', dict(NAV, **{key: cap + .01 if value == 'above_cap' else value}))
        assert m.state()['active'] is None
    finally:
        m.close()


@pytest.mark.parametrize('speeds,expected', [
    ({}, (.2, .2)),
    ({'linear_speed_m_s': .5}, (.5, .2)),
    ({'angular_speed_rad_s': .4}, (.2, .4)),
    ({'linear_speed_m_s': .5, 'angular_speed_rad_s': .4}, (.5, .4)),
])
def test_coordinate_speed_defaults_and_overrides_reach_controller(speeds, expected):
    nav = NavFixture([pose(0), pose(1), pose(3)])
    nav.navigate(validate_arguments('navigate_to_pose', dict(NAV, **speeds)))
    task = next(body for path, body in nav.requests if path == '/set_task')
    assert (task['speed'], task['rotateSpeed']) == expected
    params = next(body for path, body in nav.requests if path == '/run_management/set_navi_params')
    assert {p['name']: float(p['data']) for p in params['data']} == {
        'navis_ctrl_max_vel_x': expected[0], 'navis_ctrl_max_vel_theta': expected[1]}


def test_private_api_no_actuation_and_turn_admission():
    opened = []
    def forbidden(*args, **kwargs):
        opened.append(True)
        raise AssertionError('hardware must not open')
    m = Motion(config(), lambda *a: None, enabled=False, navigation_factory=forbidden, lift_factory=forbidden)
    try:
        with pytest.raises(HttpError) as error:
            call('POST', m.http.url+'/state', {'token': 'wrong'})
        assert error.value.status == 403
        assert call('POST', m.http.url+'/state', {'token': m.token})['active'] is None
        m.begin()
        with pytest.raises(HttpError):
            m.execute('navigate_to_pose', NAV)
        assert not opened
    finally:
        m.close()


def test_lift_height_and_explicit_home_are_distinct():
    calls = []
    class Lift:
        def __init__(self, port, **kw):
            calls.append(('open', port))
        def set_height(self, height, timeout):
            calls.append(('height', height, timeout))
            return dict(height=height, speed_rpm=0)
        def reset(self, timeout):
            calls.append(('home', timeout))
            return dict(height=0, speed_rpm=0)
        def close(self):
            calls.append(('close',))
    m = Motion(config(), lambda *a: None, lift_factory=Lift)
    try:
        m.begin()
        assert m.execute('lift_move', dict(action='height', height_mm=220, timeout_s=12))['state'] == 'succeeded'
        assert [c[0] for c in calls] == ['open', 'height', 'close']
        assert m.execute('lift_move', dict(action='home', timeout_s=25))['state'] == 'succeeded'
        assert calls[-2] == ('home', 25)
        m.stop()
        with pytest.raises(HttpError):
            m.execute('lift_move', dict(action='home', timeout_s=1))
    finally:
        m.close()


def test_peer_disconnect_stops_active_motion_and_latches_uncertainty():
    entered, gone = threading.Event(), threading.Event()
    calls = []
    class Nav:
        def __init__(self, cfg, cancelled):
            self.cancelled = cancelled
        def navigate(self, args):
            entered.set()
            assert gone.wait(5)
            assert self.cancelled()
            raise InterruptedError('peer disconnected')
        def stop(self):
            calls.append('stop')
            raise OSError('stop feedback lost')
        def close(self):
            calls.append('close')
    m = Motion(config(), lambda *a: None, navigation_factory=Nav)
    m.begin()
    worker = threading.Thread(target=lambda: m.execute('navigate_to_pose', NAV, gone.is_set))
    try:
        worker.start()
        assert entered.wait(5)
        with pytest.raises(HttpError):
            m.execute('lift_move', dict(action='home', timeout_s=1))
        gone.set()
        worker.join(5)
        assert not worker.is_alive()
        assert calls == ['stop', 'close']
        assert m.state()['fault']['stop']['physical_stop_confirmed'] is False
        with pytest.raises(HttpError):
            m.execute('navigate_to_pose', NAV)
    finally:
        gone.set()
        worker.join(5)
        m.close()


class NavFixture(Navigation):
    def __init__(self, states, *, speed_ok=True, cancelled=lambda: False, initial_status=0):
        super().__init__({}, cancelled)
        self.states = iter(states)
        self.initial_pose = next(self.states)
        self.initial_status = initial_status
        self.events = deque()
        self.requests = []
        self.speed_ok = speed_ok
        self.task = None
    def connect(self):
        pass
    def _topic(self, topic, *args, **kwargs):
        if topic == '/slam_status':
            return {'nav': {'state': True, 'name': NAV['map_name']}}
        value = {'status': self.initial_status} if topic == STATUS_TOPIC else self.initial_pose
        self.feedback[topic] = value
        return value
    def _task_event(self, timeout, **kwargs):
        if not self.events:
            state = dict(next(self.states))
            status = state.pop('global_status', state['smachStatus'])
            state.setdefault('taskName', 'list_task')
            state.setdefault('btName', self.task)
            self.events.extend([{'topic': STATUS_TOPIC, 'msg': {'status': status}},
                                {'topic': POSE_TOPIC, 'msg': state}])
        event = self.events.popleft()
        self.feedback[event['topic']] = event['msg']
        return event
    def _service(self, service, args, field):
        self.requests.append((service, args))
        if not self.speed_ok:
            raise RuntimeError('speed not acknowledged')
    def _http(self, method, path, body=None):
        self.requests.append((path, body))
        return {'code': 0}


def pose(status, x=1.2):
    return dict(smachStatus=status, locationLost=False, x=x, y=-.4, rotation=90)


def telemetry_driver(pose_changes=None):
    import json
    messages = deque([
        {'op': 'publish', 'topic': STATUS_TOPIC,
         'msg': {'status': 2, 'text': 'task is canceled', 'map_name': 'fixture-map'}},
        {'op': 'publish', 'topic': POSE_TOPIC,
         'msg': {**pose(1), 'mapName': 'fixture-map', 'taskName': 'list_task',
                 'btName': 'fixture-task', 'percentage': 35, **(pose_changes or {})}},
    ])
    class Socket:
        def __init__(self):
            self.sent = []
        def send(self, message):
            self.sent.append(json.loads(message))
        def settimeout(self, timeout):
            pass
        def recv(self):
            return json.dumps(messages.popleft())
    nav = Navigation({}, lambda: False)
    nav.ws = Socket()
    nav.connect = lambda: None
    return nav


@pytest.mark.parametrize('lost', [False, True])
def test_navigation_observation_reads_device_pose_without_actuation(lost):
    nav = telemetry_driver({'locationLost': lost})
    result = nav.read_state(1)
    assert result['pose'] == {'x': 1.2, 'y': -.4, 'heading_deg': 90}
    assert result['pose_valid'] is not lost
    assert result['scheduler_status'] == 1
    assert result['navigation_status']['status'] == 2
    assert result['task'] == {'type': 'list_task', 'name': 'fixture-task'}
    assert all(t > 0 for t in result['received_at_unix_s'].values())
    assert {m['op'] for m in nav.ws.sent} <= {'subscribe', 'ping'}
    assert not nav.dispatched


@pytest.mark.parametrize('changes', [{'x': None}, {'rotation': float('nan')}, {'locationLost': 'false'}])
def test_navigation_observation_rejects_incomplete_or_invalid_pose(changes):
    nav = telemetry_driver(changes)
    with pytest.raises(RuntimeError, match='数值|字段无效'):
        nav.read_state(1)


def test_navigation_observation_timeout_preserves_partial_feedback():
    nav = telemetry_driver()
    nav.feedback[POSE_TOPIC] = pose(1)
    def timeout(*args, **kwargs):
        raise TimeoutError()
    nav._wait = timeout
    with pytest.raises(RuntimeError, match='未收到完整反馈') as error:
        nav.read_state(.1)
    assert error.value.feedback[POSE_TOPIC] == pose(1)


@pytest.mark.parametrize('fail', [False, True])
def test_read_only_navigation_works_with_fault_and_actuation_disabled(fail):
    calls = []
    class Nav:
        def __init__(self, cfg, cancelled):
            pass
        def read_state(self, timeout):
            calls.append(('read', timeout))
            if fail:
                raise TimeoutError('fixture telemetry unavailable')
            return {'state': 'succeeded', 'pose_valid': False}
        def close(self):
            calls.append(('close',))
        def stop(self):
            pytest.fail('reading must not send stop')
    m = Motion(config(), lambda *args: None, enabled=False, navigation_factory=Nav)
    try:
        m._latch_fault({'error': 'existing motion fault'})
        m.last_result = {'state': 'failed', 'error': 'preserve me'}
        before = m.state()
        result = call('POST', m.http.url+'/navigation-state', {'token': m.token, 'arguments': {'timeout_s': 1}})
        assert result['state'] == ('failed' if fail else 'succeeded')
        assert m.state() == before
        assert calls == [('read', 1), ('close',)]
        assert not m.readers
    finally:
        m.close()


@pytest.mark.parametrize('args', [{}, {'timeout_s': True}, {'timeout_s': 31}, {'timeout_s': 1, 'x': 1}])
def test_navigation_observation_validates_arguments_before_transport(args):
    m = Motion(config(), lambda *args: None, navigation_factory=lambda *args: pytest.fail('invalid read opened transport'))
    try:
        with pytest.raises(HttpError):
            m.read_navigation_state(args)
    finally:
        m.close()


@pytest.mark.parametrize('peer_disconnect', [False, True])
def test_navigation_observation_closes_on_disconnect_without_stop_or_fault(peer_disconnect):
    entered, gone, closed = threading.Event(), threading.Event(), threading.Event()
    class Nav:
        def __init__(self, cfg, cancelled):
            self.cancelled = cancelled
        def read_state(self, timeout):
            import time
            entered.set()
            deadline = time.monotonic() + 3
            while not self.cancelled():
                assert time.monotonic() < deadline
                gone.wait(.01)
            raise InterruptedError('read cancelled')
        def close(self):
            closed.set()
        def stop(self):
            pytest.fail('read cancellation must not stop the robot')
    m = Motion(config(), lambda *args: None, navigation_factory=Nav)
    worker = threading.Thread(target=lambda: m.read_navigation_state({'timeout_s': 5}, gone.is_set))
    try:
        worker.start()
        assert entered.wait(2)
        if peer_disconnect:
            gone.set()
        else:
            m.close()
        worker.join(3)
        assert not worker.is_alive()
        assert closed.is_set() and not m.readers and m.fault is None
    finally:
        gone.set()
        worker.join(3)
        m.close()


def test_navigation_requires_new_execution_and_checks_measured_pose():
    nav = NavFixture([pose(0), pose(3), pose(1), pose(3)])
    assert nav.navigate(validate_arguments('navigate_to_pose', NAV))['state'] == 'succeeded'
    task = next(body for path, body in nav.requests if path == '/set_task')
    assert task['speed'] == task['rotateSpeed'] == .2
    assert task['points'][0]['position'] == dict(x=1.2, y=-.4, theta=90)
    assert len(task['points']) == 1
    nav = NavFixture([pose(0), pose(1), pose(3, x=5)])
    with pytest.raises(RuntimeError, match='实测位姿'):
        nav.navigate(validate_arguments('navigate_to_pose', NAV))
    assert nav.dispatched


def test_navigation_does_not_dispatch_when_speed_rejected_or_cancelled():
    nav = NavFixture([pose(0)], speed_ok=False)
    with pytest.raises(RuntimeError, match='speed'):
        nav.navigate(validate_arguments('navigate_to_pose', NAV))
    assert not any(path == '/run_list_task' for path, _ in nav.requests)
    nav = NavFixture([pose(0)], cancelled=lambda: True)
    with pytest.raises(InterruptedError):
        nav.navigate(validate_arguments('navigate_to_pose', NAV))
    assert not nav.dispatched


def test_navigation_checks_both_scheduler_and_navigation_status():
    nav = NavFixture([pose(2), pose(1), {**pose(1), 'global_status': 3}, pose(3)], initial_status=2)
    result = nav.navigate(validate_arguments('navigate_to_pose', NAV))
    assert result['state'] == 'succeeded'
    assert result['pose']['smachStatus'] == 3
    assert result['pose']['taskName'] == 'list_task'
    assert result['pose']['btName'] == result['task']
    assert result['navigation_status'] == 3
    busy = NavFixture([pose(0)], initial_status=1)
    with pytest.raises(RuntimeError, match='未空闲'):
        busy.navigate(validate_arguments('navigate_to_pose', NAV))
    assert not busy.dispatched
    stuck = NavFixture([pose(1)], initial_status=2)
    with pytest.raises(RuntimeError, match='未空闲'):
        stuck.navigate(validate_arguments('navigate_to_pose', NAV))
    assert not stuck.requests


def test_navigation_accepts_cleared_identity_only_after_own_task_completed():
    nav = NavFixture([pose(0), pose(1), {**pose(3), 'taskName': '', 'btName': ''}])
    assert nav.navigate(validate_arguments('navigate_to_pose', NAV))['state'] == 'succeeded'


def test_navigation_rejects_actual_task_replacement_and_global_errors():
    nav = NavFixture([pose(0), pose(1), {**pose(1), 'btName': 'another-controller'}])
    with pytest.raises(RuntimeError, match='其他控制端替换') as error:
        nav.navigate(validate_arguments('navigate_to_pose', NAV))
    assert error.value.feedback[POSE_TOPIC]['btName'] == 'another-controller'
    nav = NavFixture([pose(0), pose(1), {**pose(1), 'global_status': 7}])
    with pytest.raises(RuntimeError, match='异常状态: 7'):
        nav.navigate(validate_arguments('navigate_to_pose', NAV))


def test_navigation_cancellation_ends_scheduler_despite_turn_cancellation():
    import json
    messages = deque()
    class Socket:
        def __init__(self):
            self.sent = []
        def send(self, message):
            message = json.loads(message)
            self.sent.append(message)
            if message['op'] == 'call_service':
                messages.extend([
                    {'op': 'service_response', 'id': message['id'], 'result': True, 'values': {'success': True}},
                    {'op': 'publish', 'topic': STATUS_TOPIC, 'msg': {'status': 2}},
                    {'op': 'publish', 'topic': POSE_TOPIC, 'msg': pose(1)},
                    {'op': 'publish', 'topic': POSE_TOPIC, 'msg': pose(2)},
                ])
        def settimeout(self, timeout):
            pass
        def recv(self):
            return json.dumps(messages.popleft())
    nav = Navigation({}, lambda: True)
    nav.ws = Socket()
    nav.dispatched = True
    result = nav.stop()
    assert result['controller_acknowledged'] is True
    assert result['physical_stop_confirmed'] is False
    assert result['scheduler_status']['smachStatus'] == 2
    assert not nav.dispatched
    commands = [m for m in nav.ws.sent if m['op'] == 'call_service']
    assert len(commands) == 1
    assert commands[0]['service'] == '/agx_smach_operate'
    assert commands[0]['args']['operate'] == 4
    assert not any(m['op'] == 'unsubscribe' for m in nav.ws.sent)


def test_lift_cancellation_prevents_even_configuration_writes():
    lift = LiftController.__new__(LiftController)
    lift._io_lock = threading.RLock()
    lift._operation_lock = threading.Lock()
    lift._cancel = threading.Event()
    lift._external_cancelled = lambda: True
    writes = []
    lift._write = lambda *args: writes.append(args)
    with pytest.raises(LiftError, match='停止'):
        lift._configure()
    assert writes == []


@pytest.mark.parametrize('home', [False, True])
def test_lift_register_sequence_fixes_speed_and_homes_only_on_request(home):
    lift = LiftController.__new__(LiftController)
    lift._io_lock = threading.RLock()
    lift._operation_lock = threading.Lock()
    lift._cancel = threading.Event()
    lift._external_cancelled = lambda: False
    writes, positions = [], []
    lift._write = lambda *args: writes.append(args)
    lift._exchange = lambda payload: positions.append(payload)
    start = dict(height=100, speed_rpm=0, fault_bits=0, current=0, down_limit=False, up_limit=False)
    final = {**start, 'height': 0 if home else 220, 'down_limit': home}
    states = chain([start, start], repeat(final))
    lift.status = lambda: next(states)
    result = lift.reset(timeout=1) if home else lift.set_height(220, timeout=1)
    assert result == final
    assert (0x1D, round(400*8192/3000)) in writes
    assert [value for register, value in writes if register == 0x4C] == ([0, 0, 0] if home else [])
    assert bool(positions) is not home


def settling_lift(states):
    lift = LiftController.__new__(LiftController)
    lift._io_lock = threading.RLock()
    lift._operation_lock = threading.Lock()
    lift._cancel = threading.Event()
    lift._external_cancelled = lambda: False
    lift.writes = []
    lift._write = lambda *args: lift.writes.append(args)
    lift._exchange = lambda data: lift.writes.append(('target', data))
    samples = iter(states)
    lift.status = lambda: next(samples)
    return lift


def lift_sample(height=220, speed=0):
    return dict(height=height, speed_rpm=speed, current=0, fault_bits=0, down_limit=False, up_limit=False)


def test_lift_waits_for_deceleration_without_resending_target():
    target = lift_sample()
    lift = settling_lift([lift_sample(100), lift_sample(100), lift_sample(219, 80),
                          lift_sample(219.5, 40), target, lift_sample(220, 1), target, target])
    assert lift.set_height(220, timeout=1) == target
    assert len([item for item in lift.writes if item[0] == 'target']) == 1


def test_lift_rejects_stopped_position_outside_tolerance():
    wrong = lift_sample(225)
    lift = settling_lift(chain([lift_sample(100), lift_sample(219, 20)], repeat(wrong)))
    with pytest.raises(LiftError, match='高度未达到') as error:
        lift.set_height(220, timeout=1)
    assert error.value.feedback == wrong
    assert not any(item[0] == 'target' for item in lift.writes)


def test_lift_stop_waits_despite_cancellation_and_retains_timeout_feedback(monkeypatch):
    import steward.ego.real.drivers.lift as module
    target = lift_sample()
    lift = settling_lift([lift_sample(speed=40), target, target])
    lift._external_cancelled = lambda: True
    assert lift.stop() == target
    assert lift._cancel.is_set()
    assert lift.writes == [(0x06, 0), (0x4D, 0)]
    moving = lift_sample(speed=40)
    lift = settling_lift(repeat(moving))
    clock = iter([0., 0., .01, .02, .03, 1.])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    with pytest.raises(LiftError, match='停稳反馈超时') as error:
        lift.stop(timeout=.5)
    assert error.value.feedback == moving


@pytest.mark.parametrize('stop_confirmed', [True, False])
def test_failed_lift_keeps_position_feedback_separate_from_stop_outcome(stop_confirmed):
    measured = lift_sample(225, 0 if stop_confirmed else 40)
    class Lift:
        def __init__(self, *args, **kwargs):
            pass
        def set_height(self, height, timeout):
            raise LiftError('fixture position failure', feedback=measured)
        def stop(self):
            if not stop_confirmed:
                raise LiftError('fixture stop timeout', feedback=measured)
            return measured
        def close(self):
            pass
    motion = Motion(config(), lambda *args: None, lift_factory=Lift)
    try:
        motion.begin()
        result = motion.execute('lift_move', dict(action='height', height_mm=220, timeout_s=5))
        assert result['state'] == 'failed'
        assert result['physical_result'] == 'unknown'
        assert result['feedback'] == result['stop']['feedback'] == measured
        assert result['stop']['physical_stop_confirmed'] is stop_confirmed
        assert motion.state()['fault'] == result
    finally:
        motion.close()


def fault_clear_request(motion, **changes):
    return {'fault_id': motion.state()['fault_id'], 'operator_confirmed_stopped': True,
            'reason': '现场检查，操作员确认设备已停稳', **changes}


@pytest.mark.parametrize('changes', [
    {'fault_id': ''}, {'fault_id': 1}, {'operator_confirmed_stopped': False},
    {'operator_confirmed_stopped': 'true'}, {'operator_confirmed_stopped': 1},
    {'reason': ''}, {'reason': ' '}, {'reason': 'x' * 1001}, {'extra': True},
])
def test_clear_fault_rejects_invalid_acknowledgement(changes):
    motion = Motion(config(), lambda *args: None)
    try:
        with motion.lock:
            motion._latch_fault({'state': 'failed', 'error': 'fixture'})
        before = motion.state()
        with pytest.raises(HttpError) as error:
            motion.clear_fault(fault_clear_request(motion, **changes))
        assert error.value.status == 400
        assert motion.state() == before
    finally:
        motion.close()


def test_clear_fault_is_authenticated_no_hardware_and_retains_failure_evidence():
    def forbidden(*args, **kwargs):
        raise AssertionError('clearing a software latch must not open a device')
    events = []
    motion = Motion(config(), lambda *args: events.append(args), enabled=False,
                    navigation_factory=forbidden, lift_factory=forbidden)
    try:
        failed = {'state': 'failed', 'error': 'fixture', 'physical_result': 'unknown'}
        with motion.lock:
            motion.last_result = failed
            motion._latch_fault(failed)
        args = fault_clear_request(motion)
        with pytest.raises(HttpError) as error:
            call('POST', motion.http.url+'/clear-fault', {'token': 'wrong', 'arguments': args})
        assert error.value.status == 403
        assert motion.state()['fault'] == failed
        motion.begin()
        motion.stop()
        result = call('POST', motion.http.url+'/clear-fault', {'token': motion.token, 'arguments': args})
        assert result['cleared'] is True
        assert result['motion_admitted'] is False
        assert result['record']['previous_fault'] == failed
        assert result['record']['confirmation_source'] == 'operator'
        state = motion.state()
        assert state['fault'] is state['fault_id'] is None
        assert state['last_result'] == failed
        assert state['last_fault_clear'] == result['record']
        assert motion.cancel.is_set() and not motion.accepting and not state['motion_enabled']
        assert len(events) == 1
        assert motion.clear_fault(args)['cleared'] is False
        assert motion.state()['last_fault_clear'] == result['record']
    finally:
        motion.close()


def test_clear_fault_rejects_stale_identity_busy_and_closed_runtime():
    motion = Motion(config(), lambda *args: None)
    try:
        with motion.lock:
            motion._latch_fault({'state': 'failed', 'error': 'old'})
            old = fault_clear_request(motion)
            motion._latch_fault({'state': 'failed', 'error': 'new'})
        with pytest.raises(HttpError, match='编号已变化'):
            motion.clear_fault(old)
        current = fault_clear_request(motion)
        motion.done.clear()
        with pytest.raises(HttpError, match='尚未结束'):
            motion.clear_fault(current)
        motion.done.set()
        motion.active = {'tool': 'fixture'}
        with pytest.raises(HttpError, match='尚未结束'):
            motion.clear_fault(current)
        motion.active = None
        motion.closed = True
        with pytest.raises(HttpError, match='正在关闭'):
            motion.clear_fault(current)
        assert motion.state()['fault']['error'] == 'new'
    finally:
        motion.active = None
        motion.done.set()
        motion.close()


def test_clear_fault_allows_a_new_explicit_action_but_never_replays_one():
    calls = []
    class Lift:
        def __init__(self, *args, **kwargs):
            calls.append('open')
        def set_height(self, height, timeout):
            calls.append(height)
            return lift_sample(height)
        def close(self):
            calls.append('close')
    motion = Motion(config(), lambda *args: None, lift_factory=Lift)
    try:
        with motion.lock:
            motion._latch_fault({'state': 'failed', 'error': 'fixture'})
        motion.begin()
        args = dict(action='height', height_mm=220, timeout_s=5)
        with pytest.raises(HttpError):
            motion.execute('lift_move', args)
        assert motion.clear_fault(fault_clear_request(motion))['motion_admitted'] is True
        assert calls == []
        assert motion.execute('lift_move', args)['state'] == 'succeeded'
        assert calls == ['open', 220, 'close']
    finally:
        motion.close()
