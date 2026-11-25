# 真机控制台

控制台监听 `0.0.0.0:8400/console`，提供真机配置选择、三路实时相机、DEBUG 日志和 DeepSeek Flash 操作员对话。对话沿用仿真控制台的 Harness 会话和调用记录：模型按工具描述直接调用注册的真机工具；使用原生 function calling，不提供现场编程入口。连接诊断只显示在日志区；对话区显示操作员消息、工具名称与参数、工具返回和助手答复。WAM 通过独立双臂工具执行；对话模型仍没有相机图像理解能力。

导航、升降、缓存状态和停止工具通过 `ctx.ego` 调用真机 provider，再访问控制台拥有的 Motion 运行时；工具层不直接连接设备或 HTTP 服务。配置只加载真机运动工具，共用 `ctx.ego` 不会自动加载仿真工具。后续开发遵循[添加真机工具的流程](../../../../../dsh/packages/dsh-tool-ego-real/README.md#添加真机工具的流程)，其中说明服务定义、provider、运行时、模型描述和测试各自的职责。

设备与接口下方的“已支持的工具”面板从当前真机运行时读取动作工具列表，可折叠查看。右侧默认显示“操作员对话”；切换到 `System prompt` 可查看实际配置的系统提示词和单独列出的首轮操作说明（Harness 在请求时追加的工具声明等内容不包含在此配置预览中）；`Task context` 可编辑和保存任务位置、物料等上下文。切换页签保留对话、任务草稿和折叠状态，不会触发模型或设备调用，停止任务按钮在三个页签中始终可见。

操作员对话页签右上角的“新会话”会清空页面对话和输入草稿，下一条消息创建新的 DSH 会话，不携带旧对话历史。已保存的 Task context、设备连接、运动故障状态和磁盘中的旧会话日志均保留。任务执行期间按钮禁用，服务端也拒绝重置。

工具消息默认显示简短结果摘要；“调用参数”和“返回详情”分别展开入参及完整返回，JSON 使用小号等宽字体。工具调用正常返回但动作结果为 `failed` 时仍显示失败。输入框上方的故障提示显示原因和未确认的物理结果，原始数据收在“故障详情”中；轮询更新保留展开状态。

## 启动

从仓库根目录运行，需要 Python 3.11+、Node 和 pnpm。Python 环境需安装项目的 `real` 可选依赖（`websocket-client`）；升降驱动使用 Linux 标准库串口接口，不依赖 ROS、`../agent` 或 NAVIS 源码。

```sh
# 首次安装独立的控制台 Harness profile
STEWARD_REAL_HOME="$PWD/steward/var/dsh/ego-real-console" steward/scripts/setup-real-ego-profile.sh
# 使用已经安装 Steward 及 real 依赖的 Python 环境
PYTHONPATH=steward/src steward/.venv/bin/python -m steward.ego.real.console --key-file key.txt
```

`--key-file` 读取 API key，也可使用环境变量 `DEEPSEEK_API_KEY`。导航密码从配置中 `navigation.password_env` 指定的环境变量读取；默认名称为 `STEWARD_NAV_PASSWORD`，值应为导航系统登录接口接受的密码字段。可放在忽略版本管理的 `steward/var/real-console.env` 中，格式 `KEY=VALUE`，建议文件权限 `600`。`--env-file`、`--dsh-home` 可以覆盖默认路径。密钥不会返回网页或传入工具参数。

打开网页，选择 **Agilex Cobot Magic** 并点击“加载并连接”。加载配置只建立观测和工具入口，不移动、不自动归零、不切换地图。相机服务和已定位的导航地图需预先运行；升降串口不能被其他控制节点占用。默认地图为 `B308-V3`，相机服务在本机 `5000` 端口。Agent 缺少密钥或 profile 时网页会显示具体原因，画面仍可查看。

`--no-actuation` 启用服务端禁止运动模式：对话和 `ego_state` 可以运行，但所有运动工具在打开设备前被拒绝。适合验证模型连接与页面。正常模式下，只有发出操作员任务后才可能执行动作。页面没有登录认证，只应向可信操作员网络开放；`--host 127.0.0.1` 可限制为本机访问。

## 工具参数与结果

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `get_navigation_state` | `timeout_s`（必填，0.1–30 秒） | 只读获取底盘地图位置、朝向、定位有效性、两层导航状态、当前任务及进度 |
| `navigate_to_pose` | `x`、`y`（地图米制坐标），`heading_deg`（相对地图 +X 的角度，−180 至 180），`map_name`，`timeout_s`，`position_tolerance_m`，`heading_tolerance_deg` | 单个地图目标位姿；固定线速度 0.1 m/s、角速度 0.1 rad/s，开启避障；不含物料坐标、固定路线或速度入参 |
| `lift_move` | `action: "height"`、`height_mm`（整数 0–750）、`timeout_s`；或者 `action: "home"`、`timeout_s` | 固定 400 RPM；height 是绝对高度；home 真实向下运行至限位并归零，不能带 height_mm；不会自动 home |
| `wam_operate` | `prompt`、`max_chunks`、`timeout_s`，其他通用参数可选 | 执行 WAM 双臂动作，不自动归零；详见下方部署说明 |
| `arms_zero` | `timeout_s`、`open_grippers` | 双臂关节归零；是否张开夹爪必须明确传入 |
| `clear_motion_fault` | `fault_id`、`operator_confirmed_stopped: true`、`reason` | 现场检查后清除当前软件故障锁定；不发送设备指令，不归零、不重试 |
| `ego_state` | 无 | 读取配置、限速、当前动作及最近结果的缓存；不测量实时位姿或升降零点 |
| `body_stop` | 无 | 取消本轮动作、等待停止反馈，本轮不再接受运动 |

所有动作的 `timeout_s` 必填，范围 0.1–600 秒；导航该期限从目标发送后计时，连接与配置另有短期限。导航位置容差为 0.001–1 米，朝向容差为 0.1–30 度。地图必须已加载且定位有效，不会自动切换地图或接管其他客户端正在执行的任务。应保证同一底盘只有一个运动控制端。升降编码器零点在电机上电后需要显式 home 建立；驱动不能检测所有断电事件，零点不确定时必须先由操作员确认归零。

Agent 必须检查返回的 `state === "succeeded"` 后再执行后续动作。导航完成依据新任务的执行／终态和实测目标误差；失败、定位丢失、暂停、超时都不能作为成功。停止反馈分别报告控制器取消确认、寄存器写入确认和可获得的停止测量；导航取消确认不等于物理停稳。任何执行失败会保留故障并拒绝继续运动，需操作员确认停稳并检查现场后明确清除软件故障。未知结果不得自动重发。

NAVIS 驱动同时检查 `/agx_smach_state` 的任务调度状态和 `/run_management/global_status` 的底层导航状态，持续订阅以接收状态变化。列表任务反馈中的 `taskName: list_task` 是类型标记，具体任务身份使用 `btName`；两层都完成后仍须核对实测位姿。停止使用 NAVIS Board“结束任务”同款 `/agx_smach_operate` 服务（`operate=4`），等待服务成功和调度器终止状态；仅发布底层 `navi_task/cancel` 会留下仍占用调度器的列表任务。控制器确认不等于物理停稳确认。导航错误的 `feedback` 保留任务名、两类状态及 HTTP 拒绝的状态码和消息，不保留认证数据；`clear_motion_fault` 仍只清除 Steward 软件锁，不代替设备结束任务。

恢复时可发送：“我已确认设备停稳并检查现场，请清除软件故障，不要移动。”Agent 先读取 `ego_state.fault_id`，再调用 `clear_motion_fault`；`reason` 为 1–1000 字符的现场检查说明，确认必须来自操作员，不能由模型推断。运行时拒绝仍在执行、已关闭或编号已变化的故障清除请求。清除后保留 `last_result`，并在 `last_fault_clear` 记录原故障、原因和时间；没有故障时返回 `cleared: false`。这不会清除电机硬件告警、重新建立升降零点、打开运动权限或补发原任务。若本轮已调用 `body_stop`，清除故障后本轮仍不能运动，应等待下一条操作员指令；`--no-actuation` 也不会因此解除。

升降到达目标附近后先发送停止，在本次动作期限内等待连续两次零转速反馈，再核验实测高度误差小于 2 mm；等待期间不再发送目标位置。显式停止默认最多等待 2 秒停稳反馈，通信读取另受串口超时约束。停稳超时或高度不符仍报告失败，并在 `feedback` 中保留可取得的最后实测状态；停止确认与到位成功分别报告。

“停止任务”、断开连接和进程正常退出会取消本控制台发起的动作，等待驱动结束后释放连接。模型请求断连或工具 HTTP 请求超时也会触发取消。停止反馈超时会保留故障，不能显示已安全停止。网页按钮是软件取消，不是硬件急停；宿主断电、网络完全失联时无法保证远程底盘收到取消，现场硬件安全设施仍须有效。

`get_navigation_state` 经 `ctx.ego.navigation.readState` 调用独立只读连接，参考 NAVIS 的 `get_position` / `get_nav_status` 订阅 `/agx_smach_state`，同时读取 `/run_management/global_status`。`pose.x/y` 单位为米，`pose.heading_deg` 相对地图 +X、单位为度；`pose_valid=false` 时不能把坐标作为可靠定位。`scheduler_status` 和 `navigation_status.status` 分别保留两个状态层，`task.name` 使用列表任务的 `btName`，`progress_percent` 为设备报告的进度。`received_at_unix_s` 为各话题在本机的接收时间，不是传感器采集时间，两路数据不保证同一时刻。连接和认证另有短超时，`timeout_s` 限制等待反馈的时间；数据缺失或异常明确报失败，不返回伪造的零坐标。软件故障、禁用运动及运动进行中均可读取；读取失败不改变运动故障和最近动作结果，取消读取只释放读取连接。可在操作员对话中输入“读取底盘当前位置、朝向和导航状态，不要移动”。

## 配置和代码位置

[默认配置](configs/agilex-cobot-magic.json) 保存相机 topic、流与快照地址、机械臂 CAN 名、导航 HTTP/WebSocket 地址及地图、升降串口地址和电机 ID。`--config-dir` 指定自定义 JSON 目录，服务启动时校验所有配置；切换配置需先断开。增加型号需实现并注册启动适配器和对应运动驱动，不能只更换配置中的型号名称。

[chat.py](chat.py) 复用 `world.chat.HarnessChat`，只注册真机 robot 会话，使用独立 home 中的 `ego-real` profile 并叠加原生 motion 工具插件。原有工位 WAM profile 保持独立。工具由 [motion.ts](../../../../../dsh/packages/dsh-tool-ego-real/src/motion.ts) 注册；[motion.py](../motion.py) 在本机私有 HTTP 服务中校验参数、串行执行并处理取消；[drivers](../drivers) 保存已抽取的协议实现。UI 不直接发送设备协议。

相机从 `snapshot_url` 按 `refresh_fps` 采样 JPEG，结合 `camera_status_url` 中 `topics[status_key]` 判断画面是否有效。上游离线或超时显示遮罩，不显示假实时图。`stream_url` 保留原始合并 MJPEG 入口。日志和网页展示缓存有界；DSH 会话记录保存在所选 home，后续消息延续同一会话，断开配置后建立新会话。

## 真机 Skills

“已支持的工具”下方的“已支持的 Skills”展示当前机器人连接的技能目录，点击名称可展开完整 `SKILL.md` 文本并编辑。当前提供 `manhattan-navigation` 和 `factory-wam-operation`；可输入“按曼哈顿路径导航到地图中的 x=…、y=…、朝向=…”。目标值来自操作员或 task context。原生 Harness 在模型请求开始时提供技能摘要，再由模型调用 `skill` 加载正文；无需开放现场编程。网页和模型共用技能快照；网页保存会直接原子更新技能目录中的 `SKILL.md`，空闲时更新快照供下一轮使用，任务执行期间不能保存。保存校验 YAML、名称、描述和正文，拒绝旧页面覆盖新版本；失败保留编辑草稿。保存后的模型请求会记录版本更新提示，要求使用前重新加载正文，不清空原会话。新增技能或从外部编辑文件后，需要重新连接配置。后续开发步骤、自动发现范围和验证方法见[真机 skills 接入说明](../../../../../dsh/packages/dsh-bundle-ego-real/README.md#添加真机-skill)。

## Task context 的保存和发送

在右侧 `Task context` 中编辑资料，点击“保存上下文”。只有保存后的内容进入模型请求，未保存草稿不会覆盖已保存内容。执行任务期间不能修改；其他页面修改后，陈旧编辑会被拒绝，可用“恢复已保存内容”加载最新版本。清空编辑框并保存即可清除当前上下文。

上下文按真机配置 ID 保存到 `<DSH_HOME>/task-context/<配置ID>.txt`，与设备地址、工具定义分离；默认目录是 `steward/var/dsh/ego-real-console/task-context/`。断开、重连和重启会保留已保存内容。网页 `/api/context` 保存接口同时校验配置 generation 和客户端读取的旧内容，避免把旧页面内容写入另一台真机或覆盖其他页面的修改。

[RealChat._run](chat.py) 在每一轮操作员消息中附加完整 `task_context` JSON 快照；空字符串同样发送，明确取代之前轮次的上下文。这个消息经过正常 Harness 启动器进入模型请求并写入会话日志。上下文属于用户任务数据，不是系统指令，不注册新工具、不扩大工具权限。System prompt 页签展示配置预览，原生工具的描述及参数则通过请求的 `tools` 字段传递，不需要重复粘贴进系统提示词。

新增工具的开发步骤、描述要求和验证方法见[工具包接入说明](../../../../../dsh/packages/dsh-tool-ego-real/README.md#添加真机工具的流程)。注册到当前 profile 的工具会由 Harness 自动加入后续模型请求；只增加驱动代码或网页名称不会注册模型工具。

## 不移动真机的验证

```sh
PYTHONPATH=steward/src python3 -m pytest --noconftest -q steward/tests/test_real_motion.py steward/tests/test_real_console.py steward/tests/test_real_skills.py
pnpm --dir steward/dsh run build
STEWARD_PYTHON=python3 node --test steward/dsh/test/ego-motion.test.mjs
node --check steward/src/steward/ego/real/console/static/app.js
```

Python 测试使用模拟驱动，DSH 录制测试使用独立 HTTP 模型与设备接口，验证模型经 ctx.ego 调用清除故障、导航和升降工具、每轮上下文快照及清空后的续聊。真实模型只读验证可用 `--no-actuation` 启动后要求“只调用 ego_state，禁止任何运动”。这些检查不替代首次真机导航、升降和停止距离验收。

## Cosmos WAM 与双臂归零部署

`wam_operate` 必填 `prompt`、`max_chunks`（1–1000）和 `timeout_s`（0.1–600 秒）。工具还暴露关节控制模式、控制周期、模型频率校验、预期 execute_steps、平滑参数、夹爪比例和预测视频开关；定义及范围见模型工具描述。当前适配器支持 `movej` / `movejs`，机械臂速度固定 25%；不开放 CPV 或 EEF 模式。`arms_zero` 必填 `timeout_s` 和 `open_grippers`，固定 10% 速度；true 会完全张开双夹爪并释放持物，false 不改变夹爪目标。

机器人 JSON 的 `wam` 配置保存 `adapter: cosmos-piper-v2`、`host`、`port`、`python`、`cosmos_repo`、`piper_client_dir`、`python_paths`、`environment`、`log_dir` 及 `camera_width/height/fps`。机械臂 CAN 和相机 ROS topic 复用 `arms` / `cameras`。使用自定义旧配置时，从[默认配置](configs/agilex-cobot-magic.json)补充 `wam`，核对本机路径与端口后重启；缺少配置会在启动时明确报错。

执行桥接、子进程管理及归零算法均在 Steward 的 [drivers](../drivers) 内，不读取、导入或启动 `../agent`，也不读取其任务配置或 prompt。外部环境仍需安装设备与模型客户端库：当前 `wam.python` 为 `piper_new` 环境，提供 `piper_sdk`、`pyAgxArm`、ROS、OpenCV、NumPy、SciPy、msgpack 和 websockets 15+；`cosmos_repo` 指向已安装 Cosmos 客户端库，`piper_client_dir` 指向已验证的 Piper 相机/控制/平滑/记录库。迁移到另一台机器需要安装这些依赖并调整配置，不能只复制仓库后假设硬件 SDK 已存在。控制台 Python 与设备 worker Python 可以不同。

每次调用保存到 `wam.log_dir` 下独立目录，包含请求、worker 日志、最终结果，以及 WAM SDK 生成的反馈、动作和视频记录。失败结果保留日志路径；超时或强制终止后物理状态未知，必须检查现场。WAM 执行完 chunk 不等于识别或验证物料操作成功，也不会自动归零；物料 prompt、预算和归还后归零顺序只在 `factory-wam-operation` skill 中维护。
