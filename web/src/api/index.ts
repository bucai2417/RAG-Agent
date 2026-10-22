import { API_BASE, http } from './http'

export { API_BASE }

// ---------------- 类型 ----------------

export interface User {
  id: number
  tenant_id: number
  username: string
  display_name: string
  email?: string
  is_admin: boolean
  department_id?: number
  department_name?: string
  roles?: { id: number; code: string; name: string }[]
  permissions?: string[]
}

export interface KB {
  id: number
  name: string
  description?: string
  icon?: string
  visibility: 'public' | 'internal' | 'private'
  source_type?: 'local' | 'external' | 'entry'
  index_mode?: 'vector' | 'keyword'
  connector_kind?: string | null
  embedding_model_id?: number | null
  chunk_strategy?: Record<string, any>
  settings?: Record<string, any>
  doc_count: number
  chunk_count: number
  owner_id?: number
  my_perm?: string
  created_at: string
}

export interface Citation {
  chunk_id: number
  doc_id: number
  doc_title?: string
  page?: number
  score: number
  snippet: string
  source_uri?: string | null
}

export interface RetrievedChunk {
  chunk_id: number
  doc_id: number
  kb_id: number
  content: string
  score: number
  page?: number
  doc_title?: string
  source: string
}

export interface Attachment {
  type: 'image' | 'document' | 'video'
  file_key: string
  name: string
  mime?: string
  size?: number
  url: string
  doc_id?: number
}

/** AI 产物（生成的文件）引用 */
export interface ArtifactRef {
  artifact_id: number
  file_key: string
  name: string
  mime?: string
  size?: number
  url?: string
}

export interface ModelConfig {
  id: number
  provider_id: number
  purpose: string
  model_name: string
  display_name: string
  embedding_dim?: number
  is_default: boolean
  priority: number
  status: string
}

export interface UsageTotals {
  calls: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cached_tokens: number
}

export interface Doc {
  id: number
  kb_id: number
  title: string
  kind?: string
  file_name?: string
  file_ext?: string
  file_size?: number
  mime?: string
  content?: string | null
  attachments?: { file_key: string; name: string; mime: string; size: number }[] | null
  status: string
  progress: number
  error_msg?: string
  error_detail?: Record<string, any> | null
  page_count?: number
  char_count?: number
  chunk_count: number
  visibility?: string
  folder_id?: number | null
  tags?: string[] | null
  created_at: string
}

export interface Conversation {
  id: number
  title: string
  kb_ids?: number[]
  model_config_id?: number | null
  message_count: number
  created_at: string
  summary?: string | null
}

/** 上下文占用快照：输入框圆形进度环的数据源，与后端压缩判断同源 */
export interface ContextStats {
  available: boolean
  conversation_id: number | null
  /** 已占用 token（摘要 + 未压缩原文历史） */
  used_tokens: number
  /** 触发压缩的阈值，环满即压缩 */
  limit_tokens: number
  /** 压缩后仍保留的最近消息条数 */
  keep_recent: number
  percent: number
  compressed: boolean
  summary_tokens: number
  watermark: number
  message_count: number
  /** 已被折叠进摘要的消息条数 */
  archived_messages?: number
  compressing: boolean
}

export interface Message {
  id: number
  conversation_id: number
  role: string
  content: string
  reasoning?: string
  citations?: Citation[]
  attachments?: Attachment[]
  artifacts?: ArtifactRef[]
  usage?: Record<string, number>
  model?: string
  feedback?: number | null
  created_at: number
}

export interface UsageSummary {
  total: { calls: number; prompt_tokens: number; completion_tokens: number; avg_latency_ms: number }
  by_model: { model_config_id: number; name: string; calls: number; prompt_tokens: number; completion_tokens: number }[]
  by_user: { user_id: number; name: string; calls: number; prompt_tokens: number; completion_tokens: number }[]
  by_day: { date: string; calls: number; total_tokens?: number }[]
}

export interface NotificationItem {
  id: number
  title: string
  content?: string
  kind?: string
  level?: string
  /** 点击通知要跳转的前端路由（后端按通知类型设置，如注册申请 → /admin/users） */
  link?: string | null
  read: boolean
  created_at: number
}

// ---------------- 认证 ----------------

export const authApi = {
  login: (username: string, password: string) =>
    http.post('/auth/login', { username, password }).then((r) => r.data),
  me: () => http.get<User>('/auth/me').then((r) => r.data),
  updateProfile: (data: { display_name?: string; email?: string; phone?: string; avatar?: string }) =>
    http.patch<User>('/auth/profile', data).then((r) => r.data),
  changePassword: (oldPassword: string, newPassword: string) =>
    http
      .post('/auth/password', { old_password: oldPassword, new_password: newPassword })
      .then((r) => r.data),
  // 自助注册（是否开放由后端 registration_enabled 决定；注册后可能需管理员审核）
  register: (data: {
    username: string
    password: string
    display_name?: string
    email?: string
    department_id?: number | null
    reason?: string
  }) => http.post<{ message: string; pending: boolean }>('/auth/register', data).then((r) => r.data),
  // 注册用可选部门（公开端点，未开放注册时返回空数组）
  registerDepartments: () =>
    http
      .get<{ id: number; name: string; parent_id: number | null; depth: number }[]>(
        '/auth/register/departments',
      )
      .then((r) => r.data),
  // 注册是否开放（公开端点）
  registerStatus: () =>
    http.get<{ enabled: boolean; require_approval: boolean }>('/auth/register/status').then((r) => r.data),
}

// ---------------- 知识库 ----------------

export const kbApi = {
  list: () => http.get<KB[]>('/kbs').then((r) => r.data),
  create: (data: Partial<KB>) => http.post<KB>('/kbs', data).then((r) => r.data),
  get: (id: number) => http.get<KB>(`/kbs/${id}`).then((r) => r.data),
  update: (id: number, data: Partial<KB>) => http.patch<KB>(`/kbs/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/kbs/${id}`).then((r) => r.data),
  stats: (id: number) => http.get<KBStats>(`/kbs/${id}/stats`).then((r) => r.data),
  addMember: (id: number, principalType: string, principalId: number, perm: string) =>
    http.post(`/kbs/${id}/members`, { principal_type: principalType, principal_id: principalId, perm_level: perm }).then((r) => r.data),
  members: (id: number) => http.get<KBMember[]>(`/kbs/${id}/members`).then((r) => r.data),
  removeMember: (id: number, memberId: number) => http.delete(`/kbs/${id}/members/${memberId}`).then((r) => r.data),
  testConnectorConfig: (connector_kind: string, connector_config: Record<string, any>, query = '测试', top_k = 3) =>
    http.post<{ ok: boolean; count: number; items: any[] }>('/kbs/connector/test', { connector_kind, connector_config, query, top_k }).then((r) => r.data),
  getConnector: (id: number) => http.get<ConnectorInfo>(`/kbs/${id}/connector`).then((r) => r.data),
  testConnector: (id: number, query = '测试', top_k = 3) =>
    http.post<{ ok: boolean; count: number; items: any[] }>(`/kbs/${id}/connector/test`, { query, top_k }).then((r) => r.data),
  syncStatus: (id: number) =>
    http.get<{ enabled: boolean; last_at: number | null; last_status: string | null; last_count: number | null; last_error: string | null; seed_queries: string[]; limit: number; synced_doc_count: number }>(`/kbs/${id}/sync/status`).then((r) => r.data),
  sync: (id: number, limit = 500) =>
    http.post<{ kb_id: number; fetched: number; created: number; skipped: number; document_ids: number[] }>(`/kbs/${id}/sync`, { limit }).then((r) => r.data),
  syncConfig: (id: number, data: { enabled?: boolean; seed_queries?: string[]; limit?: number }) =>
    http.put(`/kbs/${id}/sync/config`, data).then((r) => r.data),
  audit: (id: number, staleDays = 180) =>
    http.get<{ total: number; counts: Record<string, number>; empty: any[]; failed: any[]; no_chunk: any[]; untagged: any[]; stale: any[] }>(`/kbs/${id}/audit`, { params: { stale_days: staleDays } }).then((r) => r.data),
  missedQueries: (days = 30, limit = 20) =>
    http.get<{ query: string; count: number }[]>('/retrieval/missed-queries', { params: { days, limit } }).then((r) => r.data),
}

export interface KBStats {
  doc_count: number
  chunk_count: number
  embedded_chunks: number
  embedded_ratio: number
  total_size: number
  by_status: Record<string, number>
  by_ext: Record<string, number>
}
export interface KBMember {
  id: number
  principal_id: number
  principal_type: 'user' | 'department' | 'role' | 'group'
  principal_ref_id: number
  user_id: number | null
  display_name: string
  perm_level: string
}
export interface ConnectorInfo {
  source_type: string
  connector_kind: string | null
  config: Record<string, any> | null
}

// ---------------- 文档 ----------------

