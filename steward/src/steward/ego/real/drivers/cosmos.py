"""Cosmos v2 adapter; SDK locations are deployment configuration, never model arguments.

All task selection belongs to skills. This module owns handshake validation and
execution glue formerly in ../agent, and uses installed Cosmos/Piper client libraries.
"""
from pathlib import Path
import sys
from types import SimpleNamespace

from .manipulation import WAM_SPEED_PERCENT


def configure(config, args, directory):
    """Load dependencies without device IO and resolve a complete client configuration."""
    deployment = config['wam']
    if deployment['adapter'] != 'cosmos-piper-v2':
        raise ValueError('未知 WAM adapter')
    for key in ('cosmos_repo', 'piper_client_dir'):
        if not Path(deployment[key]).is_dir():
            raise ValueError('WAM 依赖目录不存在: ' + key)
        sys.path.insert(0, deployment[key])
    from cosmos_framework.inference.causal_action.piper_client import load_lingbot, configuration
    hardware = load_lingbot(deployment['piper_client_dir'])
    # No external task/config JSON: prompt and budget come exclusively from tool input.
    cfg = configuration(SimpleNamespace(config_file=None, control_mode=args['control_mode'],
                        prompt=args['prompt'], max_chunks=args['max_chunks'], host=deployment['host'],
                        port=deployment['port'], output=directory), hardware)
    cfg.__dict__.update({k: v for k, v in args.items() if k not in ('timeout_s', 'expected_execute_steps')})
    arms = {a['id']: a['device'] for a in config['arms']}
    cameras = {c['id']: c['topic'] for c in config['cameras']}
    cfg.__dict__.update(can_left=arms['left'], can_right=arms['right'], cam_high=cameras['head'],
                        cam_left=cameras['left_wrist'], cam_right=cameras['right_wrist'],
                        cam_width=deployment['camera_width'], cam_height=deployment['camera_height'],
                        cam_fps=deployment['camera_fps'], save_dir=directory,
                        movej_speed_percent=WAM_SPEED_PERCENT / 100, model_family='causal-cosmos')
    return hardware, cfg


def connect(cfg, expected_execute_steps=None):
    """Bounded initial handshake, no reset/inference or arm enable until metadata validates."""
    from cosmos_framework.inference.causal_action.client import CausalPolicyClient
    from cosmos_framework.inference.causal_action.wire import unpackb
    from cosmos_framework.inference.causal_action.piper_protocol import CosmosProtocol
    from websockets.sync.client import connect as ws_connect
    client = CausalPolicyClient.__new__(CausalPolicyClient)
    client.connection = ws_connect(f'ws://{cfg.server_host}:{cfg.server_port}', compression=None,
                                   max_size=None, ping_interval=None, proxy=None, open_timeout=10, close_timeout=2)
    try:
        client.metadata = meta = unpackb(client.connection.recv(timeout=10))
        if (meta.get('protocol') != 'cosmos-causal-action-v2'
                or meta.get('quaternion_order') != 'xyzw' or meta.get('eef_pose_frame') != 'dataset'
                or 'joint' not in meta.get('available_control_modes', [])
                or any(meta.get(k) != 6 for k in ('left_arm_dof', 'right_arm_dof'))):
            raise ValueError('Cosmos 握手与双 Piper joint 协议不兼容')
        client.control_mode = 'joint'
        CosmosProtocol(client, cfg)
        if expected_execute_steps is not None and meta['execute_steps'] != expected_execute_steps:
            raise ValueError('服务端 execute_steps 与请求不匹配')
        return client
    except BaseException:
        client.close()
        raise


def execute(config, args, directory, resources, check):
    """Run a finite chunk budget. Completion does not establish grasp/place success."""
    hardware, cfg = configure(config, args, directory)
    from cosmos_framework.inference.causal_action.piper_protocol import CosmosProtocol, RunReport, run_loop
    check()
    client = connect(cfg, args.get('expected_execute_steps'))
    resources.append((client, 'close'))
    import rospy
    if not rospy.core.is_initialized():
        rospy.init_node('steward_wam', anonymous=True, disable_signals=True)
    camera = hardware.CameraManager(cam_high_topic=cfg.cam_high, cam_left_topic=cfg.cam_left,
             cam_right_topic=cfg.cam_right, width=cfg.cam_width, height=cfg.cam_height, fps=cfg.cam_fps)
    resources.append((camera, 'stop'))
    camera.start()
    check()
    piper = hardware.PiperController(can_left=cfg.can_left, can_right=cfg.can_right)
    # Register before connect so partial left/right connection failures also stop and close.
    resources.append((piper, 'piper'))
    piper.connect()
    for robot in (piper.robot_l, piper.robot_r):
        check()
        import time
        deadline = time.monotonic() + 3
        while not robot.enable():
            check()
            if time.monotonic() >= deadline:
                raise RuntimeError('机械臂使能超时')
            time.sleep(.02)
        robot.set_speed_percent(WAM_SPEED_PERCENT)
        hardware.restore_movej_mode(robot)
    smoother = None if cfg.smooth_method == 'none' else hardware.JointTrajectorySmoother(
        smooth_method=cfg.smooth_method, smooth_factor=cfg.smooth_factor, v_max=cfg.v_max,
        dt=cfg.action_dt, outlier_threshold_deg=cfg.outlier_threshold,
        savgol_window=cfg.savgol_window, savgol_order=cfg.savgol_order, butter_cutoff=cfg.butter_cutoff)
    logger = hardware.InferenceLogger(directory, cfg.prompt)
    logger.start(cfg.control_mode)
    resources.append((logger, 'logger'))
    report = RunReport(cfg)
    protocol = CosmosProtocol(client, cfg)
    def stop_check():
        check()
        return False
    run_loop(piper, camera, protocol, cfg, logger, smoother, hardware.send_action, stop_check, report)
    check()
    if report.data['status'] != 'complete' or report.data['completed_chunks'] != cfg.max_chunks:
        raise RuntimeError('WAM 未执行完请求的 chunk 数')
    result = {'state': 'succeeded', 'execution_complete': True, 'task_success': 'unknown',
              'message': 'chunk 预算执行完成；未验证抓取或放置成功，不自动归零或重试',
              'report': report.data}
    if cfg.save_prediction_video:
        result['prediction_video'] = client.save_prediction_video(Path(directory) / 'prediction.mp4')
    return result
