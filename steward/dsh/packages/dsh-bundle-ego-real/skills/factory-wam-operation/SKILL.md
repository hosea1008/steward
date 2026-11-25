---
name: factory-wam-operation
description: 真机工厂物料 WAM 操作规程。袋装纸盒、黑色盒子、袋装蓝线采集，以及袋装纸盒和袋装蓝线归还；包含精确 prompt、chunk 预算和独立双臂归零顺序。黑色盒子归还未支持。
---

# 工厂 WAM 操作

适用于已加载真机配置的 Cobot Magic。只调用已注册工具，不编程、不运行脚本。位置、朝向、地图、升降高度从操作员任务上下文取得；本 skill 不提供物料坐标。

## 调用前

先确定物料、采集或归还意图、目标操作位姿和当前持物情况。信息不足时询问操作员。需要导航时先使用 `manhattan-navigation`，要求导航成功且底盘停稳；升降仅在明确要求且参数已知时调用 `lift_move`。先通过 `ego_state` 检查故障，已有故障时停止依赖动作，不自动清除故障或重试。

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

`wam_operate` 的 `state=succeeded`、`execution_complete=true` 只说明执行完 chunk 预算。`task_success=unknown` 表示尚未验证物料任务成功；该对话 agent 不能声称自己看到了相机画面或已验证抓取/放置。将执行结果告诉操作员，在需要依赖任务成功的下一步动作前取得现场确认。

采集完成后保持当前双臂/夹爪目标，不自动调用 `arms_zero`，不张开夹爪。后续搬运需要操作员确认持物稳妥、通道和机械臂姿态允许移动，并提供目的地。

袋装纸盒或袋装蓝线归还经现场确认成功、物料已释放且归零路径无障碍后，调用 `arms_zero`，参数为 `{"timeout_s":30,"open_grippers":true}`。它将双臂关节归零并把双夹爪张开到各自设备报告的最大行程；固定 10% 速度，到位必须有新鲜反馈。独立归零指令中若操作员要求保留夹爪目标，使用 `open_grippers=false`；仍需确认双臂移动本身允许执行。

任何失败、取消、超时、反馈缺失或结果不确定都停止依赖动作，报告故障和日志目录，不自动再次推理、归零、释放持物或清除故障。电子急停后的设备恢复需要现场处理；`clear_motion_fault` 只清除软件锁，不解除设备急停。