export const docApi = {
  list: (kbId: number) => http.get<Doc[]>('/documents', { params: { kb_id: kbId } }).then((r) => r.data),
  get: (id: number) => http.get<Doc>(`/documents/${id}`).then((r) => r.data),
  upload: (kbId: number, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return http.post<Doc>(`/documents/upload?kb_id=${kbId}`, fd).then((r) => r.data)
  },
  createEntry: (kbId: number, title: string, content: string, files: File[]) => {
    const fd = new FormData()
    fd.append('title', title)
    fd.append('content', content)
    files.forEach((f) => fd.append('files', f))
    return http.post<Doc>(`/documents/kbs/${kbId}/entries`, fd).then((r) => r.data)
  },
  updateEntry: (id: number, data: { title?: string; content?: string }) =>
    http.patch<Doc>(`/documents/${id}/entry`, data).then((r) => r.data),
  addEntryAttachments: (id: number, files: File[]) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    return http.post<Doc>(`/documents/${id}/attachments`, fd).then((r) => r.data)
  },
  removeEntryAttachment: (id: number, idx: number) => http.delete(`/documents/${id}/attachments/${idx}`).then((r) => r.data),
  entryAttachmentUrl: (docId: number, idx: number) => `/api/v1/documents/${docId}/attachments/${idx}/download`,
  remove: (id: number) => http.delete(`/documents/${id}`).then((r) => r.data),
  reprocess: (id: number) => http.post<Doc>(`/documents/${id}/reprocess`).then((r) => r.data),
  rawUrl: (id: number) => `/api/v1/documents/${id}/raw`,
  content: (id: number) => http.get<{ id: number; title: string; content: string; source: string; page_count: number }>(`/documents/${id}/content`).then((r) => r.data),
  chunks: (id: number, page = 1, pageSize = 50) => http.get<{ total: number; items: any[] }>(`/documents/${id}/chunks`, { params: { page, page_size: pageSize } }).then((r) => r.data),
  updateChunk: (docId: number, chunkId: number, content: string) => http.put(`/documents/${docId}/chunks/${chunkId}`, { content }).then((r) => r.data),
  deleteChunk: (docId: number, chunkId: number) => http.delete(`/documents/${docId}/chunks/${chunkId}`).then((r) => r.data),
  splitChunk: (docId: number, chunkId: number, offset: number) => http.post(`/documents/${docId}/chunks/${chunkId}/split`, { offset }).then((r) => r.data),
  batchDelete: (ids: number[]) => http.post('/documents/batch-delete', { ids }).then((r) => r.data),
  batchMove: (ids: number[], folderId: number | null) => http.post('/documents/batch-move', { ids, folder_id: folderId }).then((r) => r.data),
  batchVisibility: (ids: number[], visibility: string) => http.post('/documents/batch-visibility', { ids, visibility }).then((r) => r.data),
  batchReprocess: (ids: number[]) => http.post('/documents/batch-reprocess', { ids }).then((r) => r.data),
  mergeExportUrl: (fmt: 'docx' | 'pdf' | 'md' | 'txt' = 'docx') => `${API_BASE}/documents/batch-merge-export?fmt=${fmt}`,
  setTags: (id: number, tags: string[]) => http.put<Doc>(`/documents/${id}/tags`, { tags }).then((r) => r.data),
  setVisibility: (id: number, visibility: string) => http.patch<Doc>(`/documents/${id}/visibility`, { visibility }).then((r) => r.data),
  acl: (id: number) => http.get<{ visibility: string; items: { id: number; principal_id: number; effect: string; principal_name?: string | null }[] }>(`/documents/${id}/acl`).then((r) => r.data),
  addAcl: (id: number, data: { principal_type: string; principal_id: number; effect: string }) => http.post(`/documents/${id}/acl`, data).then((r) => r.data),
  removeAcl: (id: number, aclId: number) => http.delete(`/documents/${id}/acl/${aclId}`).then((r) => r.data),
  listVersions: (docId: number) =>
    http.get<{ id: number; version: number; title: string | null; char_count: number; chunk_count: number; reason: string; created_at: string | null; current: boolean }[]>(`/documents/${docId}/versions`).then((r) => r.data),
  getVersion: (docId: number, version: number) =>
    http.get<{ id: number; version: number; title: string | null; content: string | null; char_count: number; chunk_count: number; chunk_snapshot: any[] | null; reason: string; created_at: string | null }>(`/documents/${docId}/versions/${version}`).then((r) => r.data),
  rollbackVersion: (docId: number, version: number) => http.post<Doc>(`/documents/${docId}/versions/${version}/rollback`).then((r) => r.data),
  listFolders: (kbId: number) => http.get<{ id: number; name: string; parent_id: number | null; sort?: number }[]>('/documents/folders/list', { params: { kb_id: kbId } }).then((r) => r.data),
  createFolder: (kbId: number, name: string, parentId?: number, sort?: number) => http.post('/documents/folders', { name, parent_id: parentId, sort }, { params: { kb_id: kbId } }).then((r) => r.data),
  updateFolder: (folderId: number, data: { name?: string; sort?: number; parent_id?: number | null }) => http.patch(`/documents/folders/${folderId}`, data).then((r) => r.data),
  reorderFolders: (kbId: number, orderedIds: number[]) => http.put('/documents/folders/order', { ordered_ids: orderedIds }, { params: { kb_id: kbId } }).then((r) => r.data),
  removeFolder: (folderId: number) => http.delete(`/documents/folders/${folderId}`).then((r) => r.data),
  moveDoc: (docId: number, folderId: number | null) => http.patch(`/documents/${docId}/folder`, { folder_id: folderId }).then((r) => r.data),
}

// ---------------- 智能问答 ----------------

export const chatApi = {
  conversations: (params?: { before_id?: number; limit?: number }) =>
    http.get<Conversation[]>('/chat/conversations', { params }).then((r) => r.data),
  createConversation: (kbIds: number[], title?: string) =>
    http.post<Conversation>('/chat/conversations', { kb_ids: kbIds, title }).then((r) => r.data),
  messages: (convId: number, params?: { before_id?: number; limit?: number }) =>
    http.get<Message[]>(`/chat/conversations/${convId}/messages`, { params }).then((r) => r.data),
  rename: (convId: number, title: string) =>
    http.patch(`/chat/conversations/${convId}`, { title }).then((r) => r.data),
  setModel: (convId: number, modelConfigId: number | null) =>
    http.patch(`/chat/conversations/${convId}`, { model_config_id: modelConfigId }).then((r) => r.data),
  remove: (convId: number) => http.delete(`/chat/conversations/${convId}`).then((r) => r.data),
  usage: (convId: number) => http.get<UsageTotals>(`/chat/conversations/${convId}/usage`).then((r) => r.data),
  /** 上下文占用（圆形进度环）：与后端压缩阈值同源 */
  context: (convId: number) =>
    http.get<ContextStats>(`/chat/conversations/${convId}/context`).then((r) => r.data),
  uploadAttachment: (file: File, opts: { kind?: string; kbId?: number } = {}) => {
    const fd = new FormData()
    fd.append('file', file)
    const q = new URLSearchParams()
    if (opts.kind) q.set('kind', opts.kind)
    if (opts.kbId) q.set('kb_id', String(opts.kbId))
    return http.post<Attachment>(`/chat/attachments?${q.toString()}`, fd).then((r) => r.data)
  },
  feedback: (messageId: number, feedback: number) =>
    http.patch(`/chat/messages/${messageId}/feedback`, { feedback }).then((r) => r.data),
  signAttachment: (fileKey: string) =>
    http.post<{ url: string; expires_in: number }>(`/chat/attachments/${fileKey}/sign`).then((r) => r.data),
  signAttachmentsBatch: (fileKeys: string[]) =>
    http.post<{ urls: Record<string, string>; expires_in: number }>('/chat/attachments/sign', { file_keys: fileKeys }).then((r) => r.data),
  regenerateUrl: (convId: number) => `${API_BASE}/chat/conversations/${convId}/regenerate`,
  exportUrl: (convId: number, fmt: 'md' | 'pdf' | 'docx' = 'md') =>
    `${API_BASE}/chat/conversations/${convId}/export?fmt=${fmt}`,
  share: (convId: number) =>
    http.post<{ url: string; download_url: string; expires_in: number }>(`/chat/conversations/${convId}/share`).then((r) => r.data),
  adminConversations: (userId?: number) =>
    http.get<(Conversation & { owner_id: number; owner_name: string })[]>('/chat/admin/conversations', { params: { user_id: userId } }).then((r) => r.data),
  adminChatUsers: () =>
    http.get<{ user_id: number; name: string; conversation_count: number; channel_label?: string | null; external_id?: string | null }[]>('/chat/admin/users').then((r) => r.data),
}

// ---------------- 文档 ----------------

export interface StreamHandlers {
  onMeta?: (data: { conversation_id: number }) => void
  onDelta?: (text: string) => void
  onCitations?: (citations: Citation[]) => void
  onUsage?: (usage: Record<string, number>, latency: number) => void
  onDone?: (messageId: number) => void
  onError?: (msg: string) => void
  onReasoning?: (text: string) => void
  onToolCall?: (evt: ToolCallEvt) => void
  onToolResult?: (evt: ToolResultEvt) => void
  onPendingAction?: (evt: PendingActionEvt) => void
  onCitationCheck?: (evt: { warning: string; has_fake_cite: boolean }) => void
  /** 每轮回答结束时的上下文占用快照（驱动输入框圆形进度环） */
  onContext?: (stats: ContextStats) => void
}

export interface ToolCallEvt {
  id: string
  name: string
  arguments: string
  turn?: number
}
export interface ToolResultEvt {
  id: string
  name: string
  content: string
  is_error: boolean
  data?: any
  turn?: number
}
export interface PendingActionEvt {
  action_id: number
  tool_name: string
  arguments: any
  summary: string
  expires_at?: number
}

/** SSE 流式对话（与 React 版同协议：event/data 行） */
export async function streamChat(
  body: {
    conversation_id?: number
    kb_ids: number[]
    message: string
    top_k?: number
    model_config_id?: number
    temperature?: number
    use_retrieval?: boolean
    use_tools?: boolean
    allow_auto_write?: boolean
    attachments?: any[]
  },
  handlers: StreamHandlers,
  signal?: AbortSignal,
  url?: string,
) {
  const token = localStorage.getItem('access_token')
  const resp = await fetch(url || `${API_BASE}/chat/completions`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ ...body, stream: true }),
    signal,
  })
  if (!resp.body) throw new Error('无响应流')
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let event = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''
    for (const line of lines) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) {
        const raw = line.slice(5).trim()
        if (!raw) continue
        let data: any
        try {
          data = JSON.parse(raw)
        } catch {
          continue
        }
        dispatchEvent(event, data, handlers)
      }
    }
  }
}

function dispatchEvent(event: string, data: any, handlers: StreamHandlers) {
  if (event === 'meta') handlers.onMeta?.(data)
  else if (event === 'delta') handlers.onDelta?.(data.text || '')
  else if (event === 'reasoning') handlers.onReasoning?.(data.text || '')
  else if (event === 'citations') handlers.onCitations?.(data.citations || [])
  else if (event === 'usage') handlers.onUsage?.(data.usage || {}, data.latency_ms || 0)
  else if (event === 'done') handlers.onDone?.(data.message_id)
  else if (event === 'error') handlers.onError?.(data.message)
  else if (event === 'tool_call') handlers.onToolCall?.(data)
  else if (event === 'tool_result') handlers.onToolResult?.(data)
  else if (event === 'pending_action') handlers.onPendingAction?.(data)
  else if (event === 'citation_check') handlers.onCitationCheck?.(data)
  else if (event === 'context') handlers.onContext?.(data as ContextStats)
}

/** HITL：确认/拒绝 AI 写操作；后续事件复用同一套 handler（续跑到原消息） */
export async function confirmToolAction(actionId: number, decision: 'approve' | 'reject', handlers: StreamHandlers) {
  const token = localStorage.getItem('access_token')
  const resp = await fetch(`${API_BASE}/chat/tool-confirm`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ action_id: actionId, decision }),
  })
  if (!resp.body) throw new Error('无响应流')
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let ev = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const lines = buf.split('\n')
    buf = lines.pop() || ''
    for (const line of lines) {
      if (line.startsWith('event:')) ev = line.slice(6).trim()
      else if (line.startsWith('data:')) {
        const raw = line.slice(5).trim()
        if (!raw) continue
        let data: any
        try {
          data = JSON.parse(raw)
        } catch {
          continue
        }
        dispatchEvent(ev, data, handlers)
      }
    }
  }
}

// ---------------- 用量 / 通知 ----------------

export const usageApi = {
  summary: (days = 30) => http.get<UsageSummary>('/usage/summary', { params: { days } }).then((r) => r.data),
  meToday: () => http.get<UsageTotals>('/usage/me/today').then((r) => r.data),
}

export const notificationApi = {
  list: (page = 1, pageSize = 20) =>
    http
      .get<{ total: number; unread: number; items: NotificationItem[] }>('/notifications', {
        params: { page, page_size: pageSize },
      })
      .then((r) => r.data),
  unreadCount: () => http.get<{ unread: number }>('/notifications/unread-count').then((r) => r.data),
  markRead: (ids: number[]) => http.post('/notifications/read', { ids }).then((r) => r.data),
  markAllRead: () => http.post('/notifications/read-all').then((r) => r.data),
}

// ---------------- 模型 Provider ----------------

export const providerApi = {
  list: () => http.get<any[]>('/providers').then((r) => r.data).catch(() => []),
  configs: () => http.get<ModelConfig[]>('/providers/configs/all').then((r) => r.data),
}

// ---------------- 文件（产物签名 URL） ----------------

export const fileApi = {
  signUrl: (id: number) =>
    http.get<{ url: string; download_url: string; expires_in: number }>(`/files/${id}/url`).then((r) => r.data),
}

// ---------------- 智能体 / 技能 / 工具 ----------------

