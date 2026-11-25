"""Dual Piper joint zero with fresh feedback; optional full gripper opening is explicit.

Ported from the locally validated robot_agent/arm_zero.py algorithm. No agent imports.
The caller owns CAN handles, stop on failure, and exclusive robot access.
"""
import time

ZERO_SPEED_PERCENT = 10


def joint_error(arm):
    feedback = arm.GetArmJointMsgs()
    if feedback.time_stamp <= 0 or feedback.Hz <= 0 or abs(time.time() - feedback.time_stamp) > .5:
        return None
    return max(abs(getattr(feedback.joint_state, 'joint_' + str(i))) for i in range(1, 7))


def gripper_error(arm, target):
    feedback = arm.GetArmGripperMsgs()
    if feedback.time_stamp <= 0 or feedback.Hz <= 0 or abs(time.time() - feedback.time_stamp) > .5:
        return None
    return abs(feedback.gripper_state.grippers_angle - target)


def go_zero(arms, open_grippers, check):
    """Return measured arrival after five fresh samples; speed fixed at 10%, no reset."""
    targets = {}
    if open_grippers:
        requested_at, deadline = time.time(), time.monotonic() + 2
        while len(targets) != len(arms):
            check()
            if time.monotonic() >= deadline:
                raise RuntimeError('夹爪最大行程参数缺失或无效，取消归零')
            for _, arm in arms:
                arm.ArmParamEnquiryAndConfig(param_enquiry=0x04)
            time.sleep(.1)
            for name, arm in arms:
                f = arm.GetGripperTeachingPendantParamFeedback()
                maximum = f.arm_gripper_teaching_param_feedback.max_range_config
                if f.time_stamp >= requested_at and maximum in (70, 100):
                    targets[name] = maximum * 1000
    for name, arm in arms:
        deadline = time.monotonic() + 3
        while True:
            check()
            if arm.EnablePiper():
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(name + '使能超时')
            time.sleep(.02)
    deadline = time.monotonic() + 2
    while any(joint_error(arm) is None for _, arm in arms):
        check()
        if time.monotonic() >= deadline:
            raise RuntimeError('双臂反馈缺失或过期，取消归零')
        time.sleep(.02)
    for name, arm in arms:
        check()
        arm.ModeCtrl(0x01, 0x01, ZERO_SPEED_PERCENT, 0x00)
        if open_grippers:
            arm.GripperCtrl(targets[name], 1000, 0x01, 0)
    stable = 0
    while True:
        check()
        for _, arm in arms:
            arm.JointCtrl(0, 0, 0, 0, 0, 0)
        time.sleep(.05)
        errors = {name: joint_error(arm) for name, arm in arms}
        grips = {name: gripper_error(arm, targets[name]) for name, arm in arms} if open_grippers else {}
        arrived = all(e is not None and e <= 1000 for e in [*errors.values(), *grips.values()])
        stable = stable + 1 if arrived else 0
        if stable >= 5:
            return {'state': 'succeeded', 'joint_error_mdeg': errors, 'gripper_error_um': grips,
                    'open_grippers': open_grippers, 'gripper_targets_um': targets,
                    'arrival_confirmed': True}
