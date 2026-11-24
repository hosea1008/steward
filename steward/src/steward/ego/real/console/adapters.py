"""Read-only per-model connection boot. Connection checks never enable or move devices."""
from __future__ import annotations

import json
from pathlib import Path
import socket
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener


def fetch(url: str, timeout: float, limit: int = 4_000_000) -> tuple[bytes, str]:
    """Read a bounded device response directly, without workstation proxy settings."""
    with build_opener(ProxyHandler({})).open(url, timeout=timeout) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError('设备响应超过大小限制')
        return data, response.headers.get_content_type()


def read_json(url: str, timeout: float) -> dict:
    """Require an object response from a device's status endpoint."""
    data, _ = fetch(url, timeout)
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError('设备状态不是 JSON 对象')
    return result


def boot_cobot_magic(config: dict, emit) -> list[dict]:
    """Probe CAN interface presence and navigation ports; return read-only entry points."""
    devices = []
    for arm in config['arms']:
        exists = (Path('/sys/class/net') / arm['device']).exists()
        item = dict(id=arm['id'], name=arm['name'], address=arm['device'],
                    status='available' if exists else 'unavailable',
                    detail='CAN 接口存在；未使能、未验证关节反馈' if exists else '未找到 CAN 接口')
        devices.append(item)
        emit('DEBUG' if exists else 'WARNING', 'device', item['name'] + '：' + item['detail'], item)
    for key, label in [('http_url', '导航 HTTP'), ('websocket_url', '导航 WebSocket')]:
        address = config['navigation'][key]
        parsed = urlsplit(address)
        port = parsed.port or (443 if parsed.scheme in ('https', 'wss') else 80)
        try:
            with socket.create_connection((parsed.hostname, port), timeout=config['request_timeout_s']):
                pass
            status, detail = 'reachable', '端口可达；未登录或发送导航指令'
        except OSError as exc:
            status, detail = 'unavailable', str(exc)
        item = dict(id=key, name=label, address=address, status=status, detail=detail)
        devices.append(item)
        emit('DEBUG' if status == 'reachable' else 'WARNING', 'device', label + '：' + detail, item)
    return devices


# Deployment files name a supported model; they cannot inject executable code.
BOOT_ADAPTERS = {'agilex-cobot-magic': boot_cobot_magic}
