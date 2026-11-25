# 真机 profile 与 skills

本 bundle 的默认配置服务于独立工位运行时；真机控制台通过 [RealChat.patch](../../../src/steward/ego/real/console/chat.py) 选择 motion provider 和原生工具，同时开启 Harness 的 `tool-skill` 和一个只读取本包技能快照的 `skill-filesystem` provider。普通工位 profile 的技能加载开关保持独立。

`skills/<name>/SKILL.md` 是真机技能的唯一维护入口。控制台连接机器人时，[SkillCatalog](../../../src/steward/ego/real/console/skills.py) 校验并建立这些文件的快照；模型 provider 和网页编辑器使用同一份快照，网页保存可在空闲时替换快照。首次模型请求由原生 Harness 发布技能名称和描述，模型通过 `skill({name})` 按需加载全文；目录和工具结果进入会话日志。技能规程不增加设备权限，执行仍通过已有 tools → `ctx.ego` → provider → 驱动，不启用 shell、文件操作工具或代码执行。

[manhattan-navigation](skills/manhattan-navigation/SKILL.md) 分解 X 后 Y 的途经点导航，规则对应参考导航客户端 `navigate_to_manhattan` 的 hard 模式：拐点为目标 X、起点 Y，拐点朝向为下一段 Y 的方向，任一轴重合时直接发送最终位姿。没有工厂封装的固定中心点、安全区后退或目标收缩重试，也不依赖参考代码安装路径。现有工具只保证目标位姿验收，实际绕障轨迹仍由 NAVIS 规划。具体步骤、默认超时和容差只在 skill 正文维护。

## 添加真机 skill

1. 在 `skills/` 下新增一个目录及 `SKILL.md`，目录名与 YAML frontmatter 的 `name` 一致，使用最多 64 字符的小写字母、数字和连字符；`description` 写清适用任务，正文不能为空。这里的规程供模型调用，不设置 `disable-model-invocation: true`。
2. 正文声明已有工具、输入来源、单位、执行顺序、成功判据和失败出口。仅用当前真机 profile 的工具；缺少能力时先按[工具注册流程](../dsh-tool-ego-real/README.md#添加真机工具的流程)接入工具。地点、物料坐标等现场数据放 task context，设备地址放机器人配置。
3. 不另加 Python 名称列表、前端卡片或系统提示词全文。目录扫描自动产生网页目录和模型目录，网页根据同一个名称按需显示完整文本。当前只支持一级目录内的自包含 `SKILL.md`；脚本及其他资源不自动复制，不能把它们当作已安装能力。
4. 新增文件或外部修改完成静态和行为验证后，断开并重新连接机器人配置，或重启控制台，加载新快照。新建聊天会话保留当前机器人连接及其技能快照，不负责热更新。执行过程中修改源文件不会替换正在使用的规程。
5. 验证模型请求含技能摘要、`skill` 加载正文、工具调用遵循规则，以及失败后不执行依赖动作；同时检查网页目录和全文一致、新增第二个技能也能自动发现。技能不是确定性的路径执行器；必须由运行时强制的限速、互斥、参数校验和停止机制不能只写在技能文本里。

## 验证

从仓库根目录使用安装了 `real` 依赖的 Python 3.11+：

```sh
PYTHONPATH=steward/src python3 -m pytest -q steward/tests/test_real_skills.py steward/tests/test_real_console.py steward/tests/test_real_motion.py
STEWARD_PYTHON=python3 node --test steward/dsh/test/ego-motion.test.mjs
```

录制测试使用假模型和假设备，验证原生发现、正文加载、工具可见性及会话记录。仅在有意进行真实模型评估且已配置 `DEEPSEEK_API_KEY` 时运行以下检查；它调用真实模型，但设备 HTTP 服务全部是隔离的模拟接口，不连接真实底盘：

```sh
STEWARD_TEST_REAL_SKILL=1 STEWARD_PYTHON=python3 node --test steward/dsh/test/ego-manhattan-live.test.mjs
```

评估覆盖正负 Y 方向、同 X、同位置但需朝向调整、首段失败、定位无效和仅规划任务。通过这些检查不等于已经完成真机路径验收。

[factory-wam-operation](skills/factory-wam-operation/SKILL.md) 保存三个物料的采集及袋装纸盒、袋装蓝线归还的精确 prompt 与 chunk 预算，并通过独立 `arms_zero` 工具处理归还操作成功返回后的归零。黑色盒子归还未支持。WAM 工具只执行通用参数，不包含任务识别；位置仍来自 task context。该目录同样由 Harness 摘要发现、`skill` 正文加载和控制台展示共用，无需另建注册表。

控制台支持直接编辑并保存已有 skill 的完整文件。`SkillCatalog.save` 校验正文和 frontmatter、比较编辑基线与磁盘内容，并原子替换源文件；`RealChat.save_skill` 在任务锁内拒绝忙碌时修改。成功后下一轮重建原生技能目录快照，并在正常会话日志中写入技能版本更新提示，要求模型重新加载。文件名和 frontmatter 的 `name` 不允许通过网页重命名。新增文件或外部修改仍需重新连接配置；网页冲突和保存错误保留草稿供核对。
