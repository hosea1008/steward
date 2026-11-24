/** Chinese copy owned by the standalone real-robot console. */
export const zh = Object.freeze({
  title: '真机控制台', configuration: '真机配置', connect: '加载并连接', disconnect: '断开连接',
  local: '真机控制台', workspace: 'ROBOT WORKSPACE', readOnly: '观测与连接检查',
  cameras: '实时相机', connections: '设备与接口', connectionHint: '连接状态不代表设备已使能',
  configurationDetails: '查看配置与接口地址', debugLogs: '运行日志', logLevel: '最低日志级别',
  filter: '筛选日志…', follow: '跟随最新', operator: '操作员对话', robotOnly: 'Ego robot',
  agentPending: 'Agent 待接入', welcomeTitle: '连接你的真机',
  welcomeBody: '选择配置并加载，查看实时画面与连接检查记录。这里仅保留机器人对话，不包含 World Harness。',
  chatHint: 'Agent 对话及运动工具尚未接入。当前可检查连接和查看画面；不会执行导航或机械臂动作。',
  message: '操作员消息', messagePlaceholder: '输入给机器人的任务…', send: '发送',
  draftHint: '可编辑任务草稿，Agent 接入后才能发送', footer: 'Steward · 真机观测控制台',
  idle: '未连接', connecting: '正在连接', connected: '已连接', disconnecting: '正在断开', error: '连接失败',
  live: '实时', offline: '未出图', waiting: '等待连接', stale: '画面不可用',
  receiving: '路画面正常', sample: '画面刷新', hz: '帧/秒', transportLost: '控制台服务连接中断，正在重试',
  emptyLogs: '暂无符合条件的日志', startup: '连接检查', system: '系统', details: '详情',
  available: '接口存在', reachable: '端口可达', unavailable: '不可达', unknown: '未检查',
  runtime: 'Ego Runtime', runtimeUnknown: '尚未检查 ER', runtimeUnavailable: 'ER 不可达',
  real: '真机', fake: '假设备', runtimeHint: '仅查看状态；未接入控制工具',
  agent: 'Agent 工具', notRegistered: '未接入', agentDetail: '导航与 WAM 工具待接入',
  loadFirst: '加载配置后检查设备与相机', requestFailed: '请求失败', updated: '状态更新',
  pendingMessage: 'Agent 尚未接入，草稿未发送', cameraHint: '仅显示上游报告正常且新鲜的画面',
});
export function t(key) { return zh[key] ?? key; }
