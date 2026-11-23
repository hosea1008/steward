"""Single actuator owner. A motion generation fences off late policy output after stop."""
from __future__ import annotations

import base64
import secrets
import threading
import time
import tomllib
from collections import OrderedDict
from pathlib import Path
from types import MappingProxyType

from steward.net import HttpError

from .device import Device, Executor, Operation


def catalog() -> dict[str, Operation]:
    with Path(__file__).with_name("operations.toml").open("rb") as stream:
        return {key: Operation(**values) for key, values in tomllib.load(stream).items()}


class Runtime:
    def __init__(self, device: Device, operations: dict[str, Operation], executors: dict[str, Executor],
                 *, lease_s=15., receipt_s=15., frame_s=30.):
        if operations.keys() != executors.keys() or min(lease_s, receipt_s, frame_s) <= 0:
            raise ValueError("invalid operation registry or time limits")
        self.device = device
        self.operations = MappingProxyType(dict(operations))
        self.executors = MappingProxyType(dict(executors))
        self.lease_s, self.receipt_s, self.frame_s = lease_s, receipt_s, frame_s
        self.lock = threading.RLock()
        self.generation = 0
        self.generation_started = time.monotonic()
        self.active: str | None = None
        self.cancelled: threading.Event | None = None
        self.fault: str | None = None
        self.owner: str | None = None
        self.deadline = 0.
        self.receipts: dict[str, dict] = {}
        self.jobs: OrderedDict[str, dict] = OrderedDict()
        self.frames: OrderedDict[str, tuple] = OrderedDict()
        self.closed = threading.Event()
        self.watcher = threading.Thread(target=self._watch, daemon=True, name="ego-real-lease")
        self.watcher.start()

    def _watch(self):
        while not self.closed.wait(min(.1, self.lease_s / 4)):
            with self.lock:
                self._expire()

    def close(self):
        with self.lock:
            if self.closed.is_set():
                return
            self.closed.set()
            self._end_lease("shutdown")
        self.watcher.join()

    def _expire(self):
        if self.owner and time.monotonic() >= self.deadline:
            self._end_lease("lease_expired")

    def _check(self, owner):
        self._expire()
        if not self.owner or self.owner != owner:
            raise HttpError(403, "control lease is absent or expired")

    def acquire(self):
        with self.lock:
            self._expire()
            if self.owner or self.closed.is_set() or self.fault:
                raise HttpError(409, self.fault or "control unavailable")
            self.owner = secrets.token_urlsafe(24)
            self.deadline = time.monotonic() + self.lease_s
            return {"owner": self.owner, "lease_s": self.lease_s}

    def renew(self, owner):
        with self.lock:
            self._check(owner)
            self.deadline = time.monotonic() + self.lease_s
            return {"lease_s": self.lease_s}

    def release(self, owner):
        with self.lock:
            self._check(owner)
            return self._end_lease("lease_released")

    def _end_lease(self, reason):
        result = self.stop(reason)
        self.owner = None
        self.receipts.clear()
        return result

    def _next_generation(self):
        self.generation += 1
        self.generation_started = time.monotonic()
        return self.generation

    def _finish(self, target_state, reason):
        # The same lock covers generation revocation, bounded driver write, and hold acknowledgement.
        # A future hardware adapter with a blocking/queued write must supply its own bounded fence.
        self._next_generation()
        job = self.jobs.get(self.active)
        self.active = None
        if self.cancelled is not None:
            self.cancelled.set()
            self.cancelled = None
        try:
            held = self.device.stop_and_hold()
        except Exception as exc:
            held = False
            self.fault = f"stop/hold failed: {exc}"
        if not held:
            self.fault = self.fault or "stop/hold unconfirmed"
        if job:
            job.update(state="failed" if self.fault else target_state, reason=reason, hold_confirmed=held,
                       fault=self.fault, message=self.fault or reason)
            job["log"].append(job["message"])
        return {"stopped": job and job["id"], "hold_confirmed": held, "fault": self.fault}

    def stop(self, reason="operator_stop"):
        with self.lock:
            return self._finish("stopped", reason)

    def state(self):
        with self.lock:
            self._expire()
            return {"mode": self.device.mode, "robot": self.device.robot, "active_job": self.active,
                    "generation": self.generation, "fault": self.fault, "ready": not self.fault and self.active is None and self.device.ready(),
                    "operations": {k: {"description": v.description, "version": v.version}
                                   for k, v in self.operations.items()}}

    def observe(self):
        # Capture outside the actuator lock. A frame straddling a transition cannot admit motion.
        with self.lock:
            self._expire()
            generation, job = self.generation, self.active
        frame = self.device.capture()
        with self.lock:
            self._expire()
            if generation != self.generation or frame.monotonic < self.generation_started:
                raise HttpError(409, "frame predates the current motion generation; request another frame")
            key = secrets.token_urlsafe(18)
            info = dict(frame_id=key, mode=self.device.mode, robot=self.device.robot, camera=self.device.camera,
                        captured_at=frame.captured_at, gripper=frame.gripper, job_id=job, generation=generation)
            self.frames[key] = (frame, info)
            if len(self.frames) > 16:
                self.frames.popitem(last=False)
        return self.frame(key)

    def _frame(self, key):
        item = self.frames.get(key)
        if not item or not 0 <= time.monotonic() - item[0].monotonic <= self.frame_s:
            raise HttpError(404, "unknown or stale frame")
        return item

    def frame(self, key):
        with self.lock:
            frame, info = self._frame(key)
        return {**info, "png_base64": base64.b64encode(frame.png).decode("ascii")}

    def _ready(self, frame_id):
        _, info = self._frame(frame_id)
        if self.active or self.fault or not self.device.ready() or info["generation"] != self.generation:
            raise HttpError(409, self.fault or "operation needs an idle, ready workstation and a current frame")

    def prepare(self, owner, operation, frame_id):
        with self.lock:
            self._check(owner)
            if operation not in self.operations:
                raise HttpError(404, "unknown operation")
            self._ready(frame_id)
            key = secrets.token_urlsafe(18)
            self.receipts = {k: r for k, r in self.receipts.items()
                             if r["expires"] > time.monotonic() or r["job"] in self.jobs}
            if len(self.receipts) >= 256:
                raise HttpError(409, "too many outstanding receipts")
            self.receipts[key] = dict(operation=operation, version=self.operations[operation].version,
                                      frame_id=frame_id, expires=time.monotonic() + self.receipt_s, job=None)
            return {"kind": "operation_admission", "receipt": key, "operation": operation,
                    "version": self.operations[operation].version, "frame_id": frame_id, "ttl_s": self.receipt_s}

    def _admission(self, owner, key):
        self._check(owner)
        receipt = self.receipts.get(key)
        if not receipt:
            raise HttpError(404, "unknown operation admission for this lease")
        if receipt["job"] is None:
            if receipt["expires"] <= time.monotonic():
                raise HttpError(409, "operation admission expired")
            self._ready(receipt["frame_id"])
        return receipt

    def review(self, owner, key):
        with self.lock:
            receipt = self._admission(owner, key)
            return {"kind": "operation_admission", "receipt": key, "job": receipt["job"]}

    def run(self, owner, key):
        with self.lock:
            receipt = self._admission(owner, key)
            if receipt["job"]:
                return self.status(receipt["job"])
            operation = receipt["operation"]
            job = secrets.token_urlsafe(18)
            generation = self._next_generation()
            self.active = receipt["job"] = job
            # Preserve the shared Ego client's SkillJob fields; evidence and the latch are real-ER fields.
            self.jobs[job] = dict(id=job, skill=operation, receipt=key, message="running", log=["running"],
                                  operation=operation, version=receipt["version"], frame_id=receipt["frame_id"],
                                  generation=generation, state="running", reason=None, physical_result="unknown",
                                  hold_confirmed=None, fault=None)
            if len(self.jobs) > 128:
                self.jobs.popitem(last=False)
            self.cancelled = threading.Event()
            worker = threading.Thread(target=self._execute, args=(operation, generation, self.cancelled),
                                      name=f"ego-real-{operation}", daemon=True)
            try:
                worker.start()
            except RuntimeError:
                self._finish("failed", "worker_start_failed")
                raise
            return self.status(job)

    def _execute(self, operation, generation, cancelled):
        def send(action):
            with self.lock:
                self._expire()
                if generation != self.generation:
                    raise RuntimeError("action generation revoked")
                self.device.send(action)
        state, reason = "failed", "worker_aborted"
        try:
            reason = self.executors[operation](self.operations[operation], send, cancelled)
            state = "succeeded"
        except KeyboardInterrupt:
            # The deployed WAM call path can signal its local emergency stop this way.
            state, reason = "stopped", "device_interrupt"
        except Exception as exc:
            reason = str(exc)
        finally:
            with self.lock:
                self._expire()
                if generation == self.generation:
                    self._finish(state, reason)

    def status(self, job):
        with self.lock:
            self._expire()
            if job not in self.jobs:
                raise HttpError(404, "unknown or evicted job")
            return {**self.jobs[job], "log": list(self.jobs[job]["log"])}
