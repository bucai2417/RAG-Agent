/**
 * 侧边栏导航配置（分组 + 叶子项）。
 * icon 用 Element Plus 图标的全局注册名，模板里通过 <component :is="icon" /> 渲染。
 * perm 缺省时由 router/routeMeta.ts 的路由表兜底。
 *
 * 组织方式：顶层放高频入口（工作台/问答/聊天/知识库），其余按**功能域**分组：
 *   业务应用（面向业务同学的日常事务）
 *   智能体（AI 能力本身的配置与运行）
 *   渠道与集成（外部系统的接入与通知）
 *   高级/开发者、组织权限、系统设置、安全与审计
 */
export interface NavItem {
  key: string
  icon: string
  label: string
  perm?: string
  tip?: string
}
export interface NavGroup {
  key: string
  icon: string
  label: string
  children: NavItem[]
}

export const NAV: (NavItem | NavGroup)[] = [
  { key: '/dashboard', icon: 'Odometer', label: '工作台', tip: '概览、趋势与快捷入口' },
  { key: '/chat', icon: 'ChatDotRound', label: '智能问答', tip: '向知识库提问，支持引用溯源、多模型、图片/文件' },
  { key: '/chat-room', icon: 'ChatLineSquare', label: '企业聊天', tip: '全员大群 / 群聊 / 私聊，支持 @机器人、附件、撤回置顶' },
  { key: '/kb', icon: 'Collection', label: '知识库', tip: '上传 / 录入资料，构建可检索的知识' },
  {
    key: 'g-biz',
    icon: 'Briefcase',
    label: '业务应用',
    children: [
      { key: '/service-tickets', icon: 'Headset', label: '客服工单', perm: 'service:read', tip: '处理外部渠道进来的客户会话与工单' },
      { key: '/records', icon: 'Document', label: '智能录单', perm: 'record:read', tip: '用 AI 从文本中抽取结构化成表单数据' },
      { key: '/todo', icon: 'Calendar', label: '待办日程', tip: '待办事项与日程提醒' },
      { key: '/approvals', icon: 'Stamp', label: '审批中心', perm: 'workflow:read', tip: 'AI 发起写操作时的待人工确认项' },
      { key: '/mail', icon: 'Message', label: '邮件', tip: '个人邮箱收发，AI 可代管' },
    ],
  },
  {
    key: 'g-agent',
    icon: 'Cpu',
    label: '智能体',
    children: [
      { key: '/agents', icon: 'MagicStick', label: '智能体', perm: 'agent:read', tip: '可自定义角色 / 工具 / 知识库范围的 AI 助手' },
      { key: '/admin/persona-templates', icon: 'CollectionTag', label: '人设模板', perm: 'agent:read', tip: '预置的人设（系统提示词）素材，配置智能体或行为模式时可一键套用' },
      { key: '/skills', icon: 'Grid', label: '技能', perm: 'skill:read' },
      { key: '/admin/tools', icon: 'Tools', label: '工具', perm: 'tool:read' },
      { key: '/admin/mcp', icon: 'Connection', label: 'MCP 服务器', perm: 'mcp:read' },
      { key: '/admin/models', icon: 'Setting', label: '模型管理', perm: 'model:read', tip: '配置 Provider 与 chat / embedding / rerank 模型' },
      { key: '/scheduled', icon: 'AlarmClock', label: '定时任务', perm: 'schedule:read' },
    ],
  },
  {
    key: 'g-integration',
    icon: 'Promotion',
    label: '渠道与集成',
    children: [
      { key: '/channels', icon: 'Promotion', label: '外部渠道', perm: 'channel:read', tip: '接入 QQ / 微信 / 企业微信 / 飞书' },
      { key: '/admin/channel-users', icon: 'Switch', label: '渠道身份绑定', perm: 'channel:read' },
      { key: '/admin/notify-channels', icon: 'Bell', label: '通知渠道', perm: 'notify:read' },
      { key: '/admin/event-subscriptions', icon: 'Bell', label: '事件订阅', perm: 'notify:read' },
      { key: '/admin/email-sources', icon: 'Message', label: '邮件入库', perm: 'kb:read' },
    ],
  },
  {
    key: 'g-dev',
    icon: 'DataAnalysis',
    label: '高级 / 开发者',
    children: [
      { key: '/retrieval', icon: 'Search', label: '检索调试', tip: '查看检索命中的分块与分数' },
      { key: '/eval', icon: 'DocumentChecked', label: '问答评测', perm: 'eval:read', tip: '用测试问题集批量评估回答质量' },
    ],
  },
  {
    key: 'g-org',
    icon: 'UserFilled',
    label: '组织权限',
    children: [
      { key: '/admin/users', icon: 'User', label: '用户', perm: 'user:read' },
      { key: '/admin/roles', icon: 'Avatar', label: '角色', perm: 'role:read' },
      { key: '/admin/depts', icon: 'OfficeBuilding', label: '部门', perm: 'dept:read' },
      { key: '/admin/groups', icon: 'UserFilled', label: '用户组', perm: 'group:read' },
      { key: '/admin/tenants', icon: 'OfficeBuilding', label: '组织', tip: '多组织、配额与邀请入组' },
    ],
  },
  {
    key: 'g-system',
    icon: 'Setting',
    label: '系统设置',
    children: [
      { key: '/admin/settings', icon: 'Setting', label: '系统设置', perm: 'system:manage' },
      { key: '/admin/browser', icon: 'Monitor', label: '浏览器', perm: 'tool:invoke' },
      { key: '/admin/system-ops', icon: 'Coin', label: '数据库与运维', perm: 'system:manage' },
      { key: '/admin/api-keys', icon: 'Key', label: 'API 密钥', perm: 'apikey:read' },
      { key: '/admin/sso', icon: 'Lock', label: '单点登录', perm: 'sso:read' },
    ],
  },
  {
    key: 'g-govern',
    icon: 'FolderOpened',
    label: '安全与审计',
    children: [
      { key: '/admin/security', icon: 'Stamp', label: '内容安全', perm: 'audit:read' },
      { key: '/admin/audit', icon: 'DocumentChecked', label: '审计日志', perm: 'audit:read' },
      { key: '/admin/logs', icon: 'Tickets', label: '系统日志', perm: 'system:read' },
      { key: '/admin/usage', icon: 'DataLine', label: '用量统计', perm: 'model:read' },
      { key: '/admin/files', icon: 'FolderOpened', label: '文件管理', perm: 'file:manage' },
    ],
  },
]
