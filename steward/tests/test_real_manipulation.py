"""No real devices: arm admission, process cancellation, zero feedback, and skill policy."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from steward.ego.real.console.config import DEFAULT_CONFIG_DIR, load_configs
from steward.ego.real.console.skills import SkillCatalog, SKILL_DIR
from steward.ego.real.drivers import arm_worker, arm_zero, cosmos
from steward.ego.real.drivers.manipulation import ArmWorker, resolve
from steward.ego.real.motion import Motion
from steward.net import HttpError

WAM = dict(prompt='fixture operation', max_chunks=2, timeout_s=30)
ZERO = dict(open_grippers=False, timeout_s=30)


def config():
    return deepcopy(load_configs(DEFAULT_CONFIG_DIR)['agilex-cobot-magic'])


@pytest.mark.parametrize('args', [dict(WAM, max_chunks=0), dict(WAM, max_chunks=True),
    dict(WAM, prompt=' '), dict(WAM, host='other'), dict(WAM, movej_speed_percent=1),
    dict(WAM, action_dt=float('nan')), dict(WAM, control_mode='cpv'),
    dict(WAM, savgol_window=4), dict(WAM, savgol_window=3, savgol_order=3),
    dict(WAM, butter_cutoff=30), dict(WAM, save_prediction_video='yes'),
    dict(WAM, expected_execute_steps=0), dict(WAM, max_chunks=1.5)])
def test_invalid_wam_arguments_rejected(args):
    with pytest.raises(HttpError):
        resolve('wam_operate', args)


def test_zero_requires_explicit_release_choice():
    with pytest.raises(HttpError):
        resolve('arms_zero', {'timeout_s': 30})
    assert resolve('arms_zero', ZERO) == ZERO


@pytest.mark.parametrize('tool,args', [('wam_operate', WAM), ('arms_zero', ZERO)])
def test_actuation_disabled_never_creates_arm_worker(tool, args):
    def forbidden(*a):
        pytest.fail('arm worker must not start')
    m = Motion(config(), lambda *a: None, enabled=False, arm_factory=forbidden)
    try:
        m.begin()
        with pytest.raises(HttpError):
            m.execute(tool, args)
    finally:
        m.close()


def test_failed_worker_latches_fault_and_prevents_followup():
    calls = []
    class Worker:
        def __init__(self, cfg, cancelled): pass
        def execute(self, tool, args):
            calls.append(tool)
            return {'state': 'failed', 'physical_result': 'unknown', 'stop': {'physical_stop_confirmed': False}}
        def close(self): pass
    m = Motion(config(), lambda *a: None, arm_factory=Worker)
    try:
        m.begin()
        assert m.execute('wam_operate', WAM)['state'] == 'failed'
        assert m.state()['fault_id']
        with pytest.raises(HttpError): m.execute('arms_zero', ZERO)
        assert calls == ['wam_operate']
    finally: m.close()


def test_arm_execution_serializes_with_lift_and_receives_turn_cancel():
    started = threading.Event()
    class Worker:
        def __init__(self, cfg, cancelled): self.cancelled = cancelled
        def execute(self, tool, args):
            started.set()
            while not self.cancelled(): time.sleep(.005)
            return {'state': 'failed', 'error': 'cancelled'}
        def close(self): pass
    m = Motion(config(), lambda *a: None, arm_factory=Worker)
    m.begin()
    thread = threading.Thread(target=lambda: m.execute('wam_operate', WAM))
    thread.start()
    try:
        assert started.wait(1)
        with pytest.raises(HttpError): m.execute('lift_move', dict(action='home', timeout_s=1))
        m.stop()
        assert m.state()['fault']
    finally:
        m.stop(); thread.join(1); m.close()


def test_worker_cancel_kills_stuck_process_and_never_reports_success(tmp_path):
    # Stand-in interpreter ignores all module arguments and TERM; no SDK can be imported.
    interpreter = tmp_path/'python'
    interpreter.write_text('#!/bin/sh\ntrap "" TERM\nexec sleep 30\n')
    interpreter.chmod(0o700)
    cfg = config(); cfg['wam'].update(python=str(interpreter), log_dir=str(tmp_path/'logs'))
    worker = ArmWorker(cfg, lambda: False)
    result = worker.execute('wam_operate', resolve('wam_operate', dict(WAM, timeout_s=.1)))
    assert result['state'] == 'failed' and result['physical_result'] == 'unknown'
    assert not result['stop']['physical_stop_confirmed']
    assert worker.process.poll() is not None
    assert (Path(result['log_dir'])/'worker.log').exists()


def test_cleanup_attempts_both_stops_and_reports_errors():
    calls = []
    class Arm:
        def __init__(self, name): self.name = name
        def MotionCtrl_1(self, *args):
            calls.append(self.name)
            if self.name == 'left': raise OSError('CAN failed')
        def DisconnectPort(self): pass
    stop, errors = arm_worker.cleanup([(Arm('left'), 'sdk'), (Arm('right'), 'sdk')], True)
    assert calls == ['left', 'right']
    assert stop['arms'][0]['write_acknowledged'] is False
    assert stop['arms'][1]['write_acknowledged'] is True
    assert not stop['physical_stop_confirmed'] and not errors


def test_stale_zero_feedback_is_not_arrival():
    arm = SimpleNamespace(GetArmJointMsgs=lambda: SimpleNamespace(time_stamp=0, Hz=0))
    assert arm_zero.joint_error(arm) is None


def test_zero_without_opening_preserves_gripper_targets():
    calls = []
    class Arm:
        def EnablePiper(self): return True
        def GetArmJointMsgs(self):
            return SimpleNamespace(time_stamp=time.time(), Hz=50,
                joint_state=SimpleNamespace(**{'joint_'+str(i):0 for i in range(1,7)}))
        def ModeCtrl(self, *args): calls.append(('mode', args))
        def JointCtrl(self, *args): calls.append(('joint', args))
        def GripperCtrl(self, *args): pytest.fail('gripper must not be commanded')
    result = arm_zero.go_zero([('left', Arm()), ('right', Arm())], False, lambda: None)
    assert result['arrival_confirmed'] and result['gripper_targets_um'] == {}
    assert len([c for c in calls if c[0]=='joint']) == 10
    assert [c[1][2] for c in calls if c[0]=='mode'] == [10,10]


def test_worker_failure_stops_partial_connection(monkeypatch, tmp_path):
    calls=[]
    class Arm:
        def __init__(self, device): self.device=device
        def ConnectPort(self):
            if self.device == 'can_right': raise OSError('connect failed')
        def MotionCtrl_1(self, *args): calls.append(('stop', self.device))
        def DisconnectPort(self): calls.append(('close', self.device))
    monkeypatch.setitem(sys.modules, 'piper_sdk', SimpleNamespace(C_PiperInterface_V2=Arm))
    result=arm_worker.run(dict(tool='arms_zero', arguments=ZERO, config=config(), directory=str(tmp_path)))
    assert result['state']=='failed'
    assert calls == [('stop','can_left'),('stop','can_right'),('close','can_left'),('close','can_right')]


def test_factory_skill_policy_and_budgets():
    catalog = SkillCatalog(SKILL_DIR)
    try:
        body=catalog.detail('factory-wam-operation')['text']
        for budget in (8,5,13,18,4): assert f'| {budget} |' in body
        assert '黑色盒子归还未接入' in body
        assert 'Place the big black box' not in body
        assert 'task_success=unknown' in body and '"open_grippers":true' in body
    finally: catalog.close()


def test_real_worker_process_with_fake_sdk_and_no_agent_path(tmp_path):
    sdk = tmp_path/'sdk'; sdk.mkdir()
    (sdk/'piper_sdk.py').write_text('''
import time
from types import SimpleNamespace
class C_PiperInterface_V2:
    def __init__(self, device): pass
    def ConnectPort(self): pass
    def DisconnectPort(self): pass
    def EnablePiper(self): return True
    def ModeCtrl(self, *args): pass
    def JointCtrl(self, *args): pass
    def GetArmJointMsgs(self):
        return SimpleNamespace(time_stamp=time.time(), Hz=50,
            joint_state=SimpleNamespace(**{'joint_'+str(i):0 for i in range(1,7)}))
''')
    cfg=config(); cfg['wam'].update(python=sys.executable, python_paths=[str(sdk)],
                                    log_dir=str(tmp_path/'logs'), environment={})
    result=ArmWorker(cfg, lambda:False).execute('arms_zero', ZERO)
    assert result['state']=='succeeded', result
    assert result['arrival_confirmed'] and result['open_grippers'] is False
    assert json.loads((Path(result['log_dir'])/'request.json').read_text())['arguments']==ZERO


def test_cosmos_handshake_rejects_mismatch_and_closes_before_hardware(monkeypatch):
    closed=[]
    class Connection:
        def recv(self, **kw): return {'protocol':'wrong'}
        def close(self): closed.append(True)
    class Client:
        def close(self): self.connection.close()
    modules={
        'cosmos_framework.inference.causal_action.client':SimpleNamespace(CausalPolicyClient=Client),
        'cosmos_framework.inference.causal_action.wire':SimpleNamespace(unpackb=lambda x:x),
        'cosmos_framework.inference.causal_action.piper_protocol':SimpleNamespace(CosmosProtocol=lambda *a: None),
        'websockets.sync.client':SimpleNamespace(connect=lambda *a, **k:Connection()),
    }
    for name, module in modules.items(): monkeypatch.setitem(sys.modules,name,module)
    with pytest.raises(ValueError, match='协议不兼容'):
        cosmos.connect(SimpleNamespace(server_host='fixture',server_port=8000))
    assert closed==[True]


def test_cosmos_execution_preserves_prompt_budget_and_reports_unverified_task(monkeypatch, tmp_path):
    calls=[]
    class Robot:
        def enable(self): calls.append('enable'); return True
        def set_speed_percent(self, speed): assert speed==25
    class Piper:
        def __init__(self, **kw): self.robot_l=Robot(); self.robot_r=Robot()
        def connect(self): calls.append('robot-connect')
    class Camera:
        def __init__(self, **kw): pass
        def start(self): calls.append('camera-start')
    class Logger:
        def __init__(self, *a): pass
        def start(self, mode): assert mode=='movej'
    class Report:
        def __init__(self,cfg): self.data={}
    def run_loop(piper, camera, protocol, cfg, logger, smoother, send_action, stop_check, report):
        assert cfg.prompt==WAM['prompt'] and cfg.max_chunks==2
        assert stop_check() is False
        report.data.update(status='complete',completed_chunks=2,robot_commands_sent=64)
    cfg=SimpleNamespace(**resolve('wam_operate',dict(WAM,smooth_method='none')),
        can_left='left',can_right='right',cam_high='head',cam_left='left',cam_right='right',
        cam_width=640,cam_height=480,cam_fps=30)
    hardware=SimpleNamespace(PiperController=Piper,CameraManager=Camera,InferenceLogger=Logger,
        restore_movej_mode=lambda robot:None,send_action=lambda *a:None)
    monkeypatch.setattr(cosmos,'configure',lambda *a:(hardware,cfg))
    def connect(*a): calls.append('handshake'); return object()
    monkeypatch.setattr(cosmos,'connect',connect)
    monkeypatch.setitem(sys.modules,'rospy',SimpleNamespace(core=SimpleNamespace(is_initialized=lambda:True)))
    monkeypatch.setitem(sys.modules,'cosmos_framework.inference.causal_action.piper_protocol',
        SimpleNamespace(CosmosProtocol=lambda *a:object(),RunReport=Report,run_loop=run_loop))
    resources=[]
    result=cosmos.execute(config(),WAM,str(tmp_path),resources,lambda:None)
    assert calls.index('handshake') < calls.index('enable')
    assert result['execution_complete'] and result['task_success']=='unknown'
    assert result['report']['completed_chunks']==2
    assert [method for _,method in resources]==['close','stop','piper','logger']


def test_zero_opening_uses_each_queried_limit_and_fresh_gripper_feedback():
    commands=[]
    class Arm:
        def __init__(self, maximum): self.maximum=maximum; self.target=None
        def ArmParamEnquiryAndConfig(self, **kw): assert kw=={'param_enquiry':0x04}
        def GetGripperTeachingPendantParamFeedback(self):
            return SimpleNamespace(time_stamp=time.time(),
                arm_gripper_teaching_param_feedback=SimpleNamespace(max_range_config=self.maximum))
        def EnablePiper(self): return True
        def GetArmJointMsgs(self):
            return SimpleNamespace(time_stamp=time.time(),Hz=50,
                joint_state=SimpleNamespace(**{'joint_'+str(i):0 for i in range(1,7)}))
        def ModeCtrl(self,*args): pass
        def GripperCtrl(self,target,*args): self.target=target; commands.append(target)
        def JointCtrl(self,*args): pass
        def GetArmGripperMsgs(self):
            return SimpleNamespace(time_stamp=time.time(),Hz=50,
                gripper_state=SimpleNamespace(grippers_angle=self.target))
    result=arm_zero.go_zero([('left',Arm(70)),('right',Arm(100))],True,lambda:None)
    assert commands==[70000,100000]
    assert result['gripper_error_um']=={'left':0,'right':0}
    assert result['arrival_confirmed']
