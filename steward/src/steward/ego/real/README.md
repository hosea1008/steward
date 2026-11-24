# ego-real 工位运行与交接

[真机控制台](console/README.md) 在本机 `8400/console` 提供配置选择、实时相机、连接状态、调试日志及 DeepSeek Flash 对话，直接调用地图目标导航和显式升降工具。

`ego-real` 使用机器人项目锁定的 DSH，独立的 profile、会话目录及 ER 进程。可执行的验收后端为 `--backend fake`。合成相机图和模拟驱动均在本进程内；启动器要求显式选择后端。抓黑盒的操作 ID 为 `pick_black_box`，固定英文指令和 13 个 chunk 的预算在 [operations.toml](operations.toml)。最终现场验收需要真机配置、驱动反馈与抓取记录。

```text
操作员原话 → scripts/run-real-ego（持有、续租、释放控制权）
              └─ DSH ego-real 实例 / EA 会话
                   ├─ ego_observe → 采集帧 → DSH 图像附件 → 模型下一轮请求
                   ├─ 可选观察 MCP → 用 frame_id 读同一帧
                   └─ prepare / run / status / stop → 本机 ER → 运行中的策略 → 受控发送 → 设备
独立终止命令 ───────────────────────────────────────────→ 本机 ER → 停止并确认保持
```

## 从源码验证

以下命令在含 `pyproject.toml` 的机器人项目下执行。Python 3.11+ 足以运行假设备 ER。EH 需要 Node 与 pnpm；本次使用 Node 24、pnpm 11.28.5 和工作区锁定的 DSH 0.2.1-rc.3。

```sh
scripts/setup-real-ego-profile.sh
PYTHONPATH=src python3 -m steward.ego.real --backend fake
```

### 后续接入 GPT Astra

[EA 模型模板](../../../../config/ego-real-ea-model.example.yml) 是本项目预留的接入点。将它复制到独立 home 的 `ea-model.patch.yml`，按部署方给出的实际值设置 `STEWARD_REAL_EA_PROVIDER` 和 `STEWARD_REAL_EA_MODEL`。已有 DSH provider 可直接选择；如需注册网关，把对应 provider 配置加在同一文件中。模板附有 Chat Completions 兼容接口的条件示例，凭据使用环境变量引用。具体协议、地址和模型 ID 等待部署确认。验收时应确认模型能返回原生工具调用、接收 DSH 传来的图像，并对抓稳证据不足如实作答。

`scripts/run-real-ego` 优先读取显式传入的 `--patch`，其次读取独立 home 的 `ea-model.patch.yml`。缺少模型配置时，在申请机器人控制权之前退出。独立人工停止命令始终可用。

```sh
scripts/run-real-ego --patch /绝对路径/model.yml '拿起面前的黑盒，抓稳后等我'
scripts/run-real-ego --stop
```

默认 ER 地址为 `http://127.0.0.1:7421`，用 `STEWARD_REAL_ENDPOINT` 覆盖。默认会话及工作目录在 `var/dsh/ego-real` 下；如需自定义，在 setup 和启动时设置相同的绝对路径 `STEWARD_REAL_HOME`。`--session-id` 可继续该 home 内已有的会话。每次任务启动时重新获取租约，宿主退出时停止其未完成作业并交回控制权。服务面向可信本机进程，独立停止入口不需要租约。

无模型凭据和机器人也可运行的端到端验收使用独立临时 home、真实 DSH CLI 和本机 HTTP 模型替身。它检查实际请求里的身份、任务原文、工具表、图像字节、同一作业 ID、终态及物理结果。该替身按脚本作答，无法衡量视觉模型自主判断抓取的准确性。可直接审阅从模型 HTTP 请求和状态响应截取的[首轮请求与作业快照](../../../../dsh/test/snapshots/ego-real-workstation.json)，其中测试专用的观察 MCP 额外贡献一个工具。

