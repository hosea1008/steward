"""Launch the read-only real robot console at http://127.0.0.1:8400/console."""
import argparse
import logging
from pathlib import Path
import signal
import threading

from .config import DEFAULT_CONFIG_DIR, load_configs
from .server import make_server
from .service import Console


def main():
    """Load deployment profiles and release connections on SIGINT or SIGTERM."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8400)
    parser.add_argument('--config-dir', type=Path, default=DEFAULT_CONFIG_DIR)
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')
    console = Console(load_configs(args.config_dir))
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    try:
        http = make_server(console, args.port, args.host).start()
        try:
            print(f'Real console: {http.url}/console', flush=True)
            done.wait()
        finally:
            http.stop()
    finally:
        console.close()


if __name__ == '__main__':
    main()
