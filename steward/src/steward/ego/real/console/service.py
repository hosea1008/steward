"""Console-owned connections, bounded frame cache and diagnostic event history."""
from __future__ import annotations

from collections import deque
import logging
import threading
import time

from steward.net import HttpError
from .adapters import BOOT_ADAPTERS, fetch, read_json


class Console:
    """Own one selected robot connection; unload joins readers before discarding frames."""

    def __init__(self, configs: dict[str, dict], *, agent_factory=None):
        for config in configs.values():
            if config['model'] not in BOOT_ADAPTERS:
                raise ValueError(f"没有该型号的启动适配器: {config['model']}")
        self.configs = configs
        self.agent_factory = agent_factory
        self.agent = None
        self.lock = threading.RLock()
        self.lifecycle = threading.Lock()
        self.events = deque(maxlen=1000)
        self.sequence = 0
        self.generation = 0
        self.config = None
        self.phase = 'idle'
        self.devices = []
        self.frames = {}
        self.camera_health = {}
        self.health_at = 0.
        self.runtime = None
        self.runtime_error = None
        self.cancel = threading.Event()
        self.worker = None
        self.readers = []
        self.closed = False
        self.emit('INFO', 'console', '控制台已启动，请选择真机配置')

    def emit(self, level: str, source: str, message: str, detail=None):
        """Append a diagnostic or connection-call event, never a fabricated model response."""
        with self.lock:
            self.sequence += 1
            event = dict(id=self.sequence, time=time.time(), level=level, source=source,
                         message=message, detail=detail)
            self.events.append(event)
        logging.getLogger(__name__).log(getattr(logging, level), '%s: %s', source, message)

    def load(self, identifier: str):
        """Start a model adapter asynchronously; reject overlapping connection attempts."""
        with self.lifecycle, self.lock:
            if self.closed:
                raise HttpError(409, '控制台正在关闭')
            if self.phase != 'idle':
                raise HttpError(409, '请先断开当前配置，再加载其他配置')
            if identifier not in self.configs:
                raise HttpError(404, '未知的真机配置')
            self.config = self.configs[identifier]
            self.generation += 1
            self.phase = 'connecting'
            self.cancel = threading.Event()
            self.emit('INFO', 'boot', '正在连接 ' + self.config['name'], {'model': self.config['model']})
            self.worker = threading.Thread(target=self._boot, name='real-console-boot', daemon=True)
            self.worker.start()
            return {'generation': self.generation}

    def _boot(self):
        try:
            config = self.config
            devices = BOOT_ADAPTERS[config['model']](config, self.emit)
            with self.lock:
                if self.cancel.is_set():
                    return
                self.devices = devices
                if self.agent_factory:
                    self.agent = self.agent_factory(config, self.emit)
                for camera in config['cameras']:
                    reader = threading.Thread(target=self._camera, args=(camera,),
                                              name='real-camera-' + camera['id'], daemon=True)
                    self.readers.append(reader)
                    reader.start()
                self.phase = 'connected'
            self.emit('INFO', 'boot', '连接入口已建立，正在检查实时画面')
            while not self.cancel.is_set():
                self._health(config)
                self.cancel.wait(config['health_interval_s'])
        except Exception as exc:
            with self.lock:
                self.phase = 'error'
            self.emit('ERROR', 'boot', '启动连接失败，请断开后重试', {'error': str(exc)})
            self.cancel.set()

    def _health(self, config):
        try:
            status = read_json(config['camera_status_url'], config['request_timeout_s'])
            topics = status.get('topics')
            if not isinstance(topics, dict):
                raise ValueError('相机状态缺少 topics')
            health = {c['id']: topics.get(c['status_key']) is True for c in config['cameras']}
            error = None
        except Exception as exc:
            health, error = {c['id']: False for c in config['cameras']}, str(exc)
        with self.lock:
            changed = health != self.camera_health
            self.camera_health, self.health_at = health, time.monotonic()
        if changed:
            self.emit('DEBUG' if all(health.values()) else 'WARNING', 'camera',
                      '相机采集状态已更新', {'receiving': health, 'error': error})
        try:
            runtime = read_json(config['runtime_url'].rstrip('/') + '/ego/summary', config['request_timeout_s'])
            if runtime.get('mode') not in ('real', 'fake'):
                raise ValueError('ER 状态缺少设备模式')
            runtime_error = None
        except Exception as exc:
            runtime, runtime_error = None, str(exc)
        with self.lock:
            changed = (runtime is None) != (self.runtime is None) or runtime_error != self.runtime_error
            self.runtime, self.runtime_error = runtime, runtime_error
        if changed:
            self.emit('DEBUG' if runtime else 'WARNING', 'runtime',
                      'ER 状态已读取' if runtime else 'ER 不可达；控制台画面仍可使用',
                      runtime if runtime else {'error': runtime_error})

    def _camera(self, camera):
        failure = None
        timeout = self.config['request_timeout_s']
        period = 1 / self.config['refresh_fps']
        while not self.cancel.is_set():
            started = time.monotonic()
            try:
                data, media = fetch(camera['snapshot_url'], timeout)
                if media != 'image/jpeg' or not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
                    raise ValueError('快照接口未返回完整 JPEG 图像')
                with self.lock:
                    first = camera['id'] not in self.frames
                    self.frames[camera['id']] = (data, time.monotonic(), time.time())
                if first or failure:
                    self.emit('DEBUG', 'camera', camera['name'] + '快照接收正常', {'bytes': len(data)})
                failure = None
            except Exception as exc:
                error = str(exc)
                if error != failure:
                    self.emit('WARNING', 'camera', camera['name'] + '快照接收失败', {'error': error})
                failure = error
            self.cancel.wait(max(.01, period - (time.monotonic() - started)))

    def frame(self, identifier: str, generation: int) -> bytes:
        """Return only fresh frames with fresh upstream health, never offline placeholders."""
        with self.lock:
            if self.config is None or generation != self.generation:
                raise HttpError(409, '相机配置已切换')
            frame = self.frames.get(identifier)
            now = time.monotonic()
            if (self.phase != 'connected' or not frame or not self.camera_health.get(identifier)
                    or now - frame[1] > self.config['stale_after_s']
                    or now - self.health_at > self.config['stale_after_s']):
                raise HttpError(503, '未收到有效的实时画面')
            return frame[0]

    def state(self, since=0):
        """Describe read-only connectivity separately from agent and motion readiness."""
        with self.lock:
            now = time.monotonic()
            cameras = []
            if self.config:
                for camera in self.config['cameras']:
                    frame = self.frames.get(camera['id'])
                    live = (self.phase == 'connected' and frame is not None and self.camera_health.get(camera['id'], False)
                            and now - frame[1] <= self.config['stale_after_s']
                            and now - self.health_at <= self.config['stale_after_s'])
                    cameras.append({**camera, 'live': live, 'received_at': frame[2] if frame else None,
                                    'age_s': round(now-frame[1], 2) if frame else None})
            return dict(phase=self.phase, generation=self.generation, config=self.config,
                        devices=list(self.devices), cameras=cameras, runtime=self.runtime,
                        runtime_error=self.runtime_error,
                        agent=self.agent.state() if self.agent else {'connected': False, 'reason': 'Agent 未启用或配置尚未加载', 'messages': []},
                        events=[e for e in self.events if e['id'] > since], cursor=self.sequence)

    def chat(self, text):
        """Accept messages only while the selected configuration remains connected."""
        with self.lifecycle, self.lock:
            if self.phase != 'connected' or self.agent is None:
                raise HttpError(409, '请先加载配置并启用 Agent')
            return self.agent.send(text)

    def skill_detail(self, name, generation):
        """Expose only the selected agent's skill snapshot, rejecting stale configuration views."""
        with self.lifecycle, self.lock:
            if self.phase != 'connected' or self.agent is None or generation != self.generation:
                raise HttpError(409, '真机配置已切换或断开，请刷新后再查看 skill')
            return self.agent.chat.skills.detail(name)

    def save_skill(self, name, text, previous, generation):
        """Serialize disk edits against configuration changes and operator turn admission."""
        with self.lifecycle, self.lock:
            if self.phase != 'connected' or self.agent is None or generation != self.generation:
                raise HttpError(409, '真机配置已切换或断开，请刷新后再保存 skill')
            result = self.agent.chat.save_skill(name, text, previous)
            self.emit('INFO', 'skill', '已保存 skill：' + name, {'revision': result['revision']})
            return result

    def stop_agent(self):
        """Cancel the operator turn and return the motion runtime's stop report."""
        with self.lifecycle:
            if self.agent is None:
                raise HttpError(409, 'Agent 未启用')
            return self.agent.stop()

    def new_session(self, generation):
        """Serialize a conversation reset against sends and configuration changes."""
        with self.lifecycle, self.lock:
            if self.phase != 'connected' or self.agent is None or generation != self.generation:
                raise HttpError(409, '真机配置已切换或断开，请刷新后再新建会话')
            return self.agent.new_session()

    def set_context(self, text, previous, generation):
        """Reject edits for a disconnected or replaced robot before persisting task context."""
        with self.lifecycle, self.lock:
            if self.phase != 'connected' or self.agent is None or generation != self.generation:
                raise HttpError(409, '真机配置已切换或断开，请刷新后再保存')
            return self.agent.set_context(text, previous)

    def unload(self):
        """Cancel owned motion and conversation before releasing camera subscriptions."""
        with self.lifecycle:
            self._unload()

    def _unload(self):
        with self.lock:
            self.cancel.set()
            worker = self.worker
            self.phase = 'disconnecting'
        if worker:
            worker.join()
        if self.agent:
            self.agent.close()
            self.agent = None
        for reader in self.readers:
            reader.join()
        with self.lock:
            self.readers.clear()
            self.worker = None
            self.config = None
            self.frames.clear()
            self.camera_health.clear()
            self.devices.clear()
            self.runtime = None
            self.runtime_error = None
            self.phase = 'idle'
            self.generation += 1
        self.emit('INFO', 'console', '已断开控制台连接；本控制台发起的任务已结束')

    def close(self):
        """Reject future loads and wait for all console-owned readers to settle."""
        with self.lifecycle:
            with self.lock:
                self.closed = True
            self._unload()