export interface Agent {
  id: number
  name: string
  slug: string
  description?: string
  icon?: string
  type: string
  system_prompt: string
  model_config_id?: number
  kb_ids?: number[]
  skill_ids?: number[]
  tool_config?: Record<string, any>
  config?: Record<string, any>
  default_mode_id?: number
  status: string
  visibility: string
  owner_id?: number
  is_builtin?: boolean
  created_at: string
}

export interface AgentMode {
  id: number
  agent_id: number
  name: string
  description?: string
  system_prompt: string
  skill_ids?: number[]
  tool_config?: Record<string, any>
  kb_ids?: number[]
  params?: Record<string, any>
  is_default: boolean
  sort: number
}

export interface Skill {
  id: number
  name: string
  slug: string
  description?: string
  icon?: string
  kind: string
  prompt_template?: string
  params_schema?: Record<string, any>
  tool_ids?: number[]
  kb_ids?: number[]
  model_config_id?: number
  status: string
  parent_id?: number | null
}

export interface ToolItem {
  id: number
  name: string
  display_name: string
  description: string
  kind: string
  parameters?: Record<string, any>
  [k: string]: any
}

// ---------------- 外部 IM 渠道 ----------------

export interface ChannelItem {
  id: number
  kind: 'qqbot' | 'wxclaw' | 'wework' | 'feishu'
  name: string
  config: Record<string, any>
  secret_meta?: Record<string, { set: boolean; length: number }>
  enabled: boolean
  connected: boolean
  last_error?: string | null
  default_kb_ids: number[]
  kb_mode?: 'auto' | 'custom' | 'off'
  service_mode?: 'qa' | 'support'
  default_lang?: string | null
  default_agent_id?: number | null
  reply_mode: 'rag' | 'smart' | 'agent' | 'workflow'
  progress_hint?: boolean
  group_progress_mode?: 'off' | 'first' | 'throttle'
  group_progress_interval?: number
  command_prefix: string
  greeting?: string | null
  attach_ingest?: 'off' | 'bound' | 'all'
  attach_kb_ids?: number[]
  attach_max_mb?: number
  attach_types?: string | null
  callback_url: string
}

export interface ChannelUserItem {
  id: number
  external_id: string
  display_name?: string
  user_id: number
  bound_user_id?: number | null
  bound_user_name?: string | null
  agent_id?: number | null
  bind_code?: string | null
  default_kb_ids?: number[]
}

export const channelApi = {
  list: () => http.get<ChannelItem[]>('/channels').then((r) => r.data),
  create: (data: any) => http.post<ChannelItem>('/channels', data).then((r) => r.data),
  update: (id: number, data: any) => http.patch<ChannelItem>(`/channels/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/channels/${id}`).then((r) => r.data),
  test: (id: number) => http.post<{ ok: boolean; message: string; latency_ms: number }>(`/channels/${id}/test`).then((r) => r.data),
  users: (id: number) => http.get<ChannelUserItem[]>(`/channels/${id}/users`).then((r) => r.data),
  secrets: (id: number) => http.get<{ secrets: Record<string, string> }>(`/channels/${id}/secrets`).then((r) => r.data),
  wxScanLogin: (id: number) =>
    http.post<{ ok: boolean; logged_in: boolean; qr: string; qr_display: string; ticket: string; status: string }>(`/channels/${id}/wxclaw/scan-login`).then((r) => r.data),
  wxLoginStatus: (id: number) =>
    http.get<{ logged_in: boolean; status: string; qr_display: string; ticket: string; message?: string }>(`/channels/${id}/wxclaw/login-status`).then((r) => r.data),
  wxScan: () =>
    http.post<{ session: string; logged_in: boolean; qr: string; qr_display: string; ticket: string; status: string }>(`/channels/wxclaw/scan`).then((r) => r.data),
  wxScanStatus: (session: string) =>
    http.get<{ logged_in: boolean; status: string; token: string; bot_id?: string; qr_display: string; message?: string }>('/channels/wxclaw/scan-status', { params: { session } }).then((r) => r.data),
  bindUser: (channelId: number, cuId: number, data: { user_id?: number; bind_code?: string }) =>
    http.post<{ message: string; bound_user_id: number }>(`/channels/${channelId}/users/${cuId}/bind`, data).then((r) => r.data),
  unbindUser: (channelId: number, cuId: number) =>
    http.delete<{ message: string }>(`/channels/${channelId}/users/${cuId}/bind`).then((r) => r.data),
  bindByCode: (bindCode: string, userId: number) =>
    http.post<{ message: string; bound_user_id: number; channel_user_id: number }>('/channels/users/bind-by-code', { bind_code: bindCode, user_id: userId }).then((r) => r.data),
}

// ---------------- 企业聊天室 ----------------

export interface ChatRoomBrief {
  id: number
  name: string
  kind: 'group' | 'direct'
  is_default: boolean
  announcement?: string | null
  owner_id: number
  my_role: string
  peer_user_id?: number | null
  last_message_at?: number | null
  last_preview?: string
  message_count?: number
  unread?: number
  peer?: {
    user_id: number
    name: string
    username?: string | null
    department?: string | null
    is_admin?: boolean
    remark?: string | null
    display?: string
    online?: boolean
  } | null
}

export interface ChatMsgItem {
  id: number
  room_id: number
  sender_id: number | null
  sender_type: 'user' | 'agent' | 'system'
  sender_name: string
  sender_username?: string | null
  sender_department?: string | null
  sender_is_agent?: boolean
  content: string
  content_type?: string
  attachments?: any[] | null
  mentions?: number[] | null
  reply_to_id?: number | null
  pinned: boolean
  revoked: boolean
  revoked_by?: number | null
  created_at: number
  read_by?: number[]
  unread_by?: number[]
  read_count?: number
}

export interface RoomDetail {
  id: number
  name: string
  kind: string
  is_default: boolean
  announcement?: string | null
  owner_id: number
  my_role: string
  peer_user_id?: number | null
  members: {
    id: number
    user_id: number | null
    agent_id?: number | null
    role: string
    name: string
    username?: string | null
    department?: string | null
    is_admin?: boolean
    is_agent?: boolean
    online?: boolean
    muted_until?: number | null
  }[]
  bots: { member_id: number; agent_id: number; name: string }[]
  announcements?: { id: number; content: string; pinned: boolean; created_by?: number | null; created_by_name?: string; created_at?: number | null }[]
  mute_all?: boolean
  pinned: ChatMsgItem[]
}

export const chatRoomApi = {
  list: () => http.get<ChatRoomBrief[]>('/chat/rooms').then((r) => r.data),
  create: (data: { name: string; announcement?: string; member_ids?: number[] }) =>
    http.post('/chat/rooms', data).then((r) => r.data),
  direct: (userId: number) => http.post<{ id: number; kind: string }>('/chat/rooms/direct', { user_id: userId }).then((r) => r.data),
  detail: (roomId: number) => http.get<RoomDetail>(`/chat/rooms/${roomId}`).then((r) => r.data),
  messages: (roomId: number, beforeId?: number, limit = 30) =>
    http.get<{ items: ChatMsgItem[]; has_more: boolean }>(`/chat/rooms/${roomId}/messages`, { params: { before_id: beforeId, limit } }).then((r) => r.data),
  send: (roomId: number, data: { content?: string; attachments?: any[]; mentions?: number[]; reply_to_id?: number | null }) =>
    http.post<ChatMsgItem>(`/chat/rooms/${roomId}/messages`, data).then((r) => r.data),
  revoke: (roomId: number, msgId: number) => http.post(`/chat/rooms/${roomId}/messages/${msgId}/revoke`).then((r) => r.data),
  pin: (roomId: number, msgId: number, pinned = true) => http.post(`/chat/rooms/${roomId}/messages/${msgId}/pin`, { pinned }).then((r) => r.data),
  read: (roomId: number) => http.post(`/chat/rooms/${roomId}/read`).then((r) => r.data),
  addMembers: (roomId: number, userIds: number[]) => http.post(`/chat/rooms/${roomId}/members`, { user_ids: userIds }).then((r) => r.data),
  setRole: (roomId: number, userId: number, role: string) => http.post(`/chat/rooms/${roomId}/members/${userId}/role`, { role }).then((r) => r.data),
  removeMember: (roomId: number, userId: number) => http.delete(`/chat/rooms/${roomId}/members/${userId}`).then((r) => r.data),
  mute: (roomId: number, userId: number, minutes: number) => http.post(`/chat/rooms/${roomId}/members/${userId}/mute`, { minutes }).then((r) => r.data),
  announcements: (roomId: number) => http.get<any[]>(`/chat/rooms/${roomId}/announcements`).then((r) => r.data),
  createAnnouncement: (roomId: number, content: string, pinned = false) =>
    http.post(`/chat/rooms/${roomId}/announcement`, { announcement: content, pinned }).then((r) => r.data),
  updateAnnouncement: (roomId: number, annId: number, data: { content?: string; pinned?: boolean }) =>
    http.patch(`/chat/rooms/${roomId}/announcements/${annId}`, data).then((r) => r.data),
  deleteAnnouncement: (roomId: number, annId: number) => http.delete(`/chat/rooms/${roomId}/announcements/${annId}`).then((r) => r.data),
  setBotRole: (roomId: number, agentId: number, role: string) => http.post(`/chat/rooms/${roomId}/bots/${agentId}/role`, { role }).then((r) => r.data),
  searchMessages: (roomId: number, q: string, limit = 30) =>
    http.get<{ items: ChatMsgItem[]; count: number; keyword: string }>(`/chat/rooms/${roomId}/messages/search`, { params: { q, limit } }).then((r) => r.data),
  muteAll: (roomId: number, enabled: boolean, minutes = 0) => http.post(`/chat/rooms/${roomId}/mute-all`, { enabled, minutes }).then((r) => r.data),
  memberProfile: (roomId: number, userId: number) =>
    http.get<{
      id: number; username: string; display_name: string; avatar?: string | null
      email?: string | null; phone?: string | null; department_name?: string | null
      is_admin: boolean; roles: string[]; user_type: string; status: string; last_login_at?: string | null
    }>(`/chat/rooms/${roomId}/members/${userId}/profile`).then((r) => r.data),
  remarks: () => http.get<{ remarks: Record<string, string> }>('/chat/rooms/remarks').then((r) => r.data),
  setRemark: (peerId: number, remark: string) =>
    http.put<{ message: string; remark: string | null }>(`/chat/rooms/remarks/${peerId}`, { remark }).then((r) => r.data),
  updateRoom: (roomId: number, data: { name?: string; avatar?: string; announcement?: string }) =>
    http.patch(`/chat/rooms/${roomId}`, data).then((r) => r.data),
  transferOwner: (roomId: number, userId: number) => http.post(`/chat/rooms/${roomId}/transfer`, { user_id: userId }).then((r) => r.data),
  leaveRoom: (roomId: number) => http.post(`/chat/rooms/${roomId}/leave`).then((r) => r.data),
  clearMessages: (roomId: number) => http.post(`/chat/rooms/${roomId}/clear`).then((r) => r.data),
  dissolveRoom: (roomId: number) => http.post(`/chat/rooms/${roomId}/dissolve`).then((r) => r.data),
  files: (roomId: number, kind?: string) =>
    http.get<{ items: any[] }>(`/chat/rooms/${roomId}/files`, { params: kind ? { kind } : {} }).then((r) => r.data),
  deleteFile: (roomId: number, messageId: number, fileKey: string) =>
    http.delete(`/chat/rooms/${roomId}/files/${messageId}`, { params: { file_key: fileKey } }).then((r) => r.data),
  addBot: (roomId: number, agentId: number) => http.post(`/chat/rooms/${roomId}/bots`, { agent_id: agentId }).then((r) => r.data),
  removeBot: (roomId: number, agentId: number) => http.delete(`/chat/rooms/${roomId}/bots/${agentId}`).then((r) => r.data),
  wsUrl: () => {
    const token = localStorage.getItem('access_token') || ''
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    return `${proto}://${location.host}/api/v1/chat/rooms/ws?token=${encodeURIComponent(token)}`
  },
}