```sh
PYTHONPATH=src python3 -m pytest --noconftest -q tests/test_real_ego.py
(cd dsh && pnpm install --frozen-lockfile && pnpm run build && node --test test/ego-real.test.mjs)
```

## 接手时先读这四处

| 责任 | 入口 | 修改时要保留的行为 |
|---|---|---|
| EH/EA | [profile patch](../../../../dsh/packages/dsh-bundle-ego-real/cordis.patch.yml) | persona 属于 system，任务来自操作员。内置六个工位工具。新增观察 MCP 可用部署 patch 挂载；检查模型的真实工具表和系统指令。 |
| 工具 → ER | [工具与准入检查](../../../../dsh/packages/dsh-tool-ego-real/src/index.ts)、[HTTP 路由](api.py) | 基于同一个 provider 的 endpoint 和 owner。模型只提交 operation、frame_id、receipt 或 job；`skill_run` 总是异步。 |
| 作业与独占控制 | [runtime.py](runtime.py) | 准入绑定本次租约、观测帧、动作版本和期限。启动时再查一次；同凭据重试返回原 job。策略推理在锁外，发送前在锁内检查 generation。停止和失联都会撤销旧 generation。 |
| 策略与设备 | [device.py](device.py) | 按 operation ID 注入执行函数。策略通过可撤销的回调发送动作，并通过每个 job 独有的取消事件及时退出模型等待。即使策略在停止后才返回，发送端仍独立检查 generation。驱动的发送、就绪查询、停止确认必须在已知上界内返回。设备反馈确认保持失败时锁定故障；现场恢复流程需在接驱动时定义。 |

`GET /ego/summary` 返回设备模式、故障、活动 job 和目录；`POST /ego/observe` 返回真实采集时间语义的帧及反馈，fake 模式明确标记合成数据。`GET /ego/frame?frame_id=…` 供本机观察 MCP 在有效期内重新读取原帧。当前保留最近 16 帧，默认有效期 30 秒。job 完成时分别记录退出原因、保持确认和物理结果；预算耗尽且没有独立证据时，`physical_result` 为 `unknown`。租约、准入、帧和最近 128 份作业记录为进程内状态。DSH 会话保存在独立 home。

增加程序技能时，同时增加目录条目及对应的执行函数，在测试里检查停止后迟到的发送。只读感知可参考[示例 MCP](../../../../dsh/test/fixtures/real-frame-mcp.mjs)，它通过给定 frame_id 取图并返回摘要。当前观察由 EA 按需触发。后台主动监控可调用同一观测接口并引用 job_id 和 generation；持续采样、异常事件与会话唤醒仍待实施。

## 现场适配的核对依据

- 小 agent 参考提交 `fd5c837` 的 `robot_agent/cosmos_backend.py` 把实际循环委托给 `cosmos_framework.inference.causal_action.piper_protocol.run_loop`，下传 `hardware.send_action` 和紧停检查。协议握手校验 v2、joint 模式、每臂 6 自由度和执行步数。需核实现场版本的所有输出路径，包括初始化、队列、收尾及夹爪保持。
- `cobot_magic` 提交 `172721f` 提供 Piper CAN SDK 和 ROS 采集代码。`collect_data/inference_web.py` 指向仓库外的 `/home/agilex/models/lingbot-va-piper/client`；`collect_data/cosmos_web.py` 是 checkpoint 清单服务。小 agent 所需的 `cosmos_inference_config.json` 与仓库中另一个 `inference_config.json` 对应不同任务。
- 核对部署机的 Cosmos 客户端、merged 驱动、配置与一次成功抓取的日志后，先读取实际相机和编码器，执行输出仅进记录器。原初始化包含 enable、切换模式以及部分模式的关节写入，不能用作只读采样入口。
- 真机放行采用操作准入。部署验收需量测停止到停稳的时间、核对驱动队列与双臂持物反馈，验证宿主失联时的动作，并在没有旧 agent 路径或 editable install 的环境重跑。现有数值期限与 fake 停止反馈只用于软件验收。
