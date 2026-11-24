"""Launch the real robot console at http://0.0.0.0:8400/console."""
import argparse
import logging
import os
from pathlib import Path
import signal
import threading

from .config import DEFAULT_CONFIG_DIR, load_configs
from .server import make_server
from .service import Console
from .chat import ConsoleAgent, ROOT
from steward.world.chat import load_env_file


def main():
    """Load deployment profiles and release connections on SIGINT or SIGTERM."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8400)
    parser.add_argument('--config-dir', type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument('--dsh-home', type=Path, default=ROOT/'var/dsh/ego-real-console')
    parser.add_argument('--env-file', type=Path, default=ROOT/'var/real-console.env')
    parser.add_argument('--key-file', type=Path, help='Read the DeepSeek API key without logging its contents')
    parser.add_argument('--no-actuation', action='store_true', help='Enable conversation but reject all movement requests')
    args = parser.parse_args()
    for key, value in load_env_file(args.env_file).items():
        os.environ.setdefault(key, value)
    if args.key_file:
        os.environ['DEEPSEEK_API_KEY'] = args.key_file.read_text().strip()
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')
    console = Console(load_configs(args.config_dir), agent_factory=lambda config, emit: ConsoleAgent(
        config, emit, args.dsh_home.resolve(), args.env_file.resolve(), motion_enabled=not args.no_actuation))
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
