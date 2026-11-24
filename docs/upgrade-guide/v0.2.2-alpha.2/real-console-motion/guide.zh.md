---
kind: upgrade-guide
description: "真机控制台需要运动配置，并在断开连接时取消自身发起的任务。"
---
# 真机控制台运动配置

[English](guide.md) | 中文

## 变更

Steward 真机控制台增加 DeepSeek 操作员对话及参数化导航、升降工具。Agent 直接调用已注册工具，不提供 `run_code`。自定义机器人 JSON 文件需要增加导航凭据引用和升降设备设置。断开控制台会先取消自身正在执行的任务再释放连接；此前只释放观测订阅。现有工位 `ego-real` 启动器仍独立使用。

## 迁移

1. 按照[部署示例](../../../../steward/src/steward/ego/real/console/configs/agilex-cobot-magic.json)，为每份机器人配置增加 `navigation.username`、`navigation.password_env` 和 `lift.port`、`lift.motor_id`、`lift.serial_timeout_s`。密码保存在指定环境变量中，不进入版本管理。
2. 安装 Python 的 `real` 可选依赖。从仓库根目录运行 `STEWARD_REAL_HOME="$PWD/steward/var/dsh/ego-real-console" steward/scripts/setup-real-ego-profile.sh`，初始化控制台独立的 DSH home。[控制台启动入口](../../../../steward/src/steward/ego/real/console/__main__.py) 通过 `DEEPSEEK_API_KEY` 或 `--key-file` 接收对话凭据。
3. 先以 `--no-actuation` 启动，加载配置并发送仅调用 `ego_state` 的只读请求。检查相机、对话和返回的限速后，再去掉该参数启动，由操作员明确下达运动任务。`home` 是显式向下运动归零；加载配置不会归零升降柱。
