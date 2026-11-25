"""Virtual joystick tests use an injected clock and socket; never contact a robot."""
import json

import pytest
from websocket import WebSocketTimeoutException

from steward.ego.real.console.config import load_configs, DEFAULT_CONFIG_DIR
from steward.ego.real.drivers import timed_move
from steward.ego.real.motion import Motion, validate_arguments
from steward.net import HttpError


class Clock:
    def __init__(self): self.now = 0.
    def monotonic(self): return self.now
    def time(self): return 1000 + self.now


class Socket:
    def __init__(self, clock, *, pong=True, fail_move=False, fail_stop=False):
        self.clock, self.pong = clock, pong
        self.fail_move, self.fail_stop = fail_move, fail_stop
        self.messages, self.pending = [], []
        self.closed = False
    def settimeout(self, timeout): self.timeout = timeout
    def send(self, raw):
        m = json.loads(raw)
        self.messages.append((self.clock.now, m))
        if m['op'] == 'ping' and self.pong:
            self.pending.append(json.dumps({'op': 'pong'}))
        if m['op'] == 'publish':
            moving = any(m['msg']['linear'].values()) or any(m['msg']['angular'].values())
            if moving and self.fail_move: raise OSError('partial send failure')
            if not moving and self.fail_stop: raise OSError('stop write failed')
    def recv(self):
        if self.pending: return self.pending.pop(0)
        self.clock.now += self.timeout
        raise WebSocketTimeoutException()
    def close(self, **kwargs): self.closed = True


@pytest.fixture
def fake(monkeypatch):
    clock = Clock(); socket = Socket(clock)
    monkeypatch.setattr(timed_move, 'time', clock)
    monkeypatch.setattr('websocket.create_connection', lambda *a, **k: socket)
    return clock, socket


def args(**patch):
    return {'direction': 'forward', 'duration_s': .35, **patch}


@pytest.mark.parametrize('patch', [dict(speed=.50001),dict(speed=0),dict(speed=-.2),dict(speed=True),
    dict(speed=float('nan')),dict(speed=float('inf')),dict(direction='invalid'),dict(direction=[]),
    dict(duration_s=.09),dict(duration_s=60.01),dict(duration_s=True),dict(duration_s=float('nan')),
    dict(linear_x=.3),dict(websocket_url='ws://other')])
def test_validation_rejects_before_transport(patch):
    with pytest.raises(HttpError): validate_arguments('move_for_duration', args(**patch))


def test_defaults_and_cap():
    assert validate_arguments('move_for_duration', args())['speed'] == .2
    assert validate_arguments('move_for_duration', args(speed=.5))['speed'] == .5


@pytest.mark.parametrize('direction,axis,expected', [
    ('forward','x',.2),('backward','x',-.2),('shift_left','y',.2),('shift_right','y',-.2),
    ('turn_left','yaw',.2),('turn_right','yaw',-.2)])
def test_six_body_frame_directions_publish_at_ten_hz_and_stop(fake,direction,axis,expected):
    clock,socket=fake
    driver=timed_move.TimedMove({'websocket_url':'ws://fixture'},lambda:False)
    result=driver.move(timed_move.resolve(args(direction=direction)))
    driver.close()
    publishes=[(at,m['msg']) for at,m in socket.messages if m['op']=='publish']
    assert len(publishes)==5
    for at,msg in publishes[:-1]:
        actual={'x':msg['linear']['x'],'y':msg['linear']['y'],'yaw':msg['angular']['z']}
        assert actual=={k:expected if k==axis else 0 for k in ('x','y','yaw')}
    assert [at for at,_ in publishes[:-1]] == pytest.approx([0,.1,.2,.3], abs=.0011)
    assert publishes[-1][0] == pytest.approx(.35)
    assert not any(publishes[-1][1]['linear'].values()) and not any(publishes[-1][1]['angular'].values())
    assert result['state']=='succeeded' and result['command_stream_complete']
    assert result['stop']['write_acknowledged'] and not result['stop']['physical_stop_confirmed']
    assert socket.closed


