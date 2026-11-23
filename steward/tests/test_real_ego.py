"""Loopback tests using a controllable late policy response and a recording device."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time

import pytest

from steward.ego.real.api import server
from steward.ego.real.device import FakeDevice
from steward.ego.real.runtime import Runtime, catalog
from steward.net import HttpError, call


@pytest.fixture
def station():
    device = FakeDevice()
    entered, resume, finished = (threading.Event() for _ in range(3))

    def delayed(op, send, cancelled):
        send("before_inference")
        entered.set()
        try:
            assert resume.wait(4)
            send("after_inference")
            return "chunk_budget_exhausted"
        finally:
            finished.set()

    operations = catalog()
    operations["test_procedure"] = replace(operations["pick_black_box"], max_chunks=1)

    def procedure(op, send, cancelled):
        send("procedure")
        return "procedure_complete"

    runtime = Runtime(device, operations, {"pick_black_box": delayed, "test_procedure": procedure})
    http = server(runtime, 0).start()
    post = lambda path, **body: call("POST", http.url + path, body)
    get = lambda path: call("GET", http.url + path)
    owner = post("/control/acquire")["owner"]

    def prepare(operation="pick_black_box"):
        frame = post("/ego/observe")["frame_id"]
        return post("/ego/prepare", operation=operation, owner=owner, frame_id=frame)["receipt"]

    try:
        yield device, runtime, post, get, owner, prepare, entered, resume, finished, http.url
    finally:
        runtime.close()
        resume.set()
        http.stop()


def test_duplicate_http_run_and_late_action_after_stop(station):
    device, _, post, get, owner, prepare, entered, resume, finished, _ = station
    key = prepare()
    def run():
        return post("/skill/run", receipt=key, owner=owner)["id"]
    with ThreadPoolExecutor(4) as pool:
        ids = list(pool.map(lambda _: run(), range(4)))
    assert len(set(ids)) == 1 and entered.wait(1)
    frame = post("/ego/observe")
    same = get("/ego/frame?frame_id=" + frame["frame_id"])
    assert frame == same and frame["job_id"] == ids[0]
    assert base64.b64decode(frame["png_base64"]).startswith(b"\x89PNG")
    assert post("/skill/stop")["hold_confirmed"]
    # A new generation can run while the old model still waits for its response.
    second = post("/skill/run", owner=owner, receipt=prepare("test_procedure"))
    resume.set()
    assert finished.wait(1)
    deadline = time.monotonic() + 1
    while get("/skill/status?job=" + second["id"])["state"] == "running":
        assert time.monotonic() < deadline
    assert device.commands == ["before_inference", "procedure"]
    stopped = get("/skill/status?job=" + run())
    assert stopped["reason"] == "operator_stop" and stopped["physical_result"] == "unknown"


@pytest.mark.parametrize("cause", ["lease_expired", "lease_released", "unconfirmed_hold"])
def test_revocation_and_fault_latch(station, cause):
    device, runtime, post, get, owner, prepare, entered, resume, finished, _ = station
    key = prepare()
    job = post("/skill/run", owner=owner, receipt=key)["id"]
    assert entered.wait(1)
    if cause == "lease_expired":
        runtime.lease_s = .05
        post("/control/renew", owner=owner)
        # The watchdog must stop it even if nobody polls the API.
        deadline = time.monotonic() + 1
        while True:
            with runtime.lock:
                if runtime.owner is None:
                    break
            assert time.monotonic() < deadline
            time.sleep(.01)
    elif cause == "lease_released":
        post("/control/release", owner=owner)
    else:
        device.confirm_hold = False
        assert post("/skill/stop")["fault"]
        with pytest.raises(HttpError):
            prepare()
    resume.set()
    assert finished.wait(1)
    status = get("/skill/status?job=" + job)
    assert status["state"] == ("failed" if cause == "unconfirmed_hold" else "stopped")
    assert device.commands == ["before_inference"]
    if cause != "unconfirmed_hold":
        assert post("/control/acquire")["owner"] != owner
        with pytest.raises(HttpError):
            post("/skill/run", owner=owner, receipt=key)


def test_admission_rechecks_readiness_owner_expiry_and_observation(station):
    device, runtime, post, _, owner, prepare, *_ = station
    key = prepare()
    for bad in ("", "another-owner"):
        with pytest.raises(HttpError):
            post("/skill/run", owner=bad, receipt=key)
    device.at_station = False
    with pytest.raises(HttpError):
        post("/skill/run", owner=owner, receipt=key)
    device.at_station = True
    post("/skill/stop")
    with pytest.raises(HttpError):
        post("/skill/run", owner=owner, receipt=key)
    runtime.receipt_s = .001
    key = prepare()
    time.sleep(.01)
    with pytest.raises(HttpError):
        post("/ego/admission", owner=owner, receipt=key)
    runtime.frame_s = .001
    frame = post("/ego/observe")["frame_id"]
    time.sleep(.01)
    with pytest.raises(HttpError):
        post("/ego/prepare", owner=owner, frame_id=frame, operation="pick_black_box")
    assert not device.commands


def test_catalog_grasp_budget_and_independent_operator_stop(station):
    import os
    from pathlib import Path
    import subprocess

    from steward.ego.real.device import fake_grasp

    device, _, post, get, owner, prepare, entered, _, _, endpoint = station
    commands = []
    op = catalog()["pick_black_box"]
    assert op.prompt == "Pick up the big black box and hold it steady with both arms."
    assert fake_grasp(op, commands.append, threading.Event()) == "chunk_budget_exhausted"
    assert commands == list(range(13))
    job = post("/skill/run", owner=owner, receipt=prepare())["id"]
    assert entered.wait(1)
    # A separate operator process can interrupt a blocked model without the host's lease.
    host = Path(__file__).resolve().parents[1] / "scripts/run-real-ego"
    result = subprocess.run([host, "--stop"], env={**os.environ, "STEWARD_REAL_ENDPOINT": endpoint},
                            capture_output=True, text=True, timeout=3, check=True)
    assert job in result.stdout
    assert get("/skill/status?job=" + job)["state"] == "stopped"
    assert device.commands == ["before_inference"]


def test_cached_frame_cannot_be_relabelled_after_stop(station, monkeypatch):
    device, _, post, *_ = station
    cached = device.capture()
    post("/skill/stop")
    monkeypatch.setattr(device, "capture", lambda: cached)
    with pytest.raises(HttpError, match="predates"):
        post("/ego/observe")


def test_device_interrupt_confirms_hold_and_settles_job():
    def interrupt(op, send, cancelled):
        raise KeyboardInterrupt
    runtime = Runtime(FakeDevice(), catalog(), {"pick_black_box": interrupt})
    try:
        owner = runtime.acquire()["owner"]
        key = runtime.prepare(owner, "pick_black_box", runtime.observe()["frame_id"])["receipt"]
        job = runtime.run(owner, key)["id"]
        deadline = time.monotonic() + 1
        while (status := runtime.status(job))["state"] == "running":
            assert time.monotonic() < deadline
            time.sleep(.005)
        assert (status["state"], status["reason"], status["hold_confirmed"]) == ("stopped", "device_interrupt", True)
    finally:
        runtime.close()


def test_two_admissions_compete_for_one_actuator(station):
    device, _, post, _, owner, prepare, entered, *_ = station
    keys = [prepare(), prepare()]

    def run(key):
        try:
            return post("/skill/run", owner=owner, receipt=key)["state"]
        except HttpError as error:
            return error.status

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, keys))
    assert set(results) == {"running", 409}
    assert entered.wait(1)
    assert device.commands == ["before_inference"]


def test_waiting_for_a_camera_does_not_block_stop(station, monkeypatch):
    device, _, post, _, owner, prepare, entered, *_ = station
    job = post("/skill/run", owner=owner, receipt=prepare())["id"]
    assert entered.wait(1)
    capture_started, finish_capture = threading.Event(), threading.Event()
    capture = device.capture

    def stalled_camera():
        capture_started.set()
        assert finish_capture.wait(2)
        return capture()

    monkeypatch.setattr(device, "capture", stalled_camera)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(post, "/ego/observe")
        try:
            assert capture_started.wait(1)
            assert post("/skill/stop")["stopped"] == job
        finally:
            finish_capture.set()
        with pytest.raises(HttpError, match="generation"):
            future.result()
