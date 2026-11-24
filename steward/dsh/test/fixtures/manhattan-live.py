"""Real-model behavior checks against a fake HTTP robot, with no device drivers imported."""
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from steward.net import JsonServer
from steward.ego.real.console.chat import RealChat

home = Path(sys.argv[1])
cases = [
    ('positive-y', (4, 5, 30), [(4, 2, 90), (4, 5, 30)], None),
    ('negative-y', (-2, -3, -45), [(-2, 2, -90), (-2, -3, -45)], None),
    ('same-x', (1, 5, 90), [(1, 5, 90)], None),
    ('same-position', (1, 2, -90), [(1, 2, -90)], None),
    ('first-leg-fails', (4, 5, 30), [(4, 2, 90)], 'first-leg'),
    ('localization-lost', (4, 5, 30), [], 'localization'),
    ('plan-only', (4, 5, 30), [], 'plan'),
]
results = []
for name, target, expected, failure in cases:
    moves, requests = [], []
    def reply(path, body):
        assert body['token'] == 'fixture-only'
        requests.append(path)
        if path in ('/state', '/stop'):
            return {'mode': 'fake', 'motion_enabled': True, 'active': None, 'fault': None,
                    'map_name': 'fixture-map'}
        if path == '/navigation-state':
            return {'state': 'succeeded', 'pose_valid': failure != 'localization',
                    'location_lost': failure == 'localization', 'map_name': 'fixture-map',
                    'pose': {'x': 1, 'y': 2, 'heading_deg': 0}, 'scheduler_status': 0,
                    'navigation_status': {'status': 0, 'map_name': 'fixture-map'}}
        if path == '/execute':
            assert body['tool'] == 'navigate_to_pose', body
            args = body['arguments']
            assert args['map_name'] == 'fixture-map'
            moves.append((args['x'], args['y'], args['heading_deg']))
            if failure == 'first-leg':
                return {'state': 'failed', 'error': 'fixture blocked waypoint', 'physical_result': 'unknown'}
            return {'state': 'succeeded', 'pose': {'x': args['x'], 'y': args['y'], 'rotation': args['heading_deg']},
                    'distance_error_m': 0, 'heading_error_deg': 0}
        raise AssertionError(f'unexpected endpoint: {path}')
    server = JsonServer({('POST', path): (lambda q, b, path=path: reply(path, b))
                         for path in ['/state', '/navigation-state', '/execute', '/stop', '/clear-fault']}).start()
    motion = SimpleNamespace(http=SimpleNamespace(url=server.url), token='fixture-only',
                             begin=lambda: None, stop=lambda: None)
    chat = RealChat({'id': 'fixture', 'name': 'Fake Manhattan robot'}, motion, home, home/'absent.env')
    try:
        text = (f'这是已授权的模拟设备验收。使用 manhattan-navigation skill，地图 fixture-map，'
                f'目标 x={target[0]}、y={target[1]}、heading_deg={target[2]}。'
                '使用 skill 的默认超时和容差，不需要再次确认。')
        if failure == 'plan':
            text += '本次仅规划，允许只读查询，禁止移动。'
        chat.send('robot', text)
        deadline = time.monotonic() + 55
        while chat.state()['agents']['robot']['busy']:
            if time.monotonic() > deadline:
                raise TimeoutError(name)
            time.sleep(.05)
        transcript = chat.state()['messages']
        errors = [m for m in transcript if m['role'] == 'error']
        assert not errors, (name, errors)
        calls = [m for m in transcript if m['role'] == 'tool']
        skills = [m for m in calls if m['tool'] == 'skill']
        assert len(skills) == 1 and skills[0]['input'] == {'name': 'manhattan-navigation'}, (name, calls)
        # A cached ego_state read may precede loading; task-specific queries and
        # movement must follow the full skill instructions, not only its summary.
        skill_index = calls.index(skills[0])
        assert all(i > skill_index for i, m in enumerate(calls) if m['tool'] in ('get_navigation_state', 'navigate_to_pose'))
        assert '/navigation-state' in requests, (name, requests)
        assert moves == expected, (name, moves, expected, transcript)
        assert '/clear-fault' not in requests
        results.append({'case': name, 'passed': True, 'moves': moves})
        print(json.dumps(results[-1]), file=sys.stderr, flush=True)
    finally:
        chat.close()
        server.stop()
print(json.dumps(results))
