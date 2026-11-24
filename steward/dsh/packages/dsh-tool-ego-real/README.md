# Real robot tools

The default export and `./provider` implement the workstation admission API used by the native `ego-real` profile. `./motion` registers `get_navigation_state`, `navigate_to_pose`, `lift_move`, `clear_motion_fault`, `ego_state` and `body_stop` for the real console's native overlay. The model calls existing tools directly; registrations use the normal guarded tool pipeline and are disposed with their plugin.

The console overlay loads `./motion-provider` as the `ctx.ego` provider and `./motion` as its tool consumer. Only the provider receives `endpoint`, a private `token`, and `timeoutMs`; the tool plugin has no connection configuration. The provider exposes the optional `EgoMotion` and `EgoNavigation` interfaces declared in `@steward/dsh-ego`, plus cached `summary` and turn-scoped `stop`. It reuses the Ego HTTP client base, but Motion does not implement the base client's other Ego API endpoints. The overlay disables the workstation provider and tools and does not load the simulation bundle. A provider registration alone exposes no model tools.

The console owns the loopback Motion runtime and passes connection settings in its per-turn overlay; neither the token nor device credentials are model parameters. Request aborts close the HTTP call, allowing the runtime to cancel the operation. An ambiguous transport failure must not be retried automatically. Tool results contain an execution `state`; the agent must check it before continuing dependent actions. The Motion runtime and the workstation Runtime remain separate execution owners; using `ctx.ego` does not merge their locks, jobs or control leases.

The [console guide](../../../src/steward/ego/real/console/README.md) owns deployment, parameter units, fixed speed limits, explicit homing, stop guarantees and live-test restrictions. Motion does not depend on a workstation camera admission or enable WAM manipulation. No material names, target coordinates or factory routes are built into these tools.

`clear_motion_fault` calls `ctx.ego.motion.clearFault`, implemented by the provider's private `/clear-fault` request. The runtime requires the current `fault_id`, an explicit operator stop/inspection acknowledgement and a reason. It atomically rejects stale identities and unfinished operations, preserves failure evidence, and logs the clearance. No device transport is opened; hardware faults and lift origin are untouched. Clearance does not reset cancellation, enable actuation or retry a failed operation. The confirmation is operator-supplied information, not independent sensor evidence. See the console guide for operator wording and recovery limits.

Build with `pnpm --dir steward/dsh run build` from the repository root. The recorded `test/ego-motion.test.mjs` scenario runs the production overlay against mock model and motion endpoints; `test/ego-motion-service.test.mjs` checks provider substitution, missing capabilities, cancellation and registration disposal; `test/ego-real.test.mjs` covers the separate workstation path.

## 添加真机工具的流程

调用路径固定为：模型工具 → `ctx.ego` 服务接口 → 真机 provider → Python 运行时 → 设备驱动。模型工具只依赖服务定义，不能直接请求 HTTP、打开设备、读取设备凭据或用 `instanceof` 依赖某个具体 provider。Task context 中的物料位置等资料也不属于工具实现。

1. **定义服务能力。** 在 [dsh-ego 服务定义](../dsh-ego/src/index.ts) 中声明参数、返回值和取消语义。导航和升降使用可选的 `EgoMotion` 能力，只读导航查询使用独立的可选 `EgoNavigation`；其他 provider 未实现该能力时不应伪造支持。工具插件必须在注册前检查所需能力，缺失时明确报错。新的能力不应为了复用接口而冒充 ESim 预测。
2. **实现运行时和驱动。** 设备协议放在 `steward/src/steward/ego/real/drivers/`，地址来自设备配置，目标来自入参。在 `motion.py` 中完成参数校验和执行分派，复用当前的动作串行化、取消、超时和故障处理。只读查询使用独立连接及取消生命周期；查询失败或取消只关闭查询连接，不停止设备、不修改运动故障或覆盖动作结果，并允许故障锁定和禁用运动时诊断。设备访问前拒绝无效参数，固定限速不交给模型修改。明确何时可报告成功、停止有哪些反馈，以及通信失败后哪些结果未知。打开设备不能自动运动。
3. **实现 provider。** 在 [motion-provider.ts](src/motion-provider.ts) 中实现服务方法，将业务参数转换成运行时请求。地址、私有 token、HTTP 路径和传输超时只放在这一层；逐层传递 `AbortSignal`，不自动重试结果不确定的动作。需要新增运行时能力时同时更新服务定义，不能让工具绕过接口补发请求。
4. **注册模型工具。** 在 [motion.ts](src/motion.ts) 中声明 `inject: ['ego', 'tools']`，通过 `ctx.effect(() => ctx.tools.register(...))` 注册和释放工具。执行函数只调用 `ctx.ego` 暴露的方法。描述写清用途、物理效果、前提、完成证据和失败处理；参数说明写清单位、坐标系、范围、必填项和互斥关系。Harness 自动把描述和参数定义送入模型请求，代码注释不会替代工具描述。
5. **装配与展示。** 控制台的 [RealChat.patch](../../../src/steward/ego/real/console/chat.py) 为当前 Harness 实例选择一个 Ego provider 和明确的工具插件。不要同时加载仿真工具包或恢复代码执行。网页目录仍从 `Motion.state().tools` 读取，新增动作需要同步该列表；需要中文展示时补充控制台本地化文案。当前流程并非跨 Python、TypeScript 和网页的一处自动注册。
6. **验证。** 用替代的 `ctx.ego` 实现验证工具不依赖具体 provider；验证能力缺失、取消传递、注册释放和运行时参数拒绝。运行录制会话测试，检查模型收到的工具清单与参数、执行结果和对话展示，确保没有仿真工具混入。驱动和运行时测试使用假设备，不用真实运动代替回归测试。同步更新本说明和控制台操作说明中的相关前提。

在 `steward/dsh` 下运行 `pnpm run build`，再运行 `node --test test/ego-motion-service.test.mjs test/ego-motion.test.mjs test/ego-real.test.mjs`。测试环境的 PATH 需要包含 pnpm 和 Python 3.11+；控制台与运动运行时的 Python 回归测试位于 `steward/tests/test_real_console.py` 和 `steward/tests/test_real_motion.py`。

Task context is managed by the [console](../../../src/steward/ego/real/console/README.md#task-context-的保存和发送), not by individual tools. Each turn includes the saved snapshot as logged user data, including an explicit empty value after clearing it. Tool descriptions stay generic so the same tool can act on different operator-provided contexts.
