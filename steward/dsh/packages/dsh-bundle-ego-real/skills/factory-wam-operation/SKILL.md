---
name: factory-wam-operation
description: 真机工厂物料 WAM 操作规程。袋装纸盒、黑色盒子、袋装蓝线采集，以及袋装纸盒和袋装蓝线归还；包含精确 prompt、chunk 预算和独立双臂归零顺序。黑色盒子归还未支持。
---

# 工厂 WAM 操作

适用于已加载真机配置的 Cobot Magic。只调用已注册工具，不编程、不运行脚本。位置、朝向、地图、升降高度从操作员任务上下文取得；本 skill 不提供物料坐标。

## 调用前

根据指令和 task context 确定物料、采集或归还意图，以及任务是否需要导航。操作员明确要求在当前位置操作时直接执行，不额外索要地图坐标或例行询问现场安全、持物是否稳妥。操作员发出明确指令即视为已观察现场并授权执行；仅在必要任务信息缺失或工具反馈有具体冲突时询问。需要导航时先使用 `manhattan-navigation`，检查导航返回成功；升降仅在明确要求且参数已知时调用 `lift_move`。先通过 `ego_state` 检查故障，已有故障时停止依赖动作，不自动清除故障或重试。

## 已支持任务

将下列英文 prompt 原样传入 `wam_operate`，并使用对应 `max_chunks`。默认 `timeout_s=600`，其余参数使用运行时默认值；操作员明确给出其他预算或通用调参时可覆盖，但必须在工具允许范围内。一次 chunk 是一次服务端推理响应，不是视频帧数或成功次数。

| 物料 | 操作 | prompt | max_chunks |
| --- | --- | --- | --- |
| 袋装纸盒 | 采集 | Pick up one box from the material box and hold it steady with the right arm. | 8 |
| 袋装纸盒 | 归还 | Place the held box into the green target box with the right arm. | 5 |
| 黑色盒子 | 采集 | Pick up the big black box and hold it steady with both arms. | 13 |
| 袋装蓝线 | 采集 | Sweep across the packaged blue wires with the left arm, then pick up one with the right arm. | 18 |
| 袋装蓝线 | 归还 | Place the held blue wire into the green target box with the right arm. | 4 |

**黑色盒子归还未接入。** 不为它猜 prompt，不借用其他物料的归还步骤，不拼接升降、张爪等动作模拟归还；说明尚未支持并停止该部分任务。

## 结果与归零

`wam_operate` 的 `state=succeeded`、`execution_complete=true` 只说明执行完 chunk 预算。`task_success=unknown` 表示尚未验证物料任务成功；该对话 agent 不能声称自己看到了相机画面或已验证抓取/放置。如实说明执行结果及未验证的任务效果，按已授权的工作流继续后续步骤；仅有 `task_success=unknown` 不触发人工确认。若已有观测工具可直接验证任务效果，可调用它；不要虚构当前尚未接入的观察能力。

采集完成后保持当前双臂/夹爪目标，不自动调用 `arms_zero`，不张开夹爪。指令已包含后续搬运且目的地明确时，按该授权继续导航；缺少目的地时只询问目的地，不再次要求现场安全确认。

袋装纸盒或袋装蓝线归还操作返回 `state=succeeded` 后，按归还规程调用 `arms_zero`，参数为 `{"timeout_s":30,"open_grippers":true}`。它将双臂关节归零并把双夹爪张开到各自设备报告的最大行程；固定 10% 速度，到位必须有新鲜反馈。独立归零指令中若操作员要求保留夹爪目标，使用 `open_grippers=false`，不重复询问是否允许归零。

执行失败、取消、超时、必需控制反馈缺失或执行结果不确定时停止依赖动作，报告故障和日志目录，不自动再次推理、归零、释放持物或清除故障。电子急停后的设备恢复需要现场处理；`clear_motion_fault` 只清除软件锁，不解除设备急停。