def runtime():
    return Motion(load_configs(DEFAULT_CONFIG_DIR)['agilex-cobot-magic'],lambda *a:None)


def test_cancel_sends_zero_and_latches_fault(fake):
    clock,socket=fake
    m=runtime();m.begin()
    try:
        result=m.execute('move_for_duration',args(duration_s=5),lambda:clock.now>=.15)
        assert result['state']=='failed'
        assert result['stop']['write_acknowledged']
        assert socket.messages[-1][1]['msg']['linear']['x']==0
        assert clock.now<.3 and socket.closed
        assert m.state()['fault_id']
        with pytest.raises(HttpError): m.execute('lift_move',dict(action='home',timeout_s=1))
    finally:m.close()


def test_partial_first_send_still_attempts_zero(fake):
    _,socket=fake;socket.fail_move=True
    m=runtime();m.begin()
    try:
        result=m.execute('move_for_duration',args())
        assert result['state']=='failed' and result['feedback']['motion_started']
        assert result['stop']['write_acknowledged']
        assert socket.messages[-1][1]['msg']['linear']['x']==0 and socket.closed
    finally:m.close()


def test_failed_stop_is_not_reported_as_success(fake):
    _,socket=fake;socket.fail_stop=True
    m=runtime();m.begin()
    try:
        result=m.execute('move_for_duration',args())
        assert result['state']=='failed' and not result['stop']['physical_stop_confirmed']
        assert 'stop write failed' in result['stop']['error']
        assert m.state()['fault'] and socket.closed
    finally:m.close()


def test_no_handshake_means_no_movement(fake):
    clock,socket=fake;socket.pong=False
    m=runtime();m.begin()
    try:
        result=m.execute('move_for_duration',args())
        assert result['state']=='failed' and not result['stop']['requested']
        assert not any(msg['op']=='publish' for _,msg in socket.messages)
        assert clock.now>=2 and socket.closed
    finally:m.close()


def test_missing_heartbeat_during_move_stops(fake):
    clock,socket=fake
    send=socket.send
    def first_pong_only(raw):
        send(raw)
        socket.pong=False
    socket.send=first_pong_only
    m=runtime();m.begin()
    try:
        result=m.execute('move_for_duration',args(duration_s=10))
        assert result['state']=='failed' and result['stop']['write_acknowledged']
        assert 3 <= clock.now < 3.1 and socket.closed
        assert len([msg for _,msg in socket.messages if msg['op']=='ping'])>=3
    finally:m.close()


def test_no_actuation_never_opens_socket(monkeypatch):
    def forbidden(*a,**k): pytest.fail('must not open device')
    monkeypatch.setattr('websocket.create_connection',forbidden)
    m=Motion(load_configs(DEFAULT_CONFIG_DIR)['agilex-cobot-magic'],lambda *a:None,enabled=False)
    m.begin()
    try:
        with pytest.raises(HttpError): m.execute('move_for_duration',args())
    finally:m.close()


def test_disconnect_keeps_stop_failure_visible(fake):
    clock,socket=fake
    receive=socket.recv
    def disconnect():
        if clock.now >= .15:
            socket.fail_stop=True
            return ''
        return receive()
    socket.recv=disconnect
    m=runtime();m.begin()
    try:
        result=m.execute('move_for_duration',args())
        assert result['state']=='failed' and result['physical_result']=='unknown'
        assert not result['stop']['physical_stop_confirmed']
        assert result['feedback']['commands_sent']>0 and socket.closed
    finally:m.close()


def test_server_error_is_not_ignored(fake):
    _,socket=fake
    send=socket.send
    def reject(raw):
        send(raw)
        if json.loads(raw)['op']=='publish':
            socket.pending.append(json.dumps({'op':'status','level':'error','msg':'rejected'}))
    socket.send=reject
    m=runtime();m.begin()
    try:
        result=m.execute('move_for_duration',args())
        assert result['state']=='failed' and 'rejected' in result['error']
        assert result['stop']['write_acknowledged'] and socket.closed
    finally:m.close()
