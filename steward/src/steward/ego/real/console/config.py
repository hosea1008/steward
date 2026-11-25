"""Load trusted local robot configurations; request bodies select IDs, never file paths."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

DEFAULT_CONFIG_DIR = Path(__file__).with_name('configs')


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} 必须是非空字符串')
    return value


def _identifier(value, field):
    value = _text(value, field)
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', value):
        raise ValueError(f'{field} 只能包含字母、数字、下划线或连字符')
    return value


def _url(value, field, schemes=('http', 'https')):
    value = _text(value, field)
    parsed = urlsplit(value)
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f'{field} 地址无效，不允许在地址中保存凭据')
    _ = parsed.port
    return value


def validate(value: dict) -> dict:
    """Validate deployment fields before creating any network connections."""
    if not isinstance(value, dict):
        raise ValueError('配置必须为 JSON 对象')
    for field in ('id', 'model'):
        _identifier(value.get(field), field)
    for field in ('name', 'description'):
        _text(value.get(field), field)
    for field in ('camera_status_url', 'runtime_url'):
        _url(value.get(field), field)
    cameras = value.get('cameras')
    if not isinstance(cameras, list) or not cameras:
        raise ValueError('至少配置一路相机')
    seen = set()
    for camera in cameras:
        if not isinstance(camera, dict):
            raise ValueError('相机配置必须为对象')
        key = _identifier(camera.get('id'), 'camera.id')
        if key in seen:
            raise ValueError('相机 ID 重复')
        seen.add(key)
        for field in ('name', 'topic', 'status_key'):
            _text(camera.get(field), f'camera.{field}')
        for field in ('stream_url', 'snapshot_url'):
            _url(camera.get(field), f'camera.{field}')
    arms = value.get('arms')
    if not isinstance(arms, list) or not arms:
        raise ValueError('至少配置一个机械臂设备')
    for arm in arms:
        if not isinstance(arm, dict):
            raise ValueError('机械臂配置必须为对象')
        for field in ('id', 'device'):
            _identifier(arm.get(field), f'arm.{field}')
        _text(arm.get('name'), 'arm.name')
    nav = value.get('navigation')
    if not isinstance(nav, dict):
        raise ValueError('navigation 必须为对象')
    _url(nav.get('http_url'), 'navigation.http_url')
    _url(nav.get('websocket_url'), 'navigation.websocket_url', ('ws', 'wss'))
    _text(nav.get('map'), 'navigation.map')
    _text(nav.get('username'), 'navigation.username')
    _identifier(nav.get('password_env'), 'navigation.password_env')
    lift = value.get('lift')
    if not isinstance(lift, dict):
        raise ValueError('lift 必须为对象')
    if not _text(lift.get('port'), 'lift.port').startswith('/dev/'):
        raise ValueError('lift.port 必须是 /dev/ 下的设备地址')
    if type(lift.get('motor_id')) is not int or not 1 <= lift['motor_id'] <= 247:
        raise ValueError('lift.motor_id 必须在 1–247 之间')
    if type(lift.get('serial_timeout_s')) not in (int, float) or not .05 <= lift['serial_timeout_s'] <= 1:
        raise ValueError('lift.serial_timeout_s 必须在 0.05–1 之间')
    wam = value.get('wam')
    if not isinstance(wam, dict) or wam.get('adapter') != 'cosmos-piper-v2':
        raise ValueError('wam.adapter 必须为 cosmos-piper-v2')
    for field in ('host', 'python', 'cosmos_repo', 'piper_client_dir', 'log_dir'):
        _text(wam.get(field), 'wam.' + field)
    for field, high in [('port', 65535), ('camera_width', 4096), ('camera_height', 4096), ('camera_fps', 60)]:
        if type(wam.get(field)) is not int or not 1 <= wam[field] <= high:
            raise ValueError('wam.' + field + ' 必须为有效正整数')
    if not isinstance(wam.get('python_paths', []), list) or any(
            not isinstance(path, str) or not path for path in wam.get('python_paths', [])):
        raise ValueError('wam.python_paths 必须是路径列表')
    if not isinstance(wam.get('environment', {}), dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in wam.get('environment', {}).items()):
        raise ValueError('wam.environment 必须是字符串字典')
    devices = {arm['id']: arm['device'] for arm in arms}
    if not {'left', 'right'} <= devices.keys() or devices['left'] == devices['right']:
        raise ValueError('WAM 和双臂归零需要不同的 left/right CAN 设备')
    if not {'head', 'left_wrist', 'right_wrist'} <= seen:
        raise ValueError('WAM 需要 head、left_wrist 和 right_wrist 相机')
    for field, low, high in [('refresh_fps', 1, 30), ('health_interval_s', .2, 60),
                             ('request_timeout_s', .1, 10), ('stale_after_s', 1, 120)]:
        number = value.get(field)
        if type(number) not in (int, float) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f'{field} 必须在 {low}–{high} 之间')
    if value['stale_after_s'] <= value['health_interval_s']:
        raise ValueError('stale_after_s 必须大于 health_interval_s')
    return value


def load_configs(directory: Path) -> dict[str, dict]:
    """Read all JSON profiles, rejecting invalid files and duplicate IDs at launch."""
    result = {}
    for path in sorted(directory.glob('*.json')):
        try:
            config = validate(json.loads(path.read_text(encoding='utf-8')))
            if config['id'] in result:
                raise ValueError('配置 ID 重复')
            result[config['id']] = config
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f'{path.name}: {exc}') from exc
    if not result:
        raise ValueError(f'没有找到真机配置: {directory}')
    return result
