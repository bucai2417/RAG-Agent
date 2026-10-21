// 工作流节点元数据：与后端 NODE_EXECUTORS 一一对应，供画布 / 属性面板 / 运行面板 / 节点面板共用。
//
// 约定：
// - type 必须与后端 `app/agents/workflow/engine.py` 的 NODE_EXECUTORS key 完全一致，
//   否则保存后在执行时会报「未知节点类型」。
// - group 决定节点面板的分类折叠；新增节点请归到已有分组，不要轻易新增分组。

export interface NodeMeta {
  label: string
  color: string
  icon: string
  group?: string
  hint?: string
}

export const NODE_GROUPS: { key: string; label: string }[] = [
  { key: 'flow', label: '流程控制' },
  { key: 'ai', label: 'AI 能力' },
  { key: 'data', label: '数据处理' },
  { key: 'office', label: '办公协作' },
  { key: 'integration', label: '外部集成' },
]

export const NODE_META: Record<string, NodeMeta> = {
  // ---- 流程控制 ----
  start: { label: '开始', color: '#52c41a', icon: '▶', group: 'flow', hint: '定义工作流输入参数' },
  end: { label: '结束', color: '#f5222d', icon: '⏹', group: 'flow', hint: '输出最终结果' },
  condition: { label: '条件分支', color: '#fa8c16', icon: '⑂', group: 'flow', hint: '二选一判断' },
  switch: { label: '多路分支', color: '#fa8c16', icon: '⑃', group: 'flow', hint: '表达式多分支' },
  router: { label: '分类路由', color: '#fa8c16', icon: '⑃', group: 'flow', hint: '按 AI 分类结果分流' },
  parallel: { label: '并行网关', color: '#08979c', icon: '⋔', group: 'flow', hint: '多分支同时执行' },
  join: { label: '汇聚', color: '#08979c', icon: '⋈', group: 'flow', hint: '合并并行分支' },
  loop: { label: '循环批量', color: '#08979c', icon: '🔁', group: 'flow', hint: '逐项处理列表' },
  batch: { label: '分批', color: '#08979c', icon: '▤', group: 'flow', hint: '把列表切成 N 批' },
  delay: { label: '延时', color: '#8c8c8c', icon: '⏱', group: 'flow', hint: '等待若干秒' },
  variable: { label: '变量赋值', color: '#8c8c8c', icon: '🔤', group: 'flow', hint: '定义中间变量' },
  approval: { label: '人工审批', color: '#d46b08', icon: '✋', group: 'flow', hint: '暂停等待人工确认' },

  // ---- AI 能力 ----
  llm: { label: '大模型', color: '#1677ff', icon: '🤖', group: 'ai', hint: '调用模型生成内容' },
  knowledge_retrieval: { label: '知识库检索', color: '#722ed1', icon: '📚', group: 'ai', hint: '从知识库召回片段' },
  summarize: { label: '摘要抽取', color: '#722ed1', icon: '📋', group: 'ai', hint: '总结/抽取/分类/情感' },
  agent: { label: '调用智能体', color: '#2f54eb', icon: '🧩', group: 'ai', hint: '协作另一个智能体' },
  code: { label: '代码执行', color: '#2f54eb', icon: '⌨', group: 'ai', hint: '运行 Python 片段' },

  // ---- 数据处理 ----
  table: { label: '表格处理', color: '#08979c', icon: '▦', group: 'data', hint: '过滤/排序/汇总/转 CSV' },
  mapping: { label: '数据映射', color: '#08979c', icon: '⇄', group: 'data', hint: '重塑数据成目标结构' },
  json_path: { label: 'JSON 取值', color: '#08979c', icon: '🔍', group: 'data', hint: '按路径取字段' },
  transform: { label: '数据清洗', color: '#08979c', icon: '✂', group: 'data', hint: '提取/分割/转数字' },
  collection: { label: '集合运算', color: '#08979c', icon: '∪', group: 'data', hint: '合并/去重/差集/交集' },
  text_template: { label: '文本模板', color: '#d46b08', icon: '📝', group: 'data', hint: '拼接报告/邮件正文' },
  file: { label: '文件处理', color: '#eb2f96', icon: '📄', group: 'data', hint: '读取/生成/转换文件' },

  // ---- 办公协作 ----
  email: { label: '发送邮件', color: '#d46b08', icon: '✉', group: 'office', hint: '用个人邮箱账号发信' },
  notify: { label: '消息推送', color: '#fa541c', icon: '📤', group: 'office', hint: '推送到通知渠道' },

  // ---- 外部集成 ----
  http: { label: 'HTTP 请求', color: '#13c2c2', icon: '🌐', group: 'integration', hint: '调用任意 REST 接口' },
  http_form: { label: '表单提交', color: '#13c2c2', icon: '🧾', group: 'integration', hint: 'form/json 表单提交' },
  webhook: { label: '出站 Webhook', color: '#13c2c2', icon: '🔔', group: 'integration', hint: '回调外部系统' },
}

