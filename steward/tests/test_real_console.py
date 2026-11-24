"""Read-only console connections, frame freshness and shutdown behavior."""
from copy import deepcopy
import json
import threading
import time

import pytest

from steward.ego.real.console import service
from steward.ego.real.console.config import DEFAULT_CONFIG_DIR, load_configs, validate
from steward.ego.real.console.server import make_server
from steward.ego.real.console.service import Console
from steward.net import Binary, HttpError, JsonServer, call


def config():
    return deepcopy(load_configs(DEFAULT_CONFIG_DIR)['agilex-cobot-magic'])


@pytest.mark.parametrize('change', [
    lambda c: c.update(refresh_fps=0),
    lambda c: c.update(refresh_fps=True),
    lambda c: c.update(stale_after_s=c['health_interval_s']),
    lambda c: c['cameras'][0].update(snapshot_url='file:///etc/passwd'),
    lambda c: c['cameras'][0].update(snapshot_url='http://user:secret@localhost/frame'),
    lambda c: c['arms'][0].update(device='../can0'),
    lambda c: c['cameras'].append(deepcopy(c['cameras'][0])),
    lambda c: c.update(cameras=[]),
])
def test_invalid_deployment_fails_before_connections(change):
    value = config()
    change(value)
    with pytest.raises(ValueError):
        validate(value)


def test_unknown_model_and_duplicate_profile_fail_at_launch(tmp_path):
    value = config()
    value['model'] = 'unregistered'
    with pytest.raises(ValueError, match='适配器'):
        Console({value['id']: value})
    for name in ('a.json', 'b.json'):
        (tmp_path / name).write_text(json.dumps(config()))
    with pytest.raises(ValueError, match='重复'):
        load_configs(tmp_path)


def test_unload_waits_for_boot_and_no_reader_survives(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def boot(config, emit):
        entered.set()
        assert release.wait(10)
        return []
    monkeypatch.setitem(service.BOOT_ADAPTERS, 'agilex-cobot-magic', boot)
    c = Console({'agilex-cobot-magic': config()})
    stopped = threading.Event()
    closer = threading.Thread(target=lambda: (c.unload(), stopped.set()))
    try:
        c.load('agilex-cobot-magic')
        assert entered.wait(10)
        with pytest.raises(HttpError, match='先断开'):
            c.load('agilex-cobot-magic')
        closer.start()
        assert c.cancel.wait(10)
        assert not stopped.is_set()
        release.set()
        closer.join(10)
        assert not closer.is_alive()
        assert c.state()['phase'] == 'idle'
        assert c.worker is None and not c.readers and not c.frames
    finally:
        release.set()
        if closer.ident:
            closer.join(10)
        c.close()
    with pytest.raises(HttpError, match='关闭'):
        c.load('agilex-cobot-magic')


def test_frame_requires_health_freshness_and_matching_generation():
    c = Console({'agilex-cobot-magic': config()})
    try:
        c.config = config()
        c.phase = 'connected'
        c.frames['head'] = (b'jpeg', time.monotonic(), time.time())
        c.health_at = time.monotonic()
        c.camera_health = {'head': True}
        assert c.frame('head', 0) == b'jpeg'
        with pytest.raises(HttpError, match='切换'):
            c.frame('head', 1)
        c.camera_health['head'] = False
        with pytest.raises(HttpError, match='实时'):
            c.frame('head', 0)
        c.camera_health['head'] = True
        c.health_at -= 60
        assert not c.state()['cameras'][0]['live']
        with pytest.raises(HttpError):
            c.frame('head', 0)
        c.health_at = time.monotonic()
        c.frames['head'] = (b'jpeg', time.monotonic() - 60, time.time() - 60)
        with pytest.raises(HttpError):
            c.frame('head', 0)
    finally:
        c.close()


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(.01)
    raise AssertionError('observable state did not arrive')


def test_http_connect_live_frames_health_loss_and_disconnect(monkeypatch):
    from urllib.request import urlopen
    healthy = threading.Event()
    healthy.set()
    jpeg = b'\xff\xd8fixture\xff\xd9'
    source = JsonServer({
        ('GET', '/status'): lambda q, b: {'topics': {k: healthy.is_set() for k in ('f', 'l', 'r')}},
        ('GET', '/snapshot'): lambda q, b: Binary(jpeg, 'image/jpeg'),
        ('GET', '/ego/summary'): lambda q, b: {'mode': 'fake', 'ready': True},
    }, port=0).start()
    c = http = None
    try:
        value = config()
        value.update(camera_status_url=source.url+'/status', runtime_url=source.url, health_interval_s=.2)
        for camera in value['cameras']:
            camera['snapshot_url'] = source.url+'/snapshot'
        monkeypatch.setitem(service.BOOT_ADAPTERS, value['model'], lambda cfg, emit: [])
        c = Console({value['id']: value})
        http = make_server(c, 0, host='127.0.0.1').start()
        with urlopen(http.url+'/console') as response:
            assert b'/console/app.js' in response.read()
        assert call('GET', http.url+'/api/state')['phase'] == 'idle'
        result = call('POST', http.url+'/api/connect', {'id': value['id']})
        wait_for(lambda: all(camera['live'] for camera in c.state()['cameras']) and len(c.state()['cameras']) == 3)
        with urlopen(http.url+f"/api/camera/frame?id=head&generation={result['generation']}") as response:
            assert response.read() == jpeg
            assert response.headers['Cache-Control'] == 'no-store'
        wait_for(lambda: c.state()['runtime'] is not None)
        assert c.state()['runtime']['mode'] == 'fake'
        assert not c.state()['agent']['connected']
        with pytest.raises(HttpError, match='尚未接入') as rejected:
            call('POST', http.url+'/api/chat', {'message': 'navigate'})
        assert rejected.value.status == 409
        healthy.clear()
        wait_for(lambda: not any(camera['live'] for camera in c.state()['cameras']))
        with pytest.raises(HttpError, match='实时') as offline:
            call('GET', http.url+f"/api/camera/frame?id=head&generation={result['generation']}")
        assert offline.value.status == 503
        call('POST', http.url+'/api/disconnect', {})
        assert c.state()['phase'] == 'idle'
        assert not c.readers
    finally:
        if http:
            http.stop()
        if c:
            c.close()
        source.stop()