// ---------------- 组织用户（@ 选择器等） ----------------

export interface Role {
  id: number
  code: string
  name: string
  scope: string
  is_system: boolean
  description?: string
  permission_count?: number
  user_count?: number
}
export interface PermissionItem {
  id: number
  code: string
  name: string
  resource: string
  action: string
}
export interface UserListItem {
  id: number
  username: string
  display_name: string
  email?: string
  status: string
  approval_status?: 'pending' | 'approved' | 'rejected'
  is_admin: boolean
  department_id?: number
  created_at: string
}
export interface UserRoleItem {
  id: number
  user_id: number
  role_id: number
  role_code?: string
  role_name?: string
  scope_type: string
  scope_id: number
  expires_at?: string
}
export interface DeptNode {
  id: number
  name: string
  parent_id?: number
  code?: string
  path: string
  depth: number
  sort: number
  member_count?: number
  children: DeptNode[]
}

export const rbacApi = {
  users: (page = 1, pageSize = 20, search?: string, status?: string) =>
    http.get<{ items: UserListItem[]; total: number }>('/admin/users', { params: { page, page_size: pageSize, search, status } }).then((r) => r.data),
  roles: () => http.get<Role[]>('/admin/roles').then((r) => r.data),
  createRole: (data: Partial<Role>) => http.post<Role>('/admin/roles', data).then((r) => r.data),
  updateRole: (id: number, data: Partial<Role>) => http.patch<Role>(`/admin/roles/${id}`, data).then((r) => r.data),
  removeRole: (id: number) => http.delete(`/admin/roles/${id}`).then((r) => r.data),
  permissions: () => http.get<PermissionItem[]>('/admin/permissions').then((r) => r.data),
  rolePermissions: (id: number) => http.get<number[]>(`/admin/roles/${id}/permissions`).then((r) => r.data),
  setRolePermissions: (id: number, permissionIds: number[]) => http.put(`/admin/roles/${id}/permissions`, { permission_ids: permissionIds }).then((r) => r.data),
  pendingUsers: () =>
    http.get<{ id: number; username: string; display_name: string; email: string | null; department_name?: string | null; reason: string | null; registered_at: string | null }[]>('/admin/users/pending').then((r) => r.data),
  approveUser: (id: number) => http.post(`/admin/users/${id}/approve`).then((r) => r.data),
  rejectUser: (id: number) => http.post(`/admin/users/${id}/reject`).then((r) => r.data),
  createUser: (data: Record<string, any>) => http.post<UserListItem>('/admin/users', data).then((r) => r.data),
  updateUser: (id: number, data: Record<string, any>) => http.patch<UserListItem>(`/admin/users/${id}`, data).then((r) => r.data),
  removeUser: (id: number) => http.delete(`/admin/users/${id}`).then((r) => r.data),
  userRoles: (id: number) => http.get<UserRoleItem[]>(`/admin/users/${id}/roles`).then((r) => r.data),
  grantRole: (id: number, data: Record<string, any>) => http.post<UserRoleItem>(`/admin/users/${id}/roles`, data).then((r) => r.data),
  revokeRole: (userId: number, urId: number) => http.delete(`/admin/users/${userId}/roles/${urId}`).then((r) => r.data),
  deptTree: () => http.get<DeptNode[]>('/admin/departments/tree').then((r) => r.data),
  createDept: (data: Record<string, any>) => http.post('/admin/departments', data).then((r) => r.data),
  updateDept: (id: number, data: Record<string, any>) => http.patch(`/admin/departments/${id}`, data).then((r) => r.data),
  removeDept: (id: number) => http.delete(`/admin/departments/${id}`).then((r) => r.data),
  groups: () => http.get<{ id: number; name: string; description?: string }[]>('/admin/groups').then((r) => r.data),
  createGroup: (data: Record<string, any>) => http.post('/admin/groups', data).then((r) => r.data),
  updateGroup: (id: number, data: Record<string, any>) => http.patch(`/admin/groups/${id}`, data).then((r) => r.data),
  removeGroup: (id: number) => http.delete(`/admin/groups/${id}`).then((r) => r.data),
  groupMembers: (id: number) => http.get<number[]>(`/admin/groups/${id}/members`).then((r) => r.data),
  setGroupMembers: (id: number, userIds: number[]) => http.put(`/admin/groups/${id}/members`, { user_ids: userIds }).then((r) => r.data),
}

export const agentApi = {
  list: () => http.get<Agent[]>('/agents').then((r) => r.data),
  get: (id: number) => http.get<Agent>(`/agents/${id}`).then((r) => r.data),
  create: (data: Partial<Agent>) => http.post<Agent>('/agents', data).then((r) => r.data),
  update: (id: number, data: Partial<Agent>) => http.patch<Agent>(`/agents/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/agents/${id}`).then((r) => r.data),
  publish: (id: number) => http.post<Agent>(`/agents/${id}/publish`).then((r) => r.data),
  clone: (id: number) => http.post<Agent>(`/agents/${id}/clone`).then((r) => r.data),
  versions: (id: number) => http.get<{ id: number; version: number; note?: string; created_at: string }[]>(`/agents/${id}/versions`).then((r) => r.data),
  snapshot: (id: number, note?: string) => http.post(`/agents/${id}/versions`, { note }).then((r) => r.data),
  rollback: (id: number, versionId: number) => http.post<Agent>(`/agents/${id}/versions/${versionId}/rollback`).then((r) => r.data),
  modes: (id: number) => http.get<AgentMode[]>(`/agents/${id}/modes`).then((r) => r.data),
  createMode: (id: number, data: Partial<AgentMode>) => http.post<AgentMode>(`/agents/${id}/modes`, data).then((r) => r.data),
  updateMode: (id: number, modeId: number, data: Partial<AgentMode>) => http.patch<AgentMode>(`/agents/${id}/modes/${modeId}`, data).then((r) => r.data),
  removeMode: (id: number, modeId: number) => http.delete(`/agents/${id}/modes/${modeId}`).then((r) => r.data),
}

// ---------------- 人设模板 ----------------

export interface PersonaTemplate {
  id: number
  name: string
  slug?: string | null
  description?: string | null
  system_prompt: string
  category: string
  icon?: string | null
  tags?: string[] | null
  sort: number
  enabled: boolean
  is_builtin: boolean
}
export const personaApi = {
  list: (params?: { category?: string; enabled?: boolean }) =>
    http.get<PersonaTemplate[]>('/persona-templates', { params }).then((r) => r.data),
  create: (data: Partial<PersonaTemplate>) =>
    http.post<PersonaTemplate>('/persona-templates', data).then((r) => r.data),
  update: (id: number, data: Partial<PersonaTemplate>) =>
    http.patch<PersonaTemplate>(`/persona-templates/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/persona-templates/${id}`).then((r) => r.data),
  clone: (id: number) => http.post<PersonaTemplate>(`/persona-templates/${id}/clone`).then((r) => r.data),
}

export const skillApi = {
  list: () => http.get<Skill[]>('/skills').then((r) => r.data),
  create: (data: Partial<Skill>) => http.post<Skill>('/skills', data).then((r) => r.data),
  update: (id: number, data: Partial<Skill>) => http.patch<Skill>(`/skills/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/skills/${id}`).then((r) => r.data),
  importUpload: (file: File, subpath?: string) => {
    const fd = new FormData()
    fd.append('file', file)
    const q = subpath ? `?subpath=${encodeURIComponent(subpath)}` : ''
    return http.post<SkillImportResult>(`/skills/import/upload${q}`, fd).then((r) => r.data)
  },
  importUrl: (url: string, subpath?: string) =>
    http.post<SkillImportResult>('/skills/import/url', { url, subpath }).then((r) => r.data),
  importCollection: (url: string) =>
    http.post<SkillImportResult>('/skills/import/collection', { url }).then((r) => r.data),
  importCollectionUpload: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return http.post<SkillImportResult>('/skills/import/collection/upload', fd).then((r) => r.data)
  },
  importMarkdown: (text: string, filename?: string) =>
    http.post<SkillImportResult>('/skills/import/markdown', { text, filename }).then((r) => r.data),
  parseMarkdown: (text: string) =>
    http.post<{ name: string; description: string; has_frontmatter: boolean; meta: Record<string, any>; body_preview: string }>('/skills/parse/markdown', { text }).then((r) => r.data),
  upgradeUpload: (skillId: number, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return http.post<{ skill_id: number; version?: string; files: number }>(`/skills/${skillId}/upgrade/upload`, fd).then((r) => r.data)
  },
  upgradeUrl: (skillId: number, url: string) =>
    http.post<{ skill_id: number; version?: string; files: number }>(`/skills/${skillId}/upgrade`, { url }).then((r) => r.data),
  runScript: (skillId: number, script: string, args: Record<string, any>, extraArgs: string[] = []) =>
    http.post<{ ok: boolean; content: string; is_error: boolean }>(`/skills/${skillId}/scripts/run`, { script, args, extra_args: extraArgs }).then((r) => r.data),
  getPackage: (skillId: number) => http.get<SkillPackageInfo>(`/skills/${skillId}/package`).then((r) => r.data),
  readPackageFile: (skillId: number, path: string) =>
    http.get<{ path: string; binary: boolean; size: number; content: string | null }>('/skills/' + skillId + '/package/file', { params: { path } }).then((r) => r.data),
  writePackageFile: (skillId: number, path: string, content: string) =>
    http.put(`/skills/${skillId}/package/file`, { path, content }).then((r) => r.data),
  toggleScripts: (skillId: number, enabled: boolean) =>
    http.post<{ scripts_enabled: boolean; entry_scripts: SkillScriptEntry[] }>(`/skills/${skillId}/package/scripts?enabled=${enabled}`).then((r) => r.data),
  installDeps: (skillId: number) =>
    http.post<{ ok: boolean; message: string; log: string }>(`/skills/${skillId}/package/install-deps`).then((r) => r.data),
}

export interface SkillImportResult {
  duplicate?: boolean
  multi_skill?: boolean
  collection?: boolean
  skill_id?: number
  package_id?: number
  name?: string
  files?: number
  count?: number
  existing_skill_id?: number
  existing_skill_name?: string
  sub_skills?: string[]
  children?: { id: number; name: string; subpath: string }[]
}
export interface SkillPackageInfo {
  id: number
  name: string
  source_type: string
  source_uri?: string
  version?: string
  files?: { path: string; size: number }[]
  entry_scripts?: SkillScriptEntry[]
  scripts_enabled: boolean
  manifest?: Record<string, any>
  capabilities?: {
    has_node_manifest: boolean
    has_python_reqs: boolean
    needs_node_install: boolean
    has_manifest: boolean
  }
  manifest_template?: string
}

export interface SkillScriptEntry {
  name: string
  path: string
  description?: string
  runtime?: string
}

// ---- 技能市场 ----
export interface MarketInstalledRef {
  skill_id: number | null
  name: string
  version?: string | null
  subpath?: string
}

export interface MarketCandidate {
  repo: string
  name: string
  description: string
  stars: number
  html_url: string
  sub_skills: string[]
  source: 'curated' | 'github'
  installed: MarketInstalledRef | null
}

