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
