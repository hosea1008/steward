---
kind: upgrade-guide
description: "The real robot console requires motion configuration and cancels its own tasks on disconnect."
---
# Real console motion configuration

English | [中文](guide.zh.md)

## Change

The Steward real console adds DeepSeek operator conversations and parameterized navigation and lift tools. The agent directly calls registered tools; `run_code` is unavailable. Custom robot JSON files must include navigation credential references and lift device settings. Disconnecting the console now cancels its own running tasks before releasing connections; it previously only released observation subscriptions. The existing workstation `ego-real` launcher remains separate.

## Migration

1. Add `navigation.username`, `navigation.password_env`, and `lift.port`, `lift.motor_id`, `lift.serial_timeout_s` to each robot configuration, following the [deployment example](../../../../steward/src/steward/ego/real/console/configs/agilex-cobot-magic.json). Keep the password in the named environment variable, outside version control.
2. Install the Python `real` extra. From the repository root run `STEWARD_REAL_HOME="$PWD/steward/var/dsh/ego-real-console" steward/scripts/setup-real-ego-profile.sh` to initialize the console's isolated DSH home. The [console entry point](../../../../steward/src/steward/ego/real/console/__main__.py) accepts `DEEPSEEK_API_KEY` or `--key-file` for conversations.
3. Start with `--no-actuation`, load the configuration and send a read-only request to `ego_state`. Verify the cameras, conversation and returned limits before starting without that flag for operator-directed motion. `home` is an explicit physical downward movement; loading a configuration does not home the lift.