export interface MarketSearchResult {
  candidates: MarketCandidate[]
  mapped_terms: string[]
  error: string | null
  note: string | null
}

export interface MarketDetail {
  repo: string
  subpath: string
  name: string
  description: string
  version?: string | null
  body_md: string
  files: { path: string; size: number }[]
  entry_scripts: SkillScriptEntry[]
  html_url: string
  limitations: string[]
}

export const skillMarketApi = {
  search: (q: string, limit = 20) =>
    http.get<MarketSearchResult>('/skills/market/search', { params: { q, limit } }).then((r) => r.data),
  detail: (repo: string, subpath = '') =>
    http.get<MarketDetail>('/skills/market/detail', { params: { repo, subpath } }).then((r) => r.data),
  install: (repo: string, subpath?: string, upgradeSkillId?: number) =>
    http.post<SkillImportResult>('/skills/market/install', {
      repo,
      subpath: subpath || null,
      upgrade_skill_id: upgradeSkillId ?? null,
    }).then((r) => r.data),
  installCollection: (repo: string) =>
    http.post<SkillImportResult>('/skills/market/install-collection', { repo }).then((r) => r.data),
}

export const toolApi = {
  list: () => http.get<ToolItem[]>('/tools').then((r) => r.data),
  adminList: () => http.get<{ name: string; description: string; permission: string; kind: string }[]>('/tools/admin-list').then((r) => r.data),
  create: (data: Partial<ToolItem>) => http.post<ToolItem>('/tools', data).then((r) => r.data),
  remove: (id: number) => http.delete(`/tools/${id}`).then((r) => r.data),
  test: (id: number, args: Record<string, any>) => http.post(`/tools/${id}/test`, { args }).then((r) => r.data),
}

export const mcpApi = {
  list: () => http.get<any[]>('/mcp/servers').then((r) => r.data),
  create: (data: Record<string, any>) => http.post('/mcp/servers', data).then((r) => r.data),
  update: (id: number, data: Record<string, any>) => http.patch(`/mcp/servers/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/mcp/servers/${id}`).then((r) => r.data),
  test: (id: number) => http.post(`/mcp/servers/${id}/test`).then((r) => r.data),
  sync: (id: number) => http.post(`/mcp/servers/${id}/sync`).then((r) => r.data),
  tools: (id: number) => http.get<any[]>(`/mcp/servers/${id}/tools`).then((r) => r.data),
  call: (id: number, toolName: string, args: Record<string, any>) => http.post(`/mcp/servers/${id}/tools/${toolName}/call`, { args }).then((r) => r.data),
}

// ---------------- 定时任务（群管机器人用） ----------------

export const scheduledApi = {
  list: () => http.get<any[]>('/scheduled-tasks').then((r) => r.data),
  create: (data: any) => http.post('/scheduled-tasks', data).then((r) => r.data),
  update: (id: number, data: any) => http.patch(`/scheduled-tasks/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/scheduled-tasks/${id}`).then((r) => r.data),
  enable: (id: number, enabled: boolean) => http.post(`/scheduled-tasks/${id}/enable`, { enabled }).then((r) => r.data),
  runNow: (id: number) => http.post(`/scheduled-tasks/${id}/run-now`).then((r) => r.data),
  runs: (id: number, page = 1, pageSize = 20) =>
    http.get<{ total: number; items: any[] }>(`/scheduled-tasks/${id}/runs`, { params: { page, page_size: pageSize } }).then((r) => r.data),
  createWebhook: (id: number, name = '默认') => http.post<{ id: number; token: string; webhook_url: string }>(`/scheduled-tasks/${id}/webhook-token`, { name }).then((r) => r.data),
  webhookTokens: (id: number) => http.get<{ id: number; name: string; enabled: boolean }[]>(`/scheduled-tasks/${id}/webhook-tokens`).then((r) => r.data),
  removeWebhook: (tokenId: number) => http.delete(`/webhook-tokens/${tokenId}`).then((r) => r.data),
}

// ---------------- 检索调试 ----------------

export const retrievalApi = {
  query: (query: string, kbIds: number[], opts: { topK?: number; useHybrid?: boolean; scoreThreshold?: number } = {}) =>
    http.post<{ query: string; chunks: RetrievedChunk[]; timing_ms: number }>('/retrieval/query', {
      query, kb_ids: kbIds, top_k: opts.topK ?? 5, use_hybrid: opts.useHybrid ?? true, score_threshold: opts.scoreThreshold ?? 0,
    }).then((r) => r.data),
}

// ---------------- Provider / 模型管理 ----------------

export const providerApiFull = {
  list: () => http.get<any[]>('/providers').then((r) => r.data),
  create: (data: any) => http.post('/providers', data).then((r) => r.data),
  update: (id: number, data: any) => http.patch(`/providers/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/providers/${id}`).then((r) => r.data),
  test: (id: number, purpose: string, modelName?: string) => http.post(`/providers/${id}/test`, { purpose, model_name: modelName }).then((r) => r.data),
  health: (id: number, purpose = 'chat') => http.post<{ ok: boolean; message: string; latency_ms: number; health_status: string; last_check_at: number }>(`/providers/${id}/health`, null, { params: { purpose } }).then((r) => r.data),
  listModels: (id: number) => http.get<{ ok: boolean; models: { id: string; owned_by?: string }[]; message?: string }>(`/providers/${id}/models`).then((r) => r.data),
  configs: () => http.get<ModelConfig[]>('/providers/configs/all').then((r) => r.data),
  createConfig: (data: any) => http.post<ModelConfig>('/providers/configs', data).then((r) => r.data),
  updateConfig: (id: number, data: any) => http.patch<ModelConfig>(`/providers/configs/${id}`, data).then((r) => r.data),
  removeConfig: (id: number) => http.delete(`/providers/configs/${id}`).then((r) => r.data),
}

// ---------------- 待办 / 日程 / 提醒 ----------------

export interface ReminderItem {
  id: number
  title: string
  content?: string | null
  due_at?: number | null
  status: string
  assignee_id?: number | null
  creator_id?: number | null
  source: string
  repeat_cron?: string | null
  remind_before_minutes: number
  notify_on_due: boolean
  notified_at?: number | null
  done_at?: number | null
  created_at?: string
}
export const reminderApi = {
  list: (params: { assignee_id?: number; status?: string } = {}) => http.get<ReminderItem[]>('/reminders', { params }).then((r) => r.data),
  create: (data: Partial<ReminderItem>) => http.post<ReminderItem>('/reminders', data).then((r) => r.data),
  update: (id: number, data: Partial<ReminderItem>) => http.patch<ReminderItem>(`/reminders/${id}`, data).then((r) => r.data),
  done: (id: number) => http.post<ReminderItem>(`/reminders/${id}/done`).then((r) => r.data),
  remove: (id: number) => http.delete(`/reminders/${id}`).then((r) => r.data),
}

// ---------------- 工作流 ----------------

export const workflowApi = {
  get: (agentId: number) => http.get<any>(`/agents/${agentId}/workflow`).then((r) => r.data),
  save: (agentId: number, graph: any) => http.put(`/agents/${agentId}/workflow`, { graph }).then((r) => r.data),
  publish: (agentId: number) => http.post(`/agents/${agentId}/workflow/publish`).then((r) => r.data),
  run: (agentId: number, inputs: Record<string, any>) => http.post(`/agents/${agentId}/workflow/run`, { inputs }).then((r) => r.data),
  getRun: (runId: number) => http.get(`/workflow-runs/${runId}`).then((r) => r.data),
  listRuns: (agentId: number, limit = 20, status?: string) =>
    http.get<{ runs: any[]; status_counts: Record<string, number> }>(
      `/agents/${agentId}/workflow/runs`, { params: { limit, status: status || undefined } },
    ).then((r) => r.data),
  approveUrl: (runId: number) => `${API_BASE}/workflow-runs/${runId}/approve`,
  pendingApprovals: () =>
    http.get<{ run_id: number; agent_id: number; agent_name: string; pending_node_id?: string | null; input?: any; created_at?: string }[]>('/approvals/pending').then((r) => r.data),
  generate: (agentId: number, prompt: string, modelConfigId?: number) =>
    http.post<WorkflowGenerateResult>(`/agents/${agentId}/workflow/generate`, {
      prompt,
      model_config_id: modelConfigId ?? null,
    }).then((r) => r.data),
  nodeTypes: () =>
    http.get<{ types: string[]; specs: Record<string, string>; extra: Record<string, any> }>('/workflow/node-types').then((r) => r.data),
  resend: (runId: number) =>
    http.post<{ ok: boolean; message: string }>(`/workflow-runs/${runId}/resend`).then((r) => r.data),
  debug: (agentId: number, message: string, history: { role: string; content: string }[] = [], modelConfigId?: number) =>
    http.post<WorkflowDebugResult>(`/agents/${agentId}/workflow/debug`, {
      message,
      history,
      model_config_id: modelConfigId ?? null,
    }).then((r) => r.data),

  // ---- 外部接入 ----
  listHooks: (agentId?: number) =>
    http.get<any[]>('/workflow-hooks', { params: agentId ? { agent_id: agentId } : {} }).then((r) => r.data),
  createHook: (agentId: number, path?: string, opts?: { name?: string; rateLimit?: number; callbackUrl?: string }) =>
    http.post<{
      id: number; token: string; hook_url: string; path?: string | null
      rate_limit_per_min: number; callback_url?: string | null
    }>('/workflow-hooks', {
      agent_id: agentId,
      path: path || null,
      name: opts?.name || '默认',
      rate_limit_per_min: opts?.rateLimit ?? 60,
      callback_url: opts?.callbackUrl || null,
    }).then((r) => r.data),
  toggleHook: (id: number, enabled: boolean) =>
    http.post<{ enabled: boolean }>(`/workflow-hooks/${id}/toggle`, null, { params: { enabled } }).then((r) => r.data),
  removeHook: (id: number) => http.delete(`/workflow-hooks/${id}`).then((r) => r.data),

  // ---- 测试用例 ----
  listCases: (agentId: number) =>
    http.get<any[]>(`/agents/${agentId}/workflow/cases`).then((r) => r.data),
  createCase: (agentId: number, data: { name: string; input: any; expected?: string | null; assert_contains?: boolean }) =>
    http.post(`/agents/${agentId}/workflow/cases`, data).then((r) => r.data),
  updateCase: (caseId: number, data: Record<string, any>) =>
    http.patch(`/workflow-cases/${caseId}`, data).then((r) => r.data),
  removeCase: (caseId: number) => http.delete(`/workflow-cases/${caseId}`).then((r) => r.data),
  runCases: (agentId: number, caseIds?: number[]) =>
    http.post<{ total: number; passed: number; failed: number; results: any[] }>(
      `/agents/${agentId}/workflow/cases/run`, { case_ids: caseIds || null },
    ).then((r) => r.data),

  // ---- 取消 / 版本 / 导入导出 ----
  cancelRun: (runId: number) =>
    http.post<{ ok: boolean; signalled: boolean; message: string }>(
      `/workflow-runs/${runId}/cancel`,
    ).then((r) => r.data),
  listVersions: (agentId: number) =>
    http.get<{ id: number; version: number; note?: string | null; node_count: number; edge_count: number; created_at?: string }[]>(
      `/agents/${agentId}/workflow/versions`,
    ).then((r) => r.data),
  rollbackVersion: (agentId: number, versionId: number) =>
    http.post<{ message: string; version: number }>(
      `/agents/${agentId}/workflow/versions/${versionId}/rollback`,
    ).then((r) => r.data),
  exportWorkflow: (agentId: number) =>
    http.get<any>(`/agents/${agentId}/workflow/export`).then((r) => r.data),
  importWorkflow: (data: any) =>
    http.post<{ message: string; agent_id: number; workflow_id: number; node_count: number }>(
      '/workflow/import', data,
    ).then((r) => r.data),
  usage: (agentId: number, days = 30) =>
    http.get<{
      days: number
      total: { prompt_tokens: number; completion_tokens: number; calls: number; runs: number }
      series: { date: string; prompt_tokens: number; completion_tokens: number; calls: number; runs: number }[]
    }>(`/agents/${agentId}/workflow/usage`, { params: { days } }).then((r) => r.data),

  // ---- 模板 ----
  listTemplates: () =>
    http.get<{ builtin: WorkflowTemplateItem[]; user: WorkflowTemplateItem[]; public: WorkflowTemplateItem[] }>(
      '/workflow/templates',
    ).then((r) => r.data),
  saveAsTemplate: (agentId: number, data: { name?: string; category?: string; description?: string; include_cases?: boolean }) =>
    http.post<{ id: number; name: string; node_count: number; case_count: number }>(
      `/agents/${agentId}/workflow/save-as-template`, data,
    ).then((r) => r.data),
  publishTemplate: (id: number, isPublic = true) =>
    http.post<{
      visibility: string; review_status: string; risk_level?: string
      blocked?: boolean; message?: string
    }>(`/workflow/templates/${id}/publish`, null, { params: { public: isPublic } }).then((r) => r.data),
  listPendingReviews: () =>
    http.get<any[]>('/workflow/templates/pending').then((r) => r.data),
  reviewTemplate: (id: number, approve: boolean, note?: string) =>
    http.post<{ review_status: string; visibility: string; message: string }>(
      `/workflow/templates/${id}/review`, { approve, note: note || null },
    ).then((r) => r.data),
  rateTemplate: (id: number, rating: number, comment?: string) =>
    http.post<{ rating: number; rating_count: number; my_rating: number }>(
      `/workflow/templates/${id}/rate`, { rating, comment: comment || null },
    ).then((r) => r.data),
  deleteTemplate: (id: number) => http.delete(`/workflow/templates/${id}`).then((r) => r.data),
  applyTemplate: (data: { key?: string; template_id?: number; name?: string }) =>
    http.post<{ agent_id: number; workflow_id: number; name: string; node_count: number; case_count: number }>(
      '/workflow/templates/apply', data,
    ).then((r) => r.data),
}

