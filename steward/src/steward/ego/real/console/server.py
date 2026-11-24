"""Console HTTP routes for observation, operator turns and cancellation."""
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
        if not isinstance(body, dict) or not isinstance(body.get('text'), str) or len(body['text']) > 32000:
            raise HttpError(400, '消息必须是最多 32000 字符的文本')
        return console.chat(body['text'])

    def context(query, body):
        if not isinstance(body, dict) or type(body.get('generation')) is not int:
            raise HttpError(400, '保存上下文需要当前配置 generation')
        return console.set_context(body.get('text'), body.get('previous'), body['generation'])

    def new_session(query, body):
        if not isinstance(body, dict) or type(body.get('generation')) is not int:
            raise HttpError(400, '新建会话需要当前配置 generation')
        return console.new_session(body['generation'])

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
        ('GET', '/api/skill'): lambda q, b: console.skill_detail(q.get('name', ''), number(q, 'generation')),
        ('POST', '/api/connect'): load,
        ('POST', '/api/disconnect'): lambda q, b: console.unload(),
        ('GET', '/api/camera/frame'): frame,
        ('POST', '/api/chat'): chat,
        ('POST', '/api/chat/stop'): lambda q, b: console.stop_agent(),
        ('POST', '/api/chat/new'): new_session,
        ('POST', '/api/context'): context,
    }, host=host, port=port, name='real-console')
