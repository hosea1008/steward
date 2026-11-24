# 真机控制台

控制台提供真机配置选择、三路实时相机、设备连接检查和 DEBUG 日志，默认监听 `0.0.0.0:8400`，本机访问 `http://127.0.0.1:8400/console`，其他设备访问 `http://机器人主机IP:8400/console`。连接检查与调试记录只显示在运行日志区。操作员面板允许编辑任务草稿；Agent 对话后端与导航、WAM 工具尚未接入，发送按钮禁用，消息接口明确拒绝发送。

## 启动

在仓库根目录使用 Python 3.11+ 启动；控制台本身只依赖 Python 标准库，不需要模型密钥或机器人 SDK。

```sh
PYTHONPATH=steward/src steward/.venv/bin/python -m steward.ego.real.console
```

打开 `/console`，选择 **Agilex Cobot Magic**，点击“加载并连接”。配置中的相机服务需要提前运行。默认配置使用本机 `5000` 端口的 Cobot Magic 相机服务；三路采集正常时显示头部及左右腕部画面。上游离线、健康检查失败或画面超时会显示不可用遮罩，不会把占位图当作实时观测。关闭服务使用 Ctrl+C。

“断开连接”只停止控制台的画面读取和健康检查，不停止相机服务，不取消机器人作业，也不是急停。机械臂启动检查仅读取本机 CAN 接口是否存在；导航启动检查仅连接端口，不登录、不初始化导航、不使能或移动机械臂。ER 状态独立展示，假设备会标明；ER 不可达不影响其他画面。

## 配置与型号适配

[默认配置](configs/agilex-cobot-magic.json) 保存型号、名称、相机 topic 与 HTTP 地址、CAN 设备名、导航 HTTP/WebSocket 地址、地图、ER 地址和刷新期限。服务启动时校验配置，未知型号、重复 ID 和无效地址会直接报错。部署文件不能指定任意可执行命令；新增型号需要在 [adapters.py](adapters.py) 中实现并注册对应的启动函数。

自定义配置目录通过 `--config-dir /绝对路径` 指定，其中每个 JSON 文件是一份配置；修改后重启控制台。切换配置前先断开当前连接。`--port` 可覆盖默认端口，`--host` 可覆盖监听地址，例如 `--host 127.0.0.1` 限制为本机访问。此控制台没有登录认证，应部署在可信设备网络中。

当前相机适配从 `snapshot_url` 按 `refresh_fps` 读取各相机 JPEG，并使用 `camera_status_url` 的 `topics[status_key]` 检查上游是否仍在采集。网页读取控制台缓存，多个浏览器共享上游采集线程。`stream_url` 保留现场服务的视频入口；默认现场服务提供三路合并的 MJPEG，因此三路配置使用同一流地址、不同快照地址。页面的“实时”表示快照接收及上游健康检查均未超过配置期限，不提供相机硬件采集时间戳保证。

所有页面、服务、配置和型号适配均在本目录。`service.py` 持有连接、采集线程和有界事件缓存；`server.py` 只提供静态文件、配置、观测状态和连接管理接口。事件包含时间、级别、来源与可展开的详情；日志与任务草稿不跨服务或页面重启持久化。接入 Agent 时应消费实际会话事件，并将运动请求交给 ER，不能用连接成功代替动作完成反馈。

## 验证

```sh
PYTHONPATH=steward/src python3 -m pytest --noconftest -q steward/tests/test_real_console.py
node --check steward/src/steward/ego/real/console/static/app.js
```

测试使用临时 HTTP 服务，不连接真机，覆盖配置拒绝、加载与断开的并发、旧配置帧拒绝、上游离线、ER 模式区分及未接入 Agent 时的消息拒绝。