export interface WorkflowTemplateItem {
  id?: number
  key?: string
  name: string
  category: string
  description: string
  tags: string[]
  node_count: number
  case_count: number
  source: 'builtin' | 'user' | 'public'
  visibility?: 'private' | 'pending' | 'public'
  apply_count?: number
  risk_level?: 'none' | 'low' | 'medium' | 'high'
  review_status?: 'none' | 'pending' | 'approved' | 'rejected'
  rating?: number | null
  rating_count?: number
  risk_findings?: { node_id: string; node_type: string; level: string; reason: string; hint: string }[]
}

export interface WorkflowDebugResult {
  text: string
  error: string | null
  paused: { node_id: string; title?: string } | null
  trace: {
    node_id: string
    status: string
    latency_ms?: number
    input?: any
    output?: any
    error?: string
  }[]
  latency_ms: number
  final: any
}

export interface WorkflowGenerateResult {
  graph: { nodes: any[]; edges: any[]; name?: string; description?: string }
  warnings: string[]
  name: string
  description: string
  node_count: number
}
export async function streamWorkflowRun(
  agentId: number,
  inputs: Record<string, any>,
  handlers: { onNodeStarted?: (d: any) => void; onNodeFinished?: (d: any) => void; onFinished?: (d: any) => void; onApprovalRequired?: (d: any) => void },
) {
  const token = localStorage.getItem('access_token')
  const resp = await fetch(`${API_BASE}/agents/${agentId}/workflow/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify({ inputs }),
  })
  if (!resp.body) throw new Error('无响应流')
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let event = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''
    for (const line of lines) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) {
        const raw = line.slice(5).trim()
        if (!raw) continue
        let data: any
        try { data = JSON.parse(raw) } catch { continue }
        if (event === 'node_started') handlers.onNodeStarted?.(data)
        else if (event === 'node_finished') handlers.onNodeFinished?.(data)
        else if (event === 'approval_required') handlers.onApprovalRequired?.(data)
        else if (event === 'run_finished') handlers.onFinished?.(data)
      }
    }
  }
}

// ---------------- 审计 / API Key / SSO ----------------

export interface AuditLogItem {
  id: number
  actor_id?: number
  actor_type: string
  actor_name?: string
  action: string
  resource_type?: string
  resource_id?: string
  before?: Record<string, any>
  after?: Record<string, any>
  result: string
  error?: string
  ip?: string
  user_agent?: string
  request_id?: string
  created_at: number
}
export const auditApi = {
  list: (params: Record<string, any> = {}) => http.get<{ items: AuditLogItem[]; total: number }>('/audit-logs', { params }).then((r) => r.data),
}

export interface ApiKeyItem {
  id: number
  name: string
  key_prefix: string
  user_id?: number
  scopes?: string[]
  kb_ids?: number[]
  rate_limit: number
  status: string
  expires_at?: string
  last_used_at?: string
}
export const apiKeyApi = {
  list: () => http.get<ApiKeyItem[]>('/api-keys').then((r) => r.data),
  create: (data: Record<string, any>) => http.post<{ id: number; name: string; key: string; key_prefix: string }>('/api-keys', data).then((r) => r.data),
  revoke: (id: number) => http.delete(`/api-keys/${id}`).then((r) => r.data),
}

export interface SsoProvider { provider: string; name: string }
export interface SsoConfigItem {
  id?: number
  provider: string
  name: string
  enabled: boolean
  client_id?: string
  authorize_url?: string
  token_url?: string
  userinfo_url?: string
  corp_id?: string
  agent_id?: string
  redirect_uri?: string
}
export const ssoApi = {
  providers: () => http.get<SsoProvider[]>('/auth/sso/providers').then((r) => r.data),
  // 可配置的提供者类型（配置页用；不依赖是否已配置）
  supported: () => http.get<SsoProvider[]>('/auth/sso/supported').then((r) => r.data),
  configs: () => http.get<SsoConfigItem[]>('/auth/sso/configs/all').then((r) => r.data),
  saveConfig: (data: Record<string, any>) => http.post('/auth/sso/configs', data).then((r) => r.data),
  loginUrl: (provider: string) => `${API_BASE}/auth/sso/${provider}/login`,
  // 当前账号的第三方绑定情况（含可绑定的 provider 列表）
  bindings: () =>
    http
      .get<{ bound_provider: string | null; bound_subject: string | null; available: string[] }>(
        '/auth/sso/bindings',
      )
      .then((r) => r.data),
  // 发起绑定：返回 IdP 授权地址（需整页跳转）
  startBind: (provider: string) =>
    http.post<{ authorize_url: string }>(`/auth/sso/${provider}/bind`).then((r) => r.data),
  unbind: () => http.post<{ message: string }>('/auth/sso/unbind').then((r) => r.data),
}

// ---------------- 内容安全 ----------------

export interface GuardStatus {
  enabled: boolean
  block_threshold: number
  flag_threshold: number
  wrap_context: boolean
  sensitive_word_count: number
  engine: string
}
export interface GuardDetectResult {
  risk_score: number
  risk_level: string
  action: string
  summary: string
  hits: { category: string; rule: string; snippet: string }[]
}
export const securityApi = {
  status: () => http.get<GuardStatus>('/security/status').then((r) => r.data),
  detect: (text: string) => http.post<GuardDetectResult>('/security/detect', { text }).then((r) => r.data),
}

// ---------------- 文件管理（列表/删除） ----------------

export interface FileItem {
  id: number
  file_name: string
  file_ext?: string
  mime?: string
  size: number
  source: 'generated' | 'upload'
  user_id: number
  user_name?: string
  conversation_id?: number
  created_at: string
  expires_at?: number | null
}
export const fileAdminApi = {
  list: (params: Record<string, any> = {}) => http.get<{ total: number; items: FileItem[] }>('/files', { params }).then((r) => r.data),
  remove: (id: number) => http.delete(`/files/${id}`).then((r) => r.data),
  cleanup: () => http.post('/files/cleanup').then((r) => r.data),
}

// ---------------- 系统设置 / 运维 ----------------

export interface SettingField {
  key: string
  label: string
  group: string
  kind: 'bool' | 'int' | 'float' | 'str' | 'csv' | 'secret' | 'json'
  restart: boolean
  help?: string
  is_set?: boolean | null
  value: any
}
export const settingsApi = {
  get: () => http.get<{ groups: { key: string; label: string }[]; fields: SettingField[] }>('/system/settings').then((r) => r.data),
  update: (updates: Record<string, any>) => http.put<{ message: string; restart_required: boolean; changed: string[] }>('/system/settings', { updates }).then((r) => r.data),
  restart: () => http.post<{ message: string }>('/system/settings/restart').then((r) => r.data),
}

// ---------------- 浏览器管理 ----------------
/** 「本机没有任何可用浏览器」时的安装引导（对应 browser_manager.missing_browser_guide） */
export interface BrowserInstallOption {
  kind: 'bundled' | 'system'
  recommended: boolean
  title: string
  detail: string
  route?: string
  query?: Record<string, string>
  requires_permission?: string
  links?: { name: string; url: string }[]
}
export interface BrowserInstallGuide {
  title: string
  summary: string
  options: BrowserInstallOption[]
}

export interface BrowserStatus {
  running: boolean
  managed: boolean
  pid: number | null
  port: number
  port_ready: boolean
  profile_dir: string
  started_at: number | null
  error: string | null
  bundled_installed?: boolean
  browsers: { name: string; family: string; path: string; source?: 'bundled' | 'system' }[]
  /** 一个浏览器都没检测到（前端据此直接展示安装引导，而不是等点「启动」才报错） */
  no_browser?: boolean
  install_guide?: BrowserInstallGuide | null
  message?: string
}
export const browserApi = {
  status: () => http.get<BrowserStatus>('/browser/status').then((r) => r.data),
  launch: (params: { executable?: string; port?: number; headless?: boolean }) =>
    http.post<BrowserStatus>('/browser/launch', null, { params }).then((r) => r.data),
  stop: () => http.post<BrowserStatus>('/browser/stop').then((r) => r.data),
}

// ---------------- 可选组件（浏览器运行时等，用户点按安装） ----------------
export interface ComponentProgress {
  phase: 'idle' | 'downloading' | 'verifying' | 'extracting' | 'done' | 'failed'
  received: number
  total: number
  error: string | null
  actual_sha256: string | null
  updated_at: number | null
  installed?: boolean
  executable?: string | null
}
export interface ComponentStatus {
  key: string
  name: string
  summary: string
  detail: string
  version: string
  platform: string
  supported: boolean
  hash_pinned?: boolean
  requires_gui: boolean
  hint: string
  installed: boolean
  installed_version: string | null
  install_path: string | null
  executable: string | null
  size_bytes: number | null
  verified: boolean
  source: string | null
  installed_at: number | null
  progress: ComponentProgress
}
export const componentApi = {
  list: () => http.get<ComponentStatus[]>('/components').then((r) => r.data),
  status: (key: string) => http.get<ComponentStatus>(`/components/${key}`).then((r) => r.data),
  install: (key: string, body: { source: 'official' | 'url'; url?: string }) =>
    http.post<{ started: boolean; reason?: string }>(`/components/${key}/install`, body).then((r) => r.data),
  installUpload: (key: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return http.post<{ started: boolean; reason?: string }>(`/components/${key}/install-upload`, fd).then((r) => r.data)
  },
  progress: (key: string) => http.get<ComponentProgress>(`/components/${key}/progress`).then((r) => r.data),
  remove: (key: string) => http.delete(`/components/${key}`).then((r) => r.data),
}

export interface UpdateAsset { name: string; size: number; digest?: string | null; url: string }
export interface UpdateCheckResult {
  ok: boolean
  current: string
  latest?: string | null
  tag?: string
  update_available: boolean
  prerelease?: boolean
  published_at?: string | null
  notes?: string
  html_url?: string | null
  deployment: string
  repo?: string
  releases_url?: string
  can_self_update?: boolean
  asset?: UpdateAsset | null
  hint?: string
  cached?: boolean
  disabled?: boolean
  error?: string
}
export const updateApi = {
  check: (force = false) =>
    http.get<UpdateCheckResult>('/system/update/check', { params: { force } }).then((r) => r.data),
  download: () =>
    http.post<{ ok: boolean; version?: string; verified?: boolean; size?: number; message?: string; error?: string }>(
      '/system/update/download',
      {},
      { timeout: 0 }, // 208MB 下载，不用默认超时
    ).then((r) => r.data),
  progress: () =>
    http.get<{ phase: string; total: number; received: number; error?: string | null; version?: string | null }>(
      '/system/update/progress',
    ).then((r) => r.data),
  apply: () => http.post<{ ok: boolean; message?: string; error?: string }>('/system/update/apply', {}).then((r) => r.data),
}

// ---------------- 更新日志（所有已登录用户可见） ----------------

export interface ChangelogSection { title: string; items: string[] }
export interface ChangelogVersion {
  version: string
  date?: string | null
  url?: string | null
  sections: ChangelogSection[]
}
export interface ChangelogResult {
  ok: boolean
  source: 'local' | 'release' | 'none'
  current: string
  latest: ChangelogVersion | null
  history: ChangelogVersion[]
  cached?: boolean
}
export const changelogApi = {
  get: (force = false) => http.get<ChangelogResult>('/changelog', { params: { force } }).then((r) => r.data),
}

export interface SystemInfo {
  db_kind: string
  version: string
  deployment: string
  db_url_masked: string
  db_file?: string | null
  db_file_size: number
  driver_required: string[]
  driver_ok: boolean
  vector_backend: string
  task_backend: string
  data_dir: string
  data_dir_size: number
  files_dir_size: number
  uptime_seconds: number
  table_count: number
  python: string
  platform: string
  frozen: boolean
}
export interface DbTestResult { ok: boolean; kind?: string; message: string; driver_missing?: string[] }
export const systemOpsApi = {
  info: () => http.get<SystemInfo>('/system/info').then((r) => r.data),
  drivers: () => http.get<Record<string, { packages: string[]; ok: boolean }>>('/system/db/drivers').then((r) => r.data),
  testDb: (database_url: string) => http.post<DbTestResult>('/system/db/test', { database_url }).then((r) => r.data),
  installDriver: (target: string) => http.post<{ ok: boolean; message: string; log: string; packages?: string[] }>('/system/db/install-driver', { target }).then((r) => r.data),
  initDb: (database_url: string) => http.post<{ ok: boolean; message: string; table_count?: number }>('/system/db/init', { database_url }).then((r) => r.data),
  saveRestart: (database_url: string) => http.post<{ ok: boolean; message: string }>('/system/db/save-restart', { database_url }).then((r) => r.data),
  migrate: () => http.post<{ ok: boolean; message: string }>('/system/db/migrate').then((r) => r.data),
  reset: () => http.post<{ ok: boolean; message: string }>('/system/db/reset').then((r) => r.data),
  backupUrl: () => '/api/v1/system/db/backup',
  listBackups: () => http.get<{ name: string; size: number; created_at: number }[]>('/system/db/backups').then((r) => r.data),
  createBackup: () => http.post<{ ok: boolean; name: string; size: number; message: string }>('/system/db/backups').then((r) => r.data),
  backupDownloadUrl: (name: string) => `/api/v1/system/db/backups/${encodeURIComponent(name)}/download`,
  deleteBackup: (name: string) => http.delete<{ ok: boolean; message: string }>(`/system/db/backups/${encodeURIComponent(name)}`).then((r) => r.data),
  restore: (file: File) => { const fd = new FormData(); fd.append('file', file); return http.post<{ ok: boolean; message: string }>('/system/db/restore', fd).then((r) => r.data) },
  seed: () => http.post<{ ok: boolean; message: string }>('/system/seed').then((r) => r.data),
  resetAdminPassword: (username: string, new_password: string) => http.post<{ ok: boolean; message: string }>('/system/reset-admin-password', { username, new_password }).then((r) => r.data),
  cleanup: (scope = 'all') => http.post<{ ok: boolean; message: string; removed: number; freed_bytes: number }>('/system/cleanup', { scope }).then((r) => r.data),
}

export const systemApi = {
  logs: (params: { lines?: number; level?: string; keyword?: string } = {}) => http.get<{ lines: string[]; path: string; total: number }>('/system/logs', { params }).then((r) => r.data),
  downloadUrl: () => `/api/v1/system/logs/download`,
  logStats: () => http.get<{ path: string; total_bytes: number; file_count: number; max_bytes: number; backup_count: number; retention_days: number; cleanup_interval_hours: number }>('/system/logs/stats').then((r) => r.data),
  clearLogs: () => http.post<{ message: string; removed: number; freed_bytes: number }>('/system/logs/clear').then((r) => r.data),
}

// ---------------- 通知渠道 / 事件订阅 / 邮件入库 ----------------

export interface NotifyChannelItem {
  id: number
  kind: 'inapp' | 'webhook' | 'wecom' | 'dingtalk' | 'smtp' | 'external'
  name: string
  config?: Record<string, any> | null
  enabled: boolean
  events?: string | null
}
export const notifyChannelApi = {
  list: () => http.get<NotifyChannelItem[]>('/notify-channels').then((r) => r.data),
  create: (data: Record<string, any>) => http.post<NotifyChannelItem>('/notify-channels', data).then((r) => r.data),
  update: (id: number, data: Record<string, any>) => http.patch<NotifyChannelItem>(`/notify-channels/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/notify-channels/${id}`).then((r) => r.data),
  test: (id: number) => http.post<{ ok: boolean; message: string }>(`/notify-channels/${id}/test`).then((r) => r.data),
}

export interface EventSubItem { id: number; name: string; url: string; events?: string | null; secret_set: boolean; enabled: boolean }
export const eventSubApi = {
  list: () => http.get<EventSubItem[]>('/event-subscriptions').then((r) => r.data),
  events: () => http.get<string[]>('/event-subscriptions/events').then((r) => r.data),
  create: (data: Record<string, any>) => http.post<EventSubItem>('/event-subscriptions', data).then((r) => r.data),
  update: (id: number, data: Record<string, any>) => http.patch<EventSubItem>(`/event-subscriptions/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/event-subscriptions/${id}`).then((r) => r.data),
  test: (id: number) => http.post<{ ok: boolean; message: string }>(`/event-subscriptions/${id}/test`).then((r) => r.data),
}

export interface EmailSourceItem {
  id: number
  name: string
  source_account_id?: number | null
  imap_host: string
  imap_port: number
  use_ssl: boolean
  username: string
  password_set: boolean
  folder: string
  kb_id: number
  ingest_mode: 'both' | 'attach' | 'body'
  allow_from?: string | null
  subject_keywords?: string | null
  auto_tags?: string[] | null
  tag_rules?: Record<string, string[]> | null
  uid_cursor?: number
  enabled: boolean
  status: string
  last_error?: string | null
  last_sync_at?: number | null
  ingested_count: number
}
export const emailSourceApi = {
  list: () => http.get<EmailSourceItem[]>('/email-sources').then((r) => r.data),
  myAccounts: () => http.get<{ id: number; label: string; email_addr: string; folder: string }[]>('/email-sources/my-accounts').then((r) => r.data),
  create: (data: Record<string, any>) => http.post<EmailSourceItem>('/email-sources', data).then((r) => r.data),
  update: (id: number, data: Record<string, any>) => http.patch<EmailSourceItem>(`/email-sources/${id}`, data).then((r) => r.data),
  remove: (id: number) => http.delete(`/email-sources/${id}`).then((r) => r.data),
  test: (id: number) => http.post<{ ok: boolean; message: string }>(`/email-sources/${id}/test`).then((r) => r.data),
  sync: (id: number) => http.post<{ ok: boolean; message: string; ingested: number }>(`/email-sources/${id}/sync`).then((r) => r.data),
}

// ---------------- 个人邮件 ----------------
export interface MailAccountItem {
  id: number
  label: string
  email_addr: string
  from_name?: string | null
  imap_host: string
  imap_port: number
  imap_use_ssl: boolean
  imap_username: string
  imap_password_set: boolean
  folder: string
  smtp_host?: string | null
  smtp_port: number
  smtp_use_ssl: boolean
  smtp_starttls: boolean
  smtp_username?: string | null
  smtp_password_set: boolean
  auto_ingest: boolean
  default_kb_id?: number | null
  allow_from?: string | null
  subject_keywords?: string | null
  auto_tags?: string[] | null
  last_poll_at?: number | null
  ingested_count: number
  enabled: boolean
  status: string
  last_error?: string | null
}

export interface MailMessageItem {
  id: number
  account_id: number
  folder: string
  subject: string
  from_addr: string
  from_name?: string | null
  to_addrs?: string[] | null
  date?: number | null
  snippet?: string | null
  seen: boolean
  answered: boolean
  starred: boolean
  has_attachments: boolean
  document_id?: number | null
  body_text?: string | null
  body_html?: string | null
  cc_addrs?: string[] | null
  attachments?: { filename: string; size?: number; storage_path?: string }[] | null
}

export const mailApi = {
  listAccounts: () => http.get<MailAccountItem[]>('/mail/accounts').then((r) => r.data),
  createAccount: (data: Record<string, any>) => http.post<MailAccountItem>('/mail/accounts', data).then((r) => r.data),
  updateAccount: (id: number, data: Record<string, any>) => http.put<MailAccountItem>(`/mail/accounts/${id}`, data).then((r) => r.data),
  removeAccount: (id: number) => http.delete(`/mail/accounts/${id}`).then((r) => r.data),
  testAccount: (id: number) => http.post<{ ok: boolean; message: string }>(`/mail/accounts/${id}/test`).then((r) => r.data),
  syncAccount: (id: number) => http.post<{ ok: boolean; message: string; new: number }>(`/mail/accounts/${id}/sync`).then((r) => r.data),
  messages: (accountId: number, params?: { folder?: string; q?: string; unread?: boolean }) =>
    http.get<MailMessageItem[]>('/mail/messages', { params: { account_id: accountId, ...params } }).then((r) => r.data),
  message: (id: number) => http.get<MailMessageItem>(`/mail/messages/${id}`).then((r) => r.data),
  action: (id: number, action: string) => http.post(`/mail/messages/${id}/${action}`).then((r) => r.data),
  send: (data: { account_id: number; to: string[]; cc?: string[]; subject: string; body_text: string; reply_to_id?: number }) =>
    http.post<{ ok: boolean; message: string }>('/mail/send', data).then((r) => r.data),
}

// ---------------- 客服工单 ----------------

export interface ServiceTicketItem {
  id: number
  status: string
  priority: string
  subject: string
  category?: string | null
  tags?: string[] | null
  source?: string
  channel_id?: number | null
  channel_kind?: string | null
  external_user?: string | null
  conversation_id?: number | null
  assignee_id?: number | null
  messages?: { role: string; content: string; ts: number; agent_id?: number; attachments?: any[] }[] | null
  internal_notes?: { content: string; ts: number; agent_id?: number }[] | null
  last_message_at?: number | null
  closed_at?: number | null
  resolution?: string | null
  first_response_at?: number | null
  sla_due_at?: number | null
  sla_breached?: boolean
  satisfaction?: number | null
  customer_lang?: string | null
  watch?: boolean
}
export interface QuickReplyItem { id: number; title: string; content: string; category?: string | null; scope: string; enabled: boolean; created_by?: number | null }
export const serviceTicketApi = {
  list: (params: { status?: string; assignee_id?: number } = {}) => http.get<ServiceTicketItem[]>('/service-tickets', { params }).then((r) => r.data),
  exportUrl: (status?: string) => `${API_BASE}/service-tickets/export${status ? `?status=${encodeURIComponent(status)}` : ''}`,
  get: (id: number) => http.get<ServiceTicketItem>(`/service-tickets/${id}`).then((r) => r.data),
  create: (data: Record<string, any>) => http.post<ServiceTicketItem>('/service-tickets', data).then((r) => r.data),
  reply: (id: number, content: string) => http.post<ServiceTicketItem>(`/service-tickets/${id}/reply`, { content }).then((r) => r.data),
  update: (id: number, data: Record<string, any>) => http.patch<ServiceTicketItem>(`/service-tickets/${id}`, data).then((r) => r.data),
  addNote: (id: number, content: string) => http.post<ServiceTicketItem>(`/service-tickets/${id}/notes`, { content }).then((r) => r.data),
  context: (id: number) =>
    http.get<{ external_user: string | null; channel_kind: string | null; ticket_total: number; ticket_open: number; history: { id: number; subject: string; status: string; created_at: number }[]; recent_messages: { role: string; content: string; ts: number }[] }>(`/service-tickets/${id}/context`).then((r) => r.data),
  channelConversations: (limit = 50) =>
    http.get<{ id: number; title: string; channel: string; external_id: string | null; message_count: number; last_message?: string; last_at?: number | null; channel_user_id?: number | null; note?: string | null }[]>('/service-tickets/channel-conversations', { params: { limit } }).then((r) => r.data),
  setCustomerNote: (channelUserId: number, content: string) => http.patch<{ ok: boolean; note: string | null }>(`/service-tickets/channel-customers/${channelUserId}/note`, { content }).then((r) => r.data),
  conversationMessages: (convId: number) =>
    http.get<{ id: number; role: string; content: string; attachments: any[]; artifacts: any[]; created_at: number; model?: string | null }[]>(`/service-tickets/conversations/${convId}/messages`).then((r) => r.data),
  replyInConversation: (convId: number, content: string) => http.post<{ ok: boolean; sent_to_channel: boolean }>(`/service-tickets/conversations/${convId}/reply`, { content }).then((r) => r.data),
  conversationMeta: (convId: number) => http.get<{ notes: { content: string; ts: number }[]; watch: boolean }>(`/service-tickets/conversations/${convId}/meta`).then((r) => r.data),
  addConversationNote: (convId: number, content: string) => http.post<{ ok: boolean; notes: any[] }>(`/service-tickets/conversations/${convId}/notes`, { content }).then((r) => r.data),
  setConversationWatch: (convId: number, watch: boolean) => http.patch<{ ok: boolean; watch: boolean }>(`/service-tickets/conversations/${convId}/watch`, null, { params: { watch } }).then((r) => r.data),
  sendConversationAttachment: (convId: number, file: File) => { const fd = new FormData(); fd.append('file', file); return http.post<{ ok: boolean; sent_to_channel: boolean; attachment: any }>(`/service-tickets/conversations/${convId}/attachment`, fd).then((r) => r.data) },
  bulk: (ids: number[], action: string, value?: number | string | null) => http.post<{ updated: number; skipped: number }>('/service-tickets/bulk', { ids, action, value }).then((r) => r.data),
  assign: (id: number, assignee_id: number | null) => http.post<ServiceTicketItem>(`/service-tickets/${id}/assign`, { assignee_id }).then((r) => r.data),
  resolve: (id: number, content?: string) => http.post<ServiceTicketItem>(`/service-tickets/${id}/resolve`, content ? { content } : null).then((r) => r.data),
  quickReplies: () => http.get<QuickReplyItem[]>('/service-tickets/quick-replies').then((r) => r.data),
  createQuickReply: (data: Partial<QuickReplyItem>) => http.post<QuickReplyItem>('/service-tickets/quick-replies', data).then((r) => r.data),
  updateQuickReply: (id: number, data: Partial<QuickReplyItem>) => http.patch<QuickReplyItem>(`/service-tickets/quick-replies/${id}`, data).then((r) => r.data),
  removeQuickReply: (id: number) => http.delete(`/service-tickets/quick-replies/${id}`).then((r) => r.data),
}

// ---------------- 智能录单 ----------------

export interface RecordField { name: string; label: string; type?: string; required?: boolean; desc?: string; options?: any[] }
export interface RecordTemplateItem { id: number; name: string; description?: string | null; fields?: RecordField[] | null; instructions?: string | null; enabled: boolean }
export const recordApi = {
  listTemplates: () => http.get<RecordTemplateItem[]>('/record-templates').then((r) => r.data),
  createTemplate: (data: Record<string, any>) => http.post<RecordTemplateItem>('/record-templates', data).then((r) => r.data),
  updateTemplate: (id: number, data: Record<string, any>) => http.patch<RecordTemplateItem>(`/record-templates/${id}`, data).then((r) => r.data),
  removeTemplate: (id: number) => http.delete(`/record-templates/${id}`).then((r) => r.data),
  extract: (data: { template_id: number; text?: string; doc_id?: number; save?: boolean; source_type?: string; auto_confirm?: boolean }) => http.post<{ count: number; entries: any[]; source_type?: string; status?: string }>('/record-templates/extract', data).then((r) => r.data),
  searchDocs: (keyword?: string) => http.get<{ id: number; title: string; kb_id: number; chars: number }[]>('/record-templates/docs/search', { params: { keyword } }).then((r) => r.data),
  entries: (templateId: number, params?: { keyword?: string; status?: string }) =>
    http.get<{ id: number; data: Record<string, any>; source_type: string; source_ref?: string; raw_text?: string; created_by?: number; created_at?: number; status: string }[]>(`/record-templates/${templateId}/entries`, { params }).then((r) => r.data),
  updateEntry: (entryId: number, data: Record<string, any>, status?: string) => http.patch(`/record-templates/entries/${entryId}`, { data, status }).then((r) => r.data),
  removeEntry: (entryId: number) => http.delete(`/record-templates/entries/${entryId}`).then((r) => r.data),
  batchEntries: (action: 'delete' | 'confirm' | 'unconfirm', ids: number[]) =>
    http.post<{ ok: boolean; affected: number; message: string }>('/record-templates/entries/batch', { action, ids }).then((r) => r.data),
  exportUrl: (templateId: number, fmt: 'xlsx' | 'csv' | 'md' = 'xlsx', status?: string) =>
    `${API_BASE}/record-templates/${templateId}/export?fmt=${fmt}${status ? `&status=${status}` : ''}`,
}

// ---------------- 问答评测 ----------------

export interface EvalDataset { id: number; name: string; description?: string | null; kb_ids?: number[] | null; question_count?: number | null }
export interface EvalQuestion { id: number; dataset_id: number; question: string; expected_answer?: string | null; expected_doc_ids?: number[] | null; sort?: number }
export interface EvalRun { id: number; dataset_id: number; status: string; progress: number; total: number; summary?: Record<string, any> | null; error?: string | null; started_at?: number | null; finished_at?: number | null }
export interface EvalResult { id: number; question: string; answer?: string | null; hit_expected?: boolean | null; faithfulness?: number | null; relevance?: number | null; comment?: string | null; latency_ms?: number | null; error?: string | null }
export const evalApi = {
  datasets: () => http.get<EvalDataset[]>('/eval/datasets').then((r) => r.data),
  createDataset: (data: Partial<EvalDataset>) => http.post<EvalDataset>('/eval/datasets', data).then((r) => r.data),
  updateDataset: (id: number, data: Partial<EvalDataset>) => http.patch<EvalDataset>(`/eval/datasets/${id}`, data).then((r) => r.data),
  removeDataset: (id: number) => http.delete(`/eval/datasets/${id}`).then((r) => r.data),
  questions: (dsId: number) => http.get<EvalQuestion[]>(`/eval/datasets/${dsId}/questions`).then((r) => r.data),
  addQuestion: (dsId: number, data: Partial<EvalQuestion>) => http.post<EvalQuestion>(`/eval/datasets/${dsId}/questions`, data).then((r) => r.data),
  updateQuestion: (qId: number, data: Partial<EvalQuestion>) => http.patch<EvalQuestion>(`/eval/questions/${qId}`, data).then((r) => r.data),
  removeQuestion: (qId: number) => http.delete(`/eval/questions/${qId}`).then((r) => r.data),
  run: (dsId: number, top_k = 5) => http.post<EvalRun>(`/eval/datasets/${dsId}/run`, { top_k }).then((r) => r.data),
  getRun: (runId: number) => http.get<EvalRun>(`/eval/runs/${runId}`).then((r) => r.data),
  runResults: (runId: number) => http.get<EvalResult[]>(`/eval/runs/${runId}/results`).then((r) => r.data),
  exportUrl: (runId: number) => `/api/v1/eval/runs/${runId}/export`,
}

// ---------------- 用量统计补充 ----------------

export const usageApiFull = {
  summary: (days = 30) => http.get<UsageSummary>('/usage/summary', { params: { days } }).then((r) => r.data),
  meToday: () => http.get<UsageTotals>('/usage/me/today').then((r) => r.data),
  tools: (days = 30) => http.get<{ total: number; success: number; failure: number; success_rate: number | null; by_tool: any[]; by_day: { date: string; calls: number }[] }>('/usage/tools', { params: { days } }).then((r) => r.data),
}

// ---------------- 文档 / 知识库明细 ----------------

export const docApiFull = {
  list: (kbId: number) => http.get<Doc[]>('/documents', { params: { kb_id: kbId } }).then((r) => r.data),
  get: (id: number) => http.get<Doc>(`/documents/${id}`).then((r) => r.data),
  remove: (id: number) => http.delete(`/documents/${id}`).then((r) => r.data),
  reprocess: (id: number) => http.post<Doc>(`/documents/${id}/reprocess`).then((r) => r.data),
  content: (id: number) => http.get<{ id: number; title: string; content: string; source: string; page_count: number }>(`/documents/${id}/content`).then((r) => r.data),
  chunks: (id: number, page = 1, pageSize = 50) => http.get<any>(`/documents/${id}/chunks`, { params: { page, page_size: pageSize } }).then((r) => r.data),
}

/** 下载/预览带 token 的 URL */
export function withToken(url: string): string {
  const token = localStorage.getItem('access_token')
  if (!token) return url
  return url + (url.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(token)
}
