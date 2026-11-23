"""Loopback-only Ego API for the task host, tool provider and local stop command."""
from steward.net import HttpError, JsonServer

from .runtime import Runtime


def server(runtime: Runtime, port: int) -> JsonServer:
    def fields(body, *names):
        if not isinstance(body, dict) or any(not isinstance(body.get(k), str) or not body[k] for k in names):
            raise HttpError(400, "required string fields: " + ", ".join(names))
        return [body[k] for k in names]

    return JsonServer({
        ("POST", "/control/acquire"): lambda q, b: runtime.acquire(),
        ("POST", "/control/renew"): lambda q, b: runtime.renew(*fields(b, "owner")),
        ("POST", "/control/release"): lambda q, b: runtime.release(*fields(b, "owner")),
        ("GET", "/ego/summary"): lambda q, b: runtime.state(),
        ("POST", "/ego/observe"): lambda q, b: runtime.observe(),
        ("GET", "/ego/frame"): lambda q, b: runtime.frame(*fields(q, "frame_id")),
        ("POST", "/ego/prepare"): lambda q, b: runtime.prepare(*fields(b, "owner", "operation", "frame_id")),
        ("POST", "/ego/admission"): lambda q, b: runtime.review(*fields(b, "owner", "receipt")),
        ("POST", "/skill/run"): lambda q, b: runtime.run(*fields(b, "owner", "receipt")),
        ("GET", "/skill/status"): lambda q, b: runtime.status(*fields(q, "job")),
        ("POST", "/skill/stop"): lambda q, b: runtime.stop(),
    }, port=port, name="ego-real")
