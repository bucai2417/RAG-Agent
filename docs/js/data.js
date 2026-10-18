/* 演示站数据：全部为本地模拟样本，不涉及真实企业数据，刷新后重置 */
window.DEMO = (function () {
  'use strict';

  var kbs = [
    { id: 'kb-hr', name: '人事制度库', type: 'vector', docs: 48, chunks: 1263, size: '36.2 MB', visibility: 'internal', owner: '人力资源部', updated: '2026-10-16', embedding: 'bge-m3 · 1024', health: [] },
    { id: 'kb-fin', name: '财务与报销库', type: 'vector', docs: 31, chunks: 842, size: '21.7 MB', visibility: 'internal', owner: '财务部', updated: '2026-10-15', embedding: 'bge-m3 · 1024', health: ['2 个文档解析失败'] },
    { id: 'kb-prod', name: '产品与售前资料', type: 'vector', docs: 96, chunks: 3140, size: '118.4 MB', visibility: 'public', owner: '产品中心', updated: '2026-10-17', embedding: 'text-embedding-3-small · 1536', health: [] },
    { id: 'kb-img', name: '设备维修图文库', type: 'image', docs: 214, chunks: 214, size: '612.8 MB', visibility: 'internal', owner: '运维部', updated: '2026-10-14', embedding: '关键词检索 · 无需向量模型', health: ['3 个文档无标签'] },
    { id: 'kb-legal', name: '合同与合规模板', type: 'vector', docs: 22, chunks: 507, size: '9.4 MB', visibility: 'private', owner: '法务部', updated: '2026-10-11', embedding: 'bge-m3 · 1024', health: ['5 个文档超过 90 天未更新'] },
    { id: 'kb-dify', name: '集团 Dify 外部库', type: 'external', docs: '—', chunks: '—', size: '—', visibility: 'internal', owner: 'IT 中心', updated: '2026-10-17', embedding: '联邦调用 · Dify / RAGFlow', health: [] }
  ];

  var typeName = { vector: '向量知识库', image: '图文知识库', external: '外部知识库' };
  var visName = { public: '全租户可见', internal: '按成员', private: '仅显式成员' };

  var docs = {
    'kb-hr': [
      { id: 'd1', name: '员工手册 2026 版.pdf', chunks: 126, tokens: 41230, status: 'ok', tags: ['制度', '全员'] },
      { id: 'd2', name: '考勤与请假管理办法.docx', chunks: 34, tokens: 9860, status: 'ok', tags: ['考勤'] },
      { id: 'd3', name: '绩效与晋升实施细则.pdf', chunks: 58, tokens: 19740, status: 'ok', tags: ['绩效'] },
      { id: 'd4', name: '远程办公申请流程.md', chunks: 6, tokens: 1420, status: 'ok', tags: ['办公'] }
    ],
    'kb-fin': [
      { id: 'd5', name: '差旅报销标准 V3.pdf', chunks: 41, tokens: 12400, status: 'ok', tags: ['报销'] },
      { id: 'd6', name: '发票开具与红冲指引.docx', chunks: 27, tokens: 8100, status: 'fail', tags: ['发票'] },
      { id: 'd7', name: '采购付款审批权限表.xlsx', chunks: 12, tokens: 3300, status: 'ok', tags: ['采购'] }
    ],
    'kb-prod': [
      { id: 'd8', name: 'RAG-Agent 部署白皮书.pdf', chunks: 88, tokens: 27600, status: 'ok', tags: ['部署'] },
      { id: 'd9', name: '标准报价单 2026Q4.xlsx', chunks: 9, tokens: 2400, status: 'ok', tags: ['报价'] },
      { id: 'd10', name: '常见问题 FAQ.md', chunks: 22, tokens: 6100, status: 'ok', tags: ['FAQ'] }
    ],
    'kb-img': [
      { id: 'd11', name: '1# 注塑机换模作业指导', chunks: 1, tokens: 320, status: 'ok', tags: ['设备'] },
      { id: 'd12', name: '空压机报警代码 E12 处置', chunks: 1, tokens: 180, status: 'ok', tags: ['设备'] }
    ],
    'kb-legal': [
      { id: 'd13', name: '技术服务合同模板.docx', chunks: 19, tokens: 5900, status: 'ok', tags: ['模板'] },
      { id: 'd14', name: '数据出境合规要点.pdf', chunks: 24, tokens: 7300, status: 'stale', tags: ['合规'] }
    ],
    'kb-dify': []
  };

  /* 检索语料：用于本地简化的向量/BM25 相似度计算 */
  var chunks = [
    { id: 'c1', kb: 'kb-hr', doc: '考勤与请假管理办法.docx', text: '员工请假流程与年假计算：员工年假按入职年限计算，满 1 年不满 10 年每年 5 天，满 10 年不满 20 年 10 天。请假需提前 3 个工作日在待办日程提交申请，由直属主管审批，主管 24 小时未处理自动升级至部门负责人。' },
    { id: 'c2', kb: 'kb-hr', doc: '考勤与请假管理办法.docx', text: '病假 1 天以内可事后补交，需提供就医凭证；连续 3 天以上病假须提前报备并附二级医院证明。产假按国家与地方规定执行，生育津贴由人力资源部统一申领。' },
    { id: 'c3', kb: 'kb-hr', doc: '员工手册 2026 版.pdf', text: '工作时间实行弹性打卡，核心时段 10:00-16:00，每日满 8 小时。迟到 30 分钟以内不计异常，每月累计 3 次以内免予处理，超出部分按考勤异常流程处理。' },
    { id: 'c4', kb: 'kb-hr', doc: '远程办公申请流程.md', text: '远程办公每周不超过 2 天，需在系统提交申请并经直属主管与部门负责人双审批。远程期间须保持企业聊天与电话可达，重要会议须返回工位参会。' },
    { id: 'c5', kb: 'kb-fin', doc: '差旅报销标准 V3.pdf', text: '出差住宿标准（按城市分级的每晚上限）：上海、北京、深圳等一线城市 500 元/晚，杭州、成都等新一线 400 元/晚，其他城市 300 元/晚。市内交通实报实销，打车需注明事由。超标部分需部门负责人书面审批。' },
    { id: 'c6', kb: 'kb-fin', doc: '差旅报销标准 V3.pdf', text: '报销单须在返程后 15 个自然日内提交，逾期需财务总监审批。单笔 5000 元以上走对公付款流程，员工垫付仅限 2000 元以下小额支出。' },
    { id: 'c7', kb: 'kb-fin', doc: '发票开具与红冲指引.docx', text: '增值税专用发票需在开票后 360 日内完成认证。发票信息错误需红冲重开，红冲需上传原发票与说明材料，由财务部统一操作。' },
    { id: 'c8', kb: 'kb-prod', doc: 'RAG-Agent 部署白皮书.pdf', text: '平台默认使用 SQLite 零依赖起步，后端统一监听 6677 端口并同时托管前端构建产物。切换 PostgreSQL 只需修改 DATABASE_URL 并安装 asyncpg 与 pgvector 驱动。' },
    { id: 'c9', kb: 'kb-prod', doc: 'RAG-Agent 部署白皮书.pdf', text: '混合检索由向量召回、BM25 关键词与 RRF 融合三路组成，权重可调，可选 rerank 精排与 MMR 去冗余。检索权限条件由 permission 服务唯一入口生成。' },
    { id: 'c10', kb: 'kb-prod', doc: '标准报价单 2026Q4.xlsx', text: '标准版授权 50 坐席起，含知识库、智能问答与工单模块；企业版增加 SSO 单点登录、审计与外部渠道接入；私有化部署版按节点数计价，含一年远程支持。' },
    { id: 'c11', kb: 'kb-prod', doc: '常见问题 FAQ.md', text: '中文关键词检索默认使用应用层按字分词，PostgreSQL 可启用 zhparser 扩展增强 BM25 效果。更换 embedding 模型后需重建知识库向量。' },
    { id: 'c12', kb: 'kb-prod', doc: '常见问题 FAQ.md', text: 'AI 生成 PDF 出现中文乱码或方块占位符，多为容器缺少 TrueType 轮廓中文字体所致。镜像内置 fonts-wqy-zenhei；使用 CFF 轮廓的 fonts-noto-cjk 会被 reportlab 拒绝，可通过 RAG_PDF_FONT 指定字体。' },
    { id: 'c13', kb: 'kb-img', doc: '空压机报警代码 E12 处置', text: '空压机报警代码 E12 表示排气温度过高。出现 E12 后的处理方法：先检查油位与冷却风扇，清洁散热器滤网，重启后仍报警需联系设备科。配套图片：滤网拆装示意图 3 张。' },
    { id: 'c14', kb: 'kb-legal', doc: '数据出境合规要点.pdf', text: '涉及个人信息的数据出境需通过安全评估或标准合同备案。集团内部跨境传输同样纳入评估范围，需法务与信息安全双签。' },
    { id: 'c15', kb: 'kb-legal', doc: '技术服务合同模板.docx', text: '技术服务合同默认验收期 15 个工作日，付款分三期：预付 30%、验收 60%、质保 10%。知识产权归属需在特别约定条款中单独明确。' },
    { id: 'c16', kb: 'kb-prod', doc: 'RAG-Agent 部署白皮书.pdf', text: '单点登录配置方法：在系统设置的安全页填入 OIDC 的 Client ID、Client Secret 与回调地址，可对接企业微信、钉钉与飞书；开启后首次登录按 JIT 规则自动建号并分配默认角色，已绑定账号继续继承原有角色。' }
  ];

  var tools = [
    { name: 'kb_search', desc: '知识库混合检索', group: '知识', builtin: true },
    { name: 'http_request', desc: '通用 HTTP 调用', group: '集成', builtin: true },
    { name: 'file_read', desc: '读取工作区文件', group: '文件', builtin: true },
    { name: 'file_write', desc: '写入工作区文件', group: '文件', builtin: true },
    { name: 'table_analyze', desc: '表格数据分析', group: '数据', builtin: true },
    { name: 'doc_generate', desc: '办公文档生成 DOCX/XLSX/PPTX/PDF', group: '办公', builtin: true },
    { name: 'todo_manage', desc: '待办与日程代建代查', group: '办公', builtin: true },
    { name: 'session_manage', desc: '会话管理与导出', group: '会话', builtin: true },
    { name: 'find_tools', desc: '按需路由工具，省 token', group: '智能体', builtin: true },
    { name: 'find_skill', desc: '从 GitHub 搜索并安装技能', group: '智能体', builtin: true },
    { name: 'script_run', desc: 'Python / Node / Shell 脚本运行', group: '运行时', builtin: true },
    { name: 'ticket_create', desc: '创建客服工单', group: '客服', builtin: true },
    { name: 'email_send', desc: '邮件收发（发信需确认）', group: '邮件', builtin: true },
    { name: 'chart_render', desc: 'ECharts 图表渲染', group: '数据', builtin: true },
    { name: 'web_fetch', desc: '外部网页抓取', group: '集成', builtin: true },
    { name: 'user_manage', desc: '用户与角色管理', group: '运维', builtin: false },
    { name: 'kb_admin', desc: '知识库与分块维护', group: '运维', builtin: false },
    { name: 'audit_query', desc: '审计日志查询', group: '运维', builtin: false },
    { name: 'workflow_run', desc: '工作流触发与回滚', group: '运维', builtin: false }
  ];

  var agents = [
    { id: 'a1', name: '通用助手', mode: '默认行为', model: 'deepseek-v4.1-flash', tools: ['kb_search', 'http_request', 'file_read', 'web_fetch'], temp: 0.4, runs: 1284, ok: 98.2, desc: '默认问答智能体，装配知识检索与联网工具' },
    { id: 'a2', name: '办公文书助理', mode: '公文模式', model: 'claude-sonnet', tools: ['doc_generate', 'todo_manage', 'kb_search', 'email_send'], temp: 0.6, runs: 431, ok: 96.5, desc: '生成纪要、周报、公文通知，排版含封面页眉目录' },
    { id: 'a3', name: '数据分析员', mode: '严谨模式', model: 'gpt-4.1', tools: ['table_analyze', 'chart_render', 'script_run'], temp: 0.2, runs: 188, ok: 94.1, desc: '读取 Excel 台账做统计与图表，脚本在受限运行时执行' },
    { id: 'a4', name: '客服一线', mode: '工单模式', model: 'deepseek-v4.1-flash', tools: ['kb_search', 'ticket_create', 'session_manage'], temp: 0.3, runs: 2760, ok: 99.1, desc: '外部渠道接入，无法作答时自动转人工建单' }
  ];

  var skills = [
    { name: 'oil-price', repo: '241793/skill-oil-price', desc: '查询国内成品油调价窗口与各省最新油价', installs: 312, lang: 'Python' },
    { name: 'invoice-ocr', repo: 'corp/skill-invoice-ocr', desc: '增值税发票识别并结构化入账', installs: 148, lang: 'Python' },
    { name: 'weekly-report', repo: 'corp/skill-weekly-report', desc: '汇总本周待办与提交生成周报 docx', installs: 407, lang: 'Node' },
    { name: 'sql-report', repo: '241793/skill-sql-report', desc: '按自然语言生成 SQL 并导出 xlsx 报表', installs: 96, lang: 'Shell' },
    { name: 'ppt-composer', repo: 'corp/skill-ppt-composer', desc: '按主题色自绘 PPTX 版式', installs: 231, lang: 'Python' }
  ];

  var wfNodes = [
    { id: 'n1', type: 'start', label: '开始 · 用户消息', x: 40, y: 150, cfg: { trigger: '外部渠道消息', vars: ['channel', 'user_id', 'text'] } },
    { id: 'n2', type: 'llm', label: '意图识别', x: 230, y: 150, cfg: { model: 'deepseek-v4.1-flash', prompt: '判断消息属于：咨询 / 报障 / 投诉 / 其他', temp: 0.1 } },
    { id: 'n3', type: 'switch', label: '多路分支', x: 420, y: 150, cfg: { cases: ['咨询', '报障', '投诉', 'default'] } },
    { id: 'n4', type: 'retrieval', label: '知识检索', x: 620, y: 40, cfg: { kb: '产品与售前资料', topK: 6, rerank: true } },
    { id: 'n5', type: 'http', label: '查设备台账', x: 620, y: 130, cfg: { method: 'GET', url: '/api/v1/device/ledger' } },
    { id: 'n6', type: 'agent', label: '协作智能体 · 客服一线', x: 620, y: 220, cfg: { agent: '客服一线', timeout: 60 } },
    { id: 'n7', type: 'human', label: '人工审批', x: 820, y: 220, cfg: { approver: '客服组长', action: '批准 / 拒绝 + 意见' } },
    { id: 'n8', type: 'parallel', label: '并行网关', x: 820, y: 60, cfg: { branches: 2, join: 'n9' } },
    { id: 'n9', type: 'notify', label: '消息推送', x: 1010, y: 140, cfg: { channel: '企业微信 + 站内', template: '工单已受理' } },
    { id: 'n10', type: 'end', label: '结束', x: 1190, y: 140, cfg: { output: 'ticket_id' } }
  ];
  var wfEdges = [['n1', 'n2'], ['n2', 'n3'], ['n3', 'n4'], ['n3', 'n5'], ['n3', 'n6'], ['n4', 'n8'], ['n5', 'n8'], ['n8', 'n9'], ['n6', 'n7'], ['n7', 'n9'], ['n9', 'n10']];

  var tickets = [
    { id: 'TK-2087', title: '报价单里的私有化部署价格能否含税开专票', customer: '恒信制造 · 陈经理', channel: '企业微信', priority: 'high', status: 'open', sla: '剩 1h12m', assignee: '李伟', created: '10-18 09:12', msgs: [
      { who: 'customer', name: '陈经理', text: '你们标准报价单 2026Q4 里的私有化部署版，价格能开增值税专用发票吗？需要 13% 税率。' },
      { who: 'ai', name: '客服一线', text: '私有化部署版可按 13% 开具增值税专用发票，需在合同中明确开票信息。发票认证需在开票后 360 日内完成。' },
      { who: 'customer', name: '陈经理', text: '那麻烦走个正式报价，另外希望付款分两期。' }
    ] },
    { id: 'TK-2086', title: '空压机报 E12 无法复位', customer: '第三车间 · 周工', channel: 'QQ 机器人', priority: 'urgent', status: 'pending', sla: '超时 18m', assignee: '设备科', created: '10-18 08:40', msgs: [
      { who: 'customer', name: '周工', text: '3 号空压机一直报 E12，重启两次没用。' },
      { who: 'ai', name: '图文知识库', text: 'E12 为排气温度过高报警。请先检查油位与冷却风扇、清洁散热器滤网。已发送《滤网拆装示意图》3 张。' },
      { who: 'agent', name: '设备科', text: '已派单，10:30 前到现场，先降负荷运行。' }
    ] },
    { id: 'TK-2085', title: '年假申请主管未审批', customer: '内部 · 张敏', channel: '站内', priority: 'low', status: 'resolved', sla: '已完成', assignee: 'AI 自动处理', created: '10-17 17:05', msgs: [
      { who: 'customer', name: '张敏', text: '我上周提的年假申请还没审批，怎么办？' },
      { who: 'ai', name: '通用助手', text: '年假需提前 3 个工作日提交，主管 24 小时未处理会自动升级至部门负责人。你的申请已升级，当前在部门负责人处。' }
    ] },
    { id: 'TK-2084', title: '差旅住宿超标被驳回', customer: '内部 · 刘洋', channel: '飞书', priority: 'medium', status: 'open', sla: '剩 5h40m', assignee: '未分派', created: '10-17 15:22', msgs: [
      { who: 'customer', name: '刘洋', text: '出差那晚只有 460 元的房间，报销被驳回了。' },
      { who: 'ai', name: '通用助手', text: '新一线城市住宿标准为 400 元/晚，超标部分需部门负责人书面审批后重新提交。' }
    ] },
    { id: 'TK-2083', title: '咨询 SSO 对接钉钉的支持情况', customer: '云启科技 · 吴总', channel: '微信', priority: 'medium', status: 'resolved', sla: '已完成', assignee: '李伟', created: '10-16 11:30', msgs: [
      { who: 'customer', name: '吴总', text: '你们支持钉钉登录吗？' },
      { who: 'agent', name: '李伟', text: '支持通用 OIDC 与企业微信/钉钉/飞书（OIDC + PKCE），首次登录可 JIT 建号并分配默认角色。' }
    ] },
    { id: 'TK-2082', title: 'PDF 导出中文显示为占位符', customer: '内部 · 孙悦', channel: '站内', priority: 'high', status: 'pending', sla: '剩 2h05m', assignee: '研发值班', created: '10-16 09:48', msgs: [
      { who: 'customer', name: '孙悦', text: 'AI 生成的 PDF 里中文全是 I、II 这种占位符。' },
      { who: 'ai', name: '通用助手', text: '多为容器缺少 TrueType 中文字体所致。镜像内置 fonts-wqy-zenhei；reportlab 不接受 CFF 轮廓的 fonts-noto-cjk，可用 RAG_PDF_FONT 指定字体。' }
    ] },
    { id: 'TK-2081', title: '外部知识库连接超时', customer: 'IT 中心 · 赵工', channel: '企业微信', priority: 'low', status: 'closed', sla: '已完成', assignee: '运维', created: '10-15 14:20', msgs: [
      { who: 'customer', name: '赵工', text: 'Dify 外部库检索经常超时。' },
      { who: 'agent', name: '运维', text: '已将联邦调用超时从 5s 调整为 12s，并在检索调试台补充了失败归因。' }
    ] },
    { id: 'TK-2080', title: '希望开放员工自助注册', customer: '内部 · 人力资源部', channel: '站内', priority: 'low', status: 'closed', sla: '已完成', assignee: '系统管理员', created: '10-15 10:02', msgs: [
      { who: 'customer', name: '人力资源部', text: '新员工想自己能注册账号。' },
      { who: 'agent', name: '系统管理员', text: '已在「系统设置 → 安全」开启自助注册并保留注册后管理员审核。' }
    ] }
  ];

  var members = [
    { name: '系统管理员', dept: 'IT 中心', role: '超级管理员', principal: 'USER=1e12+1', status: 'active', logins: 412 },
    { name: '李伟', dept: '客服中心', role: '客服坐席', principal: 'USER=1e12+27', status: 'active', logins: 1863 },
    { name: '张敏', dept: '产品中心', role: '普通成员', principal: 'USER=1e12+52', status: 'active', logins: 240 },
    { name: '周工', dept: '生产一部', role: '部门只读', principal: 'USER=1e12+88', status: 'active', logins: 96 },
    { name: '吴总（外部）', dept: '云启科技', role: '外部客户', principal: 'GROUP=4e12+6', status: 'pending', logins: 12 },
    { name: '赵工', dept: 'IT 中心', role: '运维管理员', principal: 'ROLE=3e12+4', status: 'locked', logins: 508 }
  ];

  var roles = [
    { name: '超级管理员', users: 2, scope: '全租户', write: true, note: '全部知识库与系统设置' },
    { name: '运维管理员', users: 5, scope: '全租户', write: true, note: '含 96 个管理运维工具' },
    { name: '客服坐席', users: 18, scope: '本部门向下继承', write: true, note: '工单读写、话术库' },
    { name: '普通成员', users: 246, scope: '按成员', write: false, note: '仅检索有权知识库' },
    { name: '外部客户', users: 63, scope: '仅显式成员', write: false, note: '渠道身份绑定后继承只读' }
  ];

  var audit = [
    { time: '10-18 09:31', user: '李伟', action: 'ticket.reply', target: 'TK-2087', ip: '10.20.4.31', result: 'ok' },
    { time: '10-18 09:12', user: '客服一线', action: 'agent.tool_call', target: 'ticket_create', ip: 'system', result: 'ok' },
    { time: '10-18 08:40', user: '图文知识库', action: 'kb.attach_send', target: '滤网拆装示意图 ×3', ip: 'system', result: 'ok' },
    { time: '10-17 22:04', user: '赵工', action: 'auth.login_failed', target: '连续失败 5 次 → 锁定 30 分钟', ip: '10.20.9.7', result: 'deny' },
    { time: '10-17 18:20', user: '系统管理员', action: 'kb.acl_update', target: '合同与合规模板 / deny 生产一部', ip: '10.20.1.2', result: 'ok' },
    { time: '10-17 16:55', user: '张敏', action: 'doc.export', target: '标准报价单 2026Q4.xlsx', ip: '10.20.6.88', result: 'ok' },
    { time: '10-17 11:02', user: '未知渠道', action: 'retrieval.deny', target: '请求含 kb-legal 但无权', ip: '10.30.2.4', result: 'deny' }
  ];

  var usage = {
    days: ['10-12', '10-13', '10-14', '10-15', '10-16', '10-17', '10-18'],
    series: [
      { name: 'deepseek-v4.1-flash', color: '#2B5CE6', values: [182, 214, 176, 268, 302, 244, 331] },
      { name: 'claude-sonnet', color: '#0E7C66', values: [46, 62, 38, 71, 88, 96, 74] },
      { name: 'bge-m3 (embedding)', color: '#B4690E', values: [128, 96, 210, 88, 142, 260, 118] }
    ]
  };

  var toolStats = [
    { name: 'kb_search', calls: 8421, ok: 99.4, ms: 340 },
    { name: 'doc_generate', calls: 1263, ok: 97.1, ms: 2140 },
    { name: 'http_request', calls: 942, ok: 93.8, ms: 880 },
    { name: 'script_run', calls: 318, ok: 91.2, ms: 3400 },
    { name: 'ticket_create', calls: 604, ok: 99.8, ms: 120 }
  ];

  var missQueries = [
    { q: '食堂几点开始供应晚餐', hits: 26, kb: '人事制度库' },
    { q: '供应商账期最长可以谈到多久', hits: 19, kb: '财务与报销库' },
    { q: '海外子公司能否用同一套 SSO', hits: 14, kb: '产品与售前资料' },
    { q: '车间夜班补贴标准', hits: 11, kb: '人事制度库' }
  ];

  var changelog = [
    { ver: 'v1.0.1', date: '2026-10-17', items: ['应用内更新支持桌面版 / Docker / 源码三种形态', '工作流新增并行网关与汇聚节点', '图文知识库命中后自动回传附件'] },
    { ver: 'v1.0.0', date: '2026-10-15', items: ['首个版本：混合检索 + ReAct 工具循环 + 客服工单', '默认 SQLite 零依赖起步', 'RBAC 四级授权与文档级 ACL'] }
  ];

  return {
    kbs: kbs, docs: docs, chunks: chunks, tools: tools, agents: agents, skills: skills,
    wfNodes: wfNodes, wfEdges: wfEdges, tickets: tickets, members: members, roles: roles,
    audit: audit, usage: usage, toolStats: toolStats, missQueries: missQueries, changelog: changelog,
    typeName: typeName, visName: visName
  };
})();
