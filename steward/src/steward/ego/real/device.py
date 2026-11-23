"""In-process device seam. The fake backend never imports the hardware SDK."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Operation:
    description: str
    prompt: str
    max_chunks: int
    version: str


@dataclass(frozen=True)
class Capture:
    png: bytes
    captured_at: float     # Unix seconds from acquisition for cross-process evidence.
    monotonic: float       # Same instant on the ER clock for local freshness checks.
    gripper: str


class Device(Protocol):
    """Readiness, send and stop/hold are bounded local calls under the actuator lock.

    Camera capture runs independently and reports the acquisition clock. True from
    stop_and_hold must mean the driver's queue is fenced and hardware confirmed hold.
    """
    robot: str
    camera: str
    mode: str

    def ready(self) -> bool: ...
    def capture(self) -> Capture: ...
    def send(self, action: object) -> None: ...
    def stop_and_hold(self) -> bool: ...


# Policies may block on inference. Their only actuator capability is this revocable callback.
Executor = Callable[[Operation, Callable[[object], None], threading.Event], str]


class FakeDevice:
    mode = "fake"
    robot = "fixture-piper"
    camera = "fixture-head"

    def __init__(self):
        self.commands: list[object] = []
        self.at_station = True
        self.confirm_hold = True
        self.png = Path(__file__).with_name("fixture-head.png").read_bytes()

    def ready(self) -> bool:
        return self.at_station

    def capture(self) -> Capture:
        return Capture(self.png, time.time(), time.monotonic(), "unknown")

    def send(self, action: object) -> None:
        self.commands.append(action)

    def stop_and_hold(self) -> bool:
        return self.confirm_hold


def fake_grasp(operation: Operation, send: Callable[[object], None], cancelled: threading.Event) -> str:
    for index in range(operation.max_chunks):
        if cancelled.wait(0.05):
            return "cancelled"
        send(index)
    return "chunk_budget_exhausted"
