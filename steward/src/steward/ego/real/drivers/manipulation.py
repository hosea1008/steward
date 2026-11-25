"""Bounded, isolated arm workers. Deployment selects adapters; tools select task parameters."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time

from steward.net import HttpError

WAM_SPEED_PERCENT = 25

# Runtime-owned defaults mirror the installed Cosmos joint client, not a material task.
WAM_DEFAULTS = dict(control_mode='movej', action_dt=.02, model_fps=30., wait_tol_deg=3.,
                    smooth_method='spline', smooth_factor=.1, v_max=3., outlier_threshold=10.,
                    savgol_window=11, savgol_order=2, butter_cutoff=5.,
                    left_gripper_scale=1., right_gripper_scale=1., gripper_close_threshold=.02,
                    save_prediction_video=False)


def resolve(tool, args):
    """Validate model input before process/device creation; prompt and chunk budget are mandatory."""
    fields = ({'prompt', 'max_chunks', 'timeout_s', 'expected_execute_steps'} | WAM_DEFAULTS.keys()
              if tool == 'wam_operate' else {'timeout_s', 'open_grippers'})
    if not isinstance(args, dict) or args.keys() - fields:
        raise HttpError(400, '未知机械臂工具参数；设备地址和速度不可覆盖')
    def number(key, low, high, integer=False):
        v = values.get(key)
        if (type(v) not in ((int,) if integer else (int, float))
                or not math.isfinite(v) or not low <= v <= high):
            raise HttpError(400, f'{key} 必须在 {low}–{high} 之间' + ('且为整数' if integer else ''))
    values = {**(WAM_DEFAULTS if tool == 'wam_operate' else {'open_grippers': True}), **args}
    number('timeout_s', .1, 600)
    if tool == 'arms_zero':
        if type(values.get('open_grippers')) is not bool:
            raise HttpError(400, 'open_grippers 必须为布尔值；默认 true，会释放持物')
        return values
    if not isinstance(values.get('prompt'), str) or not 1 <= len(values['prompt'].strip()) <= 10000:
        raise HttpError(400, 'prompt 必须为非空字符串，最多 10000 字符')
    number('max_chunks', 1, 1000, True)
    if values['control_mode'] not in ('movej', 'movejs'):
        raise HttpError(400, 'control_mode 支持 movej 或 movejs')
    if values['smooth_method'] not in ('none', 'spline', 'savgol', 'butter'):
        raise HttpError(400, '不支持的 smooth_method')
    for key, low, high in [('action_dt', .01, .2), ('model_fps', 1, 60), ('wait_tol_deg', .1, 5),
                          ('smooth_factor', 0, 1), ('v_max', .01, 3), ('outlier_threshold', .1, 30),
                          ('butter_cutoff', .01, 40), ('left_gripper_scale', 0, 1),
                          ('right_gripper_scale', 0, 1), ('gripper_close_threshold', 0, .08)]:
        number(key, low, high)
    number('savgol_window', 3, 101, True)
    number('savgol_order', 1, 10, True)
    if values['savgol_window'] % 2 != 1 or values['savgol_order'] >= values['savgol_window']:
        raise HttpError(400, 'savgol_window 必须为奇数且大于 savgol_order')
    if values['butter_cutoff'] >= .5 / values['action_dt']:
        raise HttpError(400, 'butter_cutoff 必须低于控制频率的一半')
    if type(values['save_prediction_video']) is not bool:
        raise HttpError(400, 'save_prediction_video 必须为布尔值')
    if 'expected_execute_steps' in values:
        number('expected_execute_steps', 1, 10000, True)
    return values


class ArmWorker:
    """Own one process and report unknown physical state if cancellation cannot finish cleanup."""

    def __init__(self, config, cancelled):
        self.config, self.cancelled = config, cancelled
        self.process = None
        self.result = None

    def execute(self, tool, args):
        cfg = self.config['wam']
        root = Path(cfg['log_dir']).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=tool + '-', dir=root))
        result_path = directory / 'result.json'
        request = {'tool': tool, 'arguments': args, 'config': self.config, 'directory': str(directory)}
        request_path = directory / 'request.json'
        request_path.write_text(json.dumps(request), encoding='utf-8')
        env = dict(os.environ)
        # Explicit source root permits a separate ROS/Piper interpreter without installing ../agent.
        src = str(Path(__file__).resolve().parents[4])
        env['PYTHONPATH'] = os.pathsep.join([src, *cfg.get('python_paths', []), env.get('PYTHONPATH', '')])
        env.update(cfg.get('environment', {}))
        reason = None
        with (directory / 'worker.log').open('w') as log:
            if self.cancelled():
                raise InterruptedError('机械臂调用已取消，未启动设备连接')
            self.process = subprocess.Popen([cfg['python'], '-m', 'steward.ego.real.drivers.arm_worker',
                                             str(request_path)], stdout=log, stderr=subprocess.STDOUT,
                                            env=env, start_new_session=True)
            deadline = time.monotonic() + args['timeout_s']
            while self.process.poll() is None:
                if self.cancelled() or time.monotonic() >= deadline:
                    reason = '调用已取消' if self.cancelled() else '机械臂执行超时'
                    self._terminate()
                    break
                time.sleep(.05)
        try:
            result = json.loads(result_path.read_text())
            if not isinstance(result, dict) or result.get('state') not in ('failed', 'succeeded'):
                raise ValueError('无效的 worker 结果')
        except (OSError, ValueError) as exc:
            result = {'state': 'failed', 'physical_result': 'unknown', 'error': str(exc),
                      'stop': {'physical_stop_confirmed': False}}
        if reason or self.process.returncode != 0:
            result = {**result, 'state': 'failed', 'error': reason or result.get('error', '机械臂进程异常退出'),
                      'physical_result': 'unknown'}
        result['log_dir'] = str(directory)
        self.result = result
        return result

    def _terminate(self):
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)

    def stop(self):
        """Interrupt the worker; exit alone is never evidence of physical rest."""
        self._terminate()
        return (self.result or {}).get('stop', {'requested': True, 'physical_stop_confirmed': False})

    def close(self):
        self._terminate()
