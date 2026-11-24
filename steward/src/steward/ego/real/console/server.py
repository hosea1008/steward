"""Console HTTP routes; no actuator or model invocation endpoints."""
from pathlib import Path

from steward.net import Binary, HttpError, JsonServer
from .service import Console

STATIC = Path(__file__).with_name('static')


def make_server(console: Console, port: int = 8400, host: str = '0.0.0.0') -> JsonServer:
    """Serve assets, connection state and latest camera frames on the configured listen address."""
    def load(query, body):
        if not isinstance(body, dict) or not isinstance(body.get('id'), str):
            raise HttpError(400, '请选择一个真机配置')
        return console.load(body['id'])

    def number(query, key, default=0):
        try:
            value = int(query.get(key, default))
            if value < 0:
                raise ValueError()
            return value
        except (TypeError, ValueError) as exc:
            raise HttpError(400, f'{key} 必须为非负整数') from exc

    def frame(query, body):
        return Binary(console.frame(query.get('id', ''), number(query, 'generation')),
                      'image/jpeg', {'Cache-Control': 'no-store'})

    def chat(query, body):
        raise HttpError(409, 'Agent 尚未接入，消息未发送；当前仅提供连接检查和实时画面')

    def asset(name, media):
        return lambda q, b: Binary((STATIC / name).read_bytes(), media, {'Cache-Control': 'no-store'})

    return JsonServer({
        ('GET', '/console'): asset('index.html', 'text/html; charset=utf-8'),
        ('GET', '/console/'): asset('index.html', 'text/html; charset=utf-8'),
        ('GET', '/console/app.js'): asset('app.js', 'text/javascript; charset=utf-8'),
        ('GET', '/console/style.css'): asset('style.css', 'text/css; charset=utf-8'),
        ('GET', '/console/i18n.js'): asset('i18n.js', 'text/javascript; charset=utf-8'),
        ('GET', '/api/configs'): lambda q, b: {'configs': list(console.configs.values())},
        ('GET', '/api/state'): lambda q, b: console.state(number(q, 'since')),
        ('POST', '/api/connect'): load,
        ('POST', '/api/disconnect'): lambda q, b: console.unload(),
        ('GET', '/api/camera/frame'): frame,
        ('POST', '/api/chat'): chat,
    }, host=host, port=port, name='real-console')