/** 添加新节点时的默认 data（与后端执行器读取的字段对齐） */
export function defaultData(type: string): any {
  switch (type) {
    case 'start': return { inputs: [{ name: 'query', type: 'string', required: true }] }
    case 'llm': return { system: '', prompt: '', temperature: 0.3 }
    case 'knowledge_retrieval': return { query: '{{start.query}}', top_k: 5 }
    case 'condition': return { expression: '' }
    case 'switch': return { cases: [{ handle: 'case1', expr: '' }, { handle: 'case2', expr: '' }] }
    case 'router': return { value: '', cases: [{ handle: 'case1', values: [] }] }
    case 'parallel': return {}
    case 'join': return {}
    case 'http': return { url: '', method: 'GET', timeout: 20 }
    case 'file': return { op: 'read', args: {} }
    case 'variable': return { assignments: [{ name: 'var', value: '' }] }
    case 'code': return { code: '', timeout: 15 }
    case 'loop': return { items: '{{start.query}}', item_var: 'item', max_iterations: 50, body: { op: 'template', template: '{{item}}' } }
    case 'approval': return { title: '需要人工审批', content: '' }
    case 'agent': return { agent_id: null, input_template: '{{start.query}}', mode_id: null, max_depth: 3 }
    case 'end': return { output: '' }
    // ---- 扩展节点 ----
    case 'table': return {
      rows: '', filters: [], sort: { field: '', desc: false },
      fields: [], aggregates: [], distinct: false, limit: 0, as_csv: true,
    }
    case 'mapping': return { mappings: [{ key: '', value: '' }] }
    case 'json_path': return { source: '', path: '' }
    case 'transform': return { value: '', op: 'to_text', separator: '\n', pattern: '', group: 0 }
    case 'collection': return { lists: [], op: 'union' }
    case 'batch': return { items: '', size: 10 }
    case 'text_template': return { sections: [{ title: '', content: '' }] }
    case 'email': return { account_id: null, to: '', subject: '', body: '' }
    case 'summarize': return { text: '', mode: 'summary', model_config_id: null, temperature: 0.1 }
    case 'http_form': return { url: '', method: 'POST', encoding: 'form', fields: [{ name: '', value: '' }], timeout: 30 }
    case 'webhook': return { url: '', fields: [{ name: 'text', value: '' }], timeout: 30 }
    case 'delay': return { seconds: 1 }
    default: return {}
  }
}

/** 运行状态 → 描边色 */
export const STATUS_COLOR: Record<string, string> = {
  running: '#1677ff',
  success: '#52c41a',
  failed: '#ff4d4f',
  skipped: '#d9d9d9',
  waiting: '#faad14',
}

/** 按分组归类（节点面板用） */
export function groupNodes(): { key: string; label: string; items: { type: string; meta: NodeMeta }[] }[] {
  const buckets: Record<string, { type: string; meta: NodeMeta }[]> = {}
  for (const [type, meta] of Object.entries(NODE_META)) {
    const g = meta.group || 'flow'
    ;(buckets[g] ||= []).push({ type, meta })
  }
  return NODE_GROUPS
    .filter((g) => buckets[g.key]?.length)
    .map((g) => ({ key: g.key, label: g.label, items: buckets[g.key] }))
}
