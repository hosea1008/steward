"""Launch the fake device explicitly. Hardware deployment requires its commissioned adapter."""
import argparse
import signal
import threading

from .api import server
from .device import FakeDevice, fake_grasp
from .runtime import Runtime, catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["fake"], required=True)
    parser.add_argument("--port", type=int, default=7421)
    args = parser.parse_args()
    runtime = Runtime(FakeDevice(), catalog(), {"pick_black_box": fake_grasp})
    done = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: done.set())
    try:
        http = server(runtime, args.port).start()
        try:
            print(f"ego-real fake device at {http.url}", flush=True)
            done.wait()
        finally:
            http.stop()
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
