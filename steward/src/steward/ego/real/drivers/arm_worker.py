"""Private subprocess entrypoint. Own the shared arm lock and stop before releasing devices."""
import fcntl
import json
from pathlib import Path
import signal
import sys
import time

from .arm_zero import go_zero
from .manipulation import resolve


def cleanup(resources, failed):
    """Attempt both arms even if one fails; retain all cleanup errors, never imply physical rest."""
    errors, stops = [], []
    arms = []
    for resource, kind in resources:
        if kind == 'sdk':
            arms.append((resource, 'MotionCtrl_1', (0x01, 0, 0), 'DisconnectPort'))
        elif kind == 'piper':
            for robot in (resource.robot_l, resource.robot_r):
                if robot is not None:
                    arms.append((robot, 'electronic_emergency_stop', (), 'disconnect'))
    if failed:
        for arm, method, args, _ in arms:
            try:
                getattr(arm, method)(*args)
                stops.append({'write_acknowledged': True})
            except Exception as exc:
                stops.append({'write_acknowledged': False, 'error': str(exc)})
    for arm, _, _, method in arms:
        try:
            getattr(arm, method)()
        except Exception as exc:
            errors.append(str(exc))
    for resource, method in reversed(resources):
        if method in ('piper', 'sdk'):
            continue
        try:
            if method == 'logger':
                resource.va_client = None
                report_path = Path(resource.save_dir) / 'report.json'
                report = json.loads(report_path.read_text()) if report_path.exists() else {}
                resource.finish(report.get('robot_commands_sent', 0), success=not failed,
                                error='execution failed' if failed else None)
            else:
                getattr(resource, method)()
        except Exception as exc:
            errors.append(str(exc))
    return {'requested': failed and bool(arms), 'arms': stops, 'physical_stop_confirmed': False}, errors


def run(request):
    """No retries or automatic reset. Lock is shared with the existing Piper client."""
    resources, result = [], None
    handlers = {}
    args = resolve(request['tool'], request['arguments'])
    deadline = time.monotonic() + args['timeout_s']
    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('机械臂执行超时')
    def interrupt(signum, frame):
        raise InterruptedError('机械臂调用已取消')
    with open('/tmp/piper-real-robot.lock', 'a+') as lock:
        locked = False
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            for sig in (signal.SIGTERM, signal.SIGINT):
                handlers[sig] = signal.signal(sig, interrupt)
            if request['tool'] == 'arms_zero':
                from piper_sdk import C_PiperInterface_V2
                arms = []
                devices = {a['id']: a['device'] for a in request['config']['arms']}
                for name in ('left', 'right'):
                    check()
                    arm = C_PiperInterface_V2(devices[name])
                    resources.append((arm, 'sdk'))
                    arms.append((name, arm))
                    arm.ConnectPort()
                result = go_zero(arms, args['open_grippers'], check)
            else:
                from .cosmos import execute
                result = execute(request['config'], args, request['directory'], resources, check)
        except BaseException as exc:
            result = {'state': 'failed', 'error': str(exc) or type(exc).__name__, 'physical_result': 'unknown'}
            report_path = Path(request['directory']) / 'report.json'
            if report_path.exists():
                try:
                    result['report'] = json.loads(report_path.read_text())
                except (OSError, ValueError) as report_error:
                    result['report_error'] = str(report_error)
        finally:
            if locked:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(sig, signal.SIG_IGN)
            stop, errors = cleanup(resources, result is None or result['state'] != 'succeeded')
            if result is None:
                result = {'state': 'failed', 'physical_result': 'unknown'}
            if result['state'] != 'succeeded':
                result['stop'] = stop
            if errors:
                result = {'state': 'failed', 'physical_result': 'unknown', 'error': '设备清理失败',
                          'cleanup_errors': errors, 'operation_result': result, 'stop': stop}
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    return result


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    result = run(request)
    path = Path(request['directory']) / 'result.json'
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False, default=str) + '\n')
    temp.replace(path)
    return 0 if result['state'] == 'succeeded' else 1


if __name__ == '__main__':
    raise SystemExit(main())
