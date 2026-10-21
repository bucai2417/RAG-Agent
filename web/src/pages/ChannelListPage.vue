<template>
  <PageContainer
    title="外部渠道"
    subtitle="让团队通过 QQ / 微信 / 企业微信 / 飞书 直接跟本平台对话（知识库问答 + 智能体 + 指令）"
  >
    <template #extra>
      <el-button v-if="canManage" type="primary" :icon="Plus" @click="openModal()">接入渠道</el-button>
    </template>

    <el-skeleton v-if="loading" :rows="4" animated />
    <EmptyState v-else-if="!channels.length" description="尚未接入任何外部渠道" :action-text="canManage ? '接入渠道' : undefined" icon="Promotion" @action="openModal()" />

    <div v-else class="ch-grid">
      <div v-for="c in channels" :key="c.id" class="app-card ch-card">
        <div class="ch-head">
          <span class="ch-title">
            <el-icon class="ch-ico"><component :is="meta(c.kind).icon" /></el-icon>
            {{ c.name }}
            <el-tag size="small" :type="meta(c.kind).tag">{{ meta(c.kind).label }}</el-tag>
          </span>
          <span class="ch-badge">
            <span class="dot" :class="c.connected ? 'on' : c.enabled ? 'warn' : 'off'" />
            {{ c.connected ? '已连接' : c.enabled ? '连接中' : '已停用' }}
          </span>
        </div>
        <p class="ch-hint">{{ meta(c.kind).hint }}</p>

        <div class="ch-info">
          <div class="ci-row"><span class="ci-k">回复模式</span><span>{{ replyModeLabel(c.reply_mode) }}</span></div>
          <div class="ci-row"><span class="ci-k">检索范围</span><span>{{ c.kb_mode === 'off' ? '不检索（纯聊天）' : c.kb_mode === 'custom' ? `指定 ${(c.default_kb_ids || []).length} 个库` : '自动（全部有权库）' }}</span></div>
          <div class="ci-row"><span class="ci-k">指令前缀</span><code>{{ c.command_prefix }}</code></div>
          <div class="ci-row">
            <span class="ci-k">Token</span>
            <span v-if="tokenLen(c)" class="token-cell">
              <el-tag size="small" type="primary">{{ tokenLen(c) }} 位</el-tag>
              <code class="token-plain">{{ showToken[c.id] && revealed[c.id] ? revealed[c.id] : '•'.repeat(Math.min(16, tokenLen(c))) }}</code>
              <el-button v-if="canManage" size="small" text :icon="showToken[c.id] ? View : Hide" @click="revealToken(c)">{{ showToken[c.id] ? '隐藏' : '查看' }}</el-button>
            </span>
            <el-tag v-else size="small" type="info">未配置</el-tag>
          </div>
        </div>

        <div class="ch-actions">
          <el-tooltip content="测试连接" placement="top"><el-button size="small" text :icon="Connection" @click="testConn(c)" /></el-tooltip>
          <el-tooltip content="查看用户" placement="top"><el-button size="small" text :icon="UserFilled" @click="openUsers(c)" /></el-tooltip>
          <el-button v-if="c.kind === 'wxclaw' && canManage" size="small" type="primary" plain :icon="Postcard" @click="setScanChannel(c)">扫码</el-button>
          <el-button v-if="canManage" size="small" text :icon="Edit" @click="openModal(c)" />
          <el-popconfirm v-if="canManage" title="删除该渠道？" @confirm="remove(c)">
            <template #reference><el-button size="small" text type="danger" :icon="Delete" /></template>
          </el-popconfirm>
        </div>
        <el-alert v-if="c.last_error" type="error" :closable="false" style="margin-top: 8px" :title="c.last_error" />
      </div>
    </div>

    <!-- 新建 / 编辑 -->
    <el-dialog v-model="open" :title="edit ? '编辑渠道' : '接入渠道'" width="620px">
      <el-form :model="form" label-position="top">
        <el-form-item label="渠道类型" required>
          <el-select v-model="kind" :disabled="!!edit" style="width: 100%">
            <el-option v-for="(m, k) in KIND_META" :key="k" :value="k" :label="m.label" />
          </el-select>
        </el-form-item>
        <el-alert v-if="KIND_META[kind]" type="info" :closable="false" show-icon style="margin-bottom: 16px" :title="KIND_META[kind].hint" />
        <el-form-item label="名称" required><el-input v-model="form.name" placeholder="如：客服机器人群" /></el-form-item>

        <!-- 各渠道配置字段 -->
        <template v-for="f in CONFIG_FIELDS[kind] || []" :key="f.key">
          <el-form-item v-if="kind === 'wxclaw' && f.key === 'token'" :label="f.label">
            <div class="cfg-row">
              <el-input v-model="wxToken" :placeholder="f.ph" style="flex: 1" />
              <el-button :icon="Postcard" @click="formScan = true">扫码获取</el-button>
              <el-button v-if="canManage && edit" :icon="View" @click="revealEditToken">查看</el-button>
            </div>
          </el-form-item>
          <el-form-item v-else :label="f.label" :required="!!f.required && !edit">
            <el-input v-model="cfg[f.key]" :placeholder="f.ph" />
          </el-form-item>
        </template>

        <el-form-item label="回复模式" :extra="'智能问答 = 同站内「智能问答」链路（可调平台工具，管理员无需建智能体）'">
          <el-select v-model="form.reply_mode" clearable placeholder="不选 = 知识库问答（默认）" style="width: 100%">
            <el-option value="rag" label="知识库问答（可选知识库，不选则纯聊天）" />
            <el-option value="smart" label="智能问答（同站内问答：工具调用 / 文件生成 / 管理员可直接操作平台）" />
            <el-option value="agent" label="智能体（选定智能体后由其回答）" />
            <el-option value="workflow" label="工作流（选定工作流型智能体后执行其流程，可带审批/推送）" />
          </el-select>
        </el-form-item>
        <el-form-item label="服务模式" extra="只答=纯咨询；客服=识别转人工关键词并生成工单">
          <el-select v-model="form.service_mode" clearable placeholder="不选 = 只答（默认）" style="width: 100%">
            <el-option value="qa" label="只答（纯咨询，不转人工）" />
            <el-option value="support" label="客服（可转人工建工单）" />
          </el-select>
        </el-form-item>
        <el-form-item label="默认回答语言（多语言客服）" extra="客户没明说语言时用此语言；留空=自动判断">
          <el-select v-model="form.default_lang" clearable placeholder="自动判断" style="width: 100%">
            <el-option v-for="l in LANGS" :key="l.v" :value="l.v" :label="l.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="检索范围" extra="自动=按该渠道用户权限检索其全部有权知识库">
          <el-select v-model="form.kb_mode" clearable placeholder="不选 = 自动（默认）" style="width: 100%">
            <el-option value="auto" label="自动（全部有权知识库）" />
            <el-option value="custom" label="指定知识库" />
            <el-option value="off" label="不检索（纯聊天）" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.kb_mode === 'custom'" label="指定知识库" required>
          <el-select v-model="form.default_kb_ids" multiple style="width: 100%">
            <el-option v-for="k in kbs" :key="k.id" :value="k.id" :label="k.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="默认智能体（可空）" :extra="form.reply_mode === 'workflow' ? '工作流模式下请选择一个「工作流型」智能体' : undefined">
          <el-select v-model="form.default_agent_id" clearable placeholder="不选 = 不使用智能体" style="width: 100%">
            <el-option v-for="a in agents" :key="a.id" :value="a.id" :label="a.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="工作中提示" extra="工作流执行时把节点进度作为消息发给客户（私聊逐节点；群聊按下方策略）">
          <el-switch v-model="form.progress_hint" />
        </el-form-item>
        <el-form-item v-if="form.progress_hint" label="群聊进度策略" extra="群聊里逐条发会刷屏，默认不发">
          <el-select v-model="form.group_progress_mode" style="width: 200px">
            <el-option value="off" label="不发送（默认）" />
            <el-option value="first" label="只发第一条" />
            <el-option value="throttle" label="按间隔节流" />
          </el-select>
          <el-input-number
            v-if="form.group_progress_mode === 'throttle'"
            v-model="form.group_progress_interval"
            :min="1"
            :max="600"
            style="width: 130px; margin-left: 8px"
          />
          <span v-if="form.group_progress_mode === 'throttle'" class="text-muted" style="margin-left: 4px">秒</span>
        </el-form-item>
        <el-form-item label="指令前缀" extra="留空 = 默认 /">
          <el-input v-model="form.command_prefix" placeholder="/" style="width: 120px" />
        </el-form-item>
        <el-form-item label="欢迎语（用户首次对话时发送）"><el-input v-model="form.greeting" type="textarea" :rows="2" /></el-form-item>
        <el-divider content-position="left">附件入库</el-divider>
        <el-form-item label="外部用户发的文档是否入库" extra="默认关闭。选「仅已绑定账号」=只有绑定了内部账号的渠道用户可入库；「所有渠道用户」请谨慎开启">
          <el-select v-model="form.attach_ingest" clearable placeholder="不选 = 关闭（默认）" style="width: 100%">
            <el-option value="off" label="关闭（不入库）" />
            <el-option value="bound" label="仅已绑定内部账号的渠道用户" />
            <el-option value="all" label="所有渠道用户（谨慎）" />
          </el-select>
        </el-form-item>
        <template v-if="form.attach_ingest && form.attach_ingest !== 'off'">
          <el-form-item label="入库到知识库" extra="留空则用上面的「指定知识库」；仅取第一个">
            <el-select v-model="form.attach_kb_ids" multiple style="width: 100%">
              <el-option v-for="k in kbs" :key="k.id" :value="k.id" :label="k.name" />
            </el-select>
          </el-form-item>
          <el-form-item label="单文件大小上限(MB)">
            <el-input-number v-model="form.attach_max_mb" :min="1" :max="200" />
          </el-form-item>
          <el-form-item label="允许的扩展名" extra="逗号分隔，留空=默认（pdf/docx/xlsx/pptx/md/txt/csv/html）">
            <el-input v-model="form.attach_types" placeholder="pdf,docx,xlsx" />
          </el-form-item>
        </template>
        <el-form-item label="启用"><el-switch v-model="form.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="open = false">取消</el-button>
        <el-button type="primary" @click="submit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 渠道用户 -->
    <el-drawer v-model="usersOpen" :title="`渠道用户：${usersChannel?.name || ''}`" size="620px">
      <p class="text-secondary" style="font-size: 12px; line-height: 1.6">
        通过该渠道首次发消息的用户会自动建档（只读外部客户）。要提升为内部权限，请在「渠道身份绑定」页把该身份绑定到内部账号——绑定后即继承该账号全部权限。
      </p>
      <el-button size="small" :icon="Link" style="margin: 8px 0 12px" @click="$router.push('/admin/channel-users')">前往「渠道身份绑定」</el-button>
      <el-empty v-if="!users.length" description="暂无用户" :image-size="70" />
      <div v-for="u in users" :key="u.id" class="cu-row">
        <el-tag>{{ u.display_name || u.external_id }}</el-tag>
        <el-tag v-if="u.bound_user_id" size="small" type="success">{{ u.bound_user_name || `账号 #${u.bound_user_id}` }}（内部）</el-tag>
        <el-tag v-else size="small" type="info">外部客户（只读）</el-tag>
        <el-tag v-if="u.bind_code" size="small">绑定码 {{ u.bind_code }}</el-tag>
      </div>
    </el-drawer>

    <!-- 扫码登录 -->
    <WxclawScanLoginModal
      :channel-id="scanChannel?.id ?? null"
      :channel-name="scanChannel?.name"
      @close="scanChannel = null"
      @success="load"
    />
    <WxclawScanLoginModal
      :open="formScan"
      channel-name="新建微信渠道"
      @close="formScan = false"
      @token="onScanToken"
    />
  </PageContainer>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus, Edit, Delete, Connection, UserFilled, Postcard, View, Hide, Link } from '@element-plus/icons-vue'
import PageContainer from '@/components/PageContainer.vue'
import EmptyState from '@/components/EmptyState.vue'
import WxclawScanLoginModal from '@/components/WxclawScanLoginModal.vue'
import { agentApi, channelApi, kbApi, type Agent, type ChannelItem, type ChannelUserItem, type KB } from '@/api'
import { errMsg } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const canManage = computed(() => auth.hasPermission('channel:manage'))

const KIND_META: Record<string, { label: string; icon: string; tag: any; hint: string }> = {
  qqbot: { label: 'QQ 机器人', icon: 'ChatDotRound', tag: 'primary', hint: 'QQ 官方 Bot（WebSocket 长连接）' },
  wxclaw: { label: '微信（非官方）', icon: 'ChatRound', tag: 'success', hint: 'iLink 协议，HTTP 轮询；非官方，可能受上游变更影响' },
  wework: { label: '企业微信', icon: 'UserFilled', tag: 'info', hint: '企微智能机器人群/私聊（长连接）' },
  feishu: { label: '飞书', icon: 'Cloudy', tag: 'warning', hint: '飞书自建应用（lark-oapi 长连接，需安装 lark-oapi）' },
}
const CONFIG_FIELDS: Record<string, { key: string; label: string; ph?: string; required?: boolean }[]> = {
  qqbot: [{ key: 'app_id', label: 'AppID', required: true }, { key: 'client_secret', label: 'Client Secret', required: true }],
  wxclaw: [
    { key: 'token', label: 'Token', ph: '可点「扫码登录」自动获取，或手动填写' },
    { key: 'poll_interval_sec', label: '轮询间隔(秒)', ph: '默认 2' },
    { key: 'x_wechat_uin', label: 'X-WECHAT-UIN', ph: '可选' },
  ],
  wework: [{ key: 'bot_id', label: 'Bot ID', required: true }, { key: 'secret', label: 'Secret', required: true }, { key: 'ws_url', label: 'WS 地址', ph: '默认 wss://openws.work.weixin.qq.com' }],
  feishu: [{ key: 'app_id', label: 'App ID', required: true }, { key: 'app_secret', label: 'App Secret', required: true }],
}
const LANGS = [
  { v: 'zh', label: '中文' }, { v: 'en', label: 'English' }, { v: 'ja', label: '日本語' }, { v: 'ko', label: '한국어' },
  { v: 'es', label: 'Español' }, { v: 'fr', label: 'Français' }, { v: 'de', label: 'Deutsch' }, { v: 'ru', label: 'Русский' },
  { v: 'pt', label: 'Português' },
]
const meta = (k: string) => KIND_META[k] || { label: k, icon: 'Promotion', tag: 'info', hint: '' }
const REPLY_MODE_LABEL: Record<string, string> = { rag: '知识库问答', smart: '智能问答', agent: '智能体', workflow: '工作流' }
const replyModeLabel = (m?: string) => REPLY_MODE_LABEL[m || 'rag'] || (m || '知识库问答')

const channels = ref<ChannelItem[]>([])
const kbs = ref<KB[]>([])
const agents = ref<Agent[]>([])
const loading = ref(true)
const open = ref(false)
const edit = ref<ChannelItem | null>(null)
const kind = ref('qqbot')
const cfg = reactive<Record<string, any>>({})
const wxToken = ref('')
const form = reactive<any>({ name: '', enabled: true, reply_mode: 'rag', service_mode: 'qa', default_lang: null, kb_mode: 'auto', default_kb_ids: [], default_agent_id: null, progress_hint: false, group_progress_mode: 'off', group_progress_interval: 15, command_prefix: '/', greeting: '', attach_ingest: 'off', attach_kb_ids: [], attach_max_mb: 20, attach_types: '' })
const scanChannel = ref<ChannelItem | null>(null)
const formScan = ref(false)
const usersOpen = ref(false)
const usersChannel = ref<ChannelItem | null>(null)
const users = ref<ChannelUserItem[]>([])
const showToken = reactive<Record<number, boolean>>({})
const revealed = reactive<Record<number, string>>({})

function tokenLen(c: ChannelItem) {
  const m = c.secret_meta || {}
  for (const k of Object.keys(m)) if (k.toLowerCase().includes('token')) return Number(m[k]?.length || 0)
  return 0
}
async function revealToken(c: ChannelItem) {
  if (revealed[c.id]) { showToken[c.id] = !showToken[c.id]; return }
  try {
    const r = await channelApi.secrets(c.id)
    const s = (r.secrets || {}) as Record<string, string>
    const plain = String(s.token ?? s.bot_token ?? s.access_token ?? Object.values(s)[0] ?? '')
    revealed[c.id] = plain
    showToken[c.id] = true
  } catch (e) { ElMessage.error(errMsg(e)) }
}

async function load() {
  loading.value = true
  try {
    channels.value = await channelApi.list()
    agents.value = await agentApi.list()
    kbs.value = await kbApi.list()
  } catch (e) { ElMessage.error(errMsg(e)) } finally { loading.value = false }
}

function openModal(c?: ChannelItem) {
  edit.value = c || null
  Object.keys(cfg).forEach((k) => delete cfg[k])
  Object.assign(form, { name: '', enabled: true, reply_mode: 'rag', service_mode: 'qa', default_lang: null, kb_mode: 'auto', default_kb_ids: [], default_agent_id: null, progress_hint: false, group_progress_mode: 'off', group_progress_interval: 15, command_prefix: '/', greeting: '', attach_ingest: 'off', attach_kb_ids: [], attach_max_mb: 20, attach_types: '' })
  wxToken.value = ''
  if (c) {
    kind.value = c.kind
    Object.assign(form, {
      name: c.name, enabled: c.enabled, reply_mode: c.reply_mode || 'rag', service_mode: c.service_mode || 'qa',
      default_lang: c.default_lang ?? null, kb_mode: c.kb_mode || 'auto', default_kb_ids: c.default_kb_ids || [],
      default_agent_id: c.default_agent_id ?? null, progress_hint: !!c.progress_hint,
      group_progress_mode: c.group_progress_mode || 'off',
      group_progress_interval: c.group_progress_interval ?? 15,
      command_prefix: c.command_prefix || '/', greeting: c.greeting || '',
      attach_ingest: c.attach_ingest || 'off', attach_kb_ids: c.attach_kb_ids || [],
      attach_max_mb: c.attach_max_mb ?? 20, attach_types: c.attach_types || '',
    })
    for (const f of CONFIG_FIELDS[c.kind] || []) cfg[f.key] = (c.config || {})[f.key] ?? ''
    if (c.kind === 'wxclaw') wxToken.value = String((c.config || {}).token ?? '')
  } else {
    kind.value = 'qqbot'
  }
  open.value = true
}
function buildConfig() {
  const out: Record<string, any> = {}
  for (const f of CONFIG_FIELDS[kind.value] || []) {
    const v = cfg[f.key]
    if (v === undefined || v === null || v === '') continue
    out[f.key] = v
  }
  if (kind.value === 'wxclaw' && wxToken.value) out.token = wxToken.value
  return out
}
async function submit() {
  if (!form.name.trim()) { ElMessage.warning('请输入名称'); return }
  const payload: any = { ...form, kind: kind.value, config: buildConfig() }
  if (payload.kb_mode !== 'custom') payload.default_kb_ids = []
  try {
    if (edit.value) await channelApi.update(edit.value.id, payload)
    else await channelApi.create(payload)
    ElMessage.success('已保存')
    open.value = false
    load()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function remove(c: ChannelItem) {
  try { await channelApi.remove(c.id); ElMessage.success('已删除'); load() } catch (e) { ElMessage.error(errMsg(e)) }
}
async function testConn(c: ChannelItem) {
  try {
    const r = await channelApi.test(c.id)
    if (r.ok) ElMessage.success(`连接正常：${r.message}（${r.latency_ms}ms）`)
    else ElMessage.error(`连接失败：${r.message}`)
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function openUsers(c: ChannelItem) {
  usersChannel.value = c
  usersOpen.value = true
  try { users.value = await channelApi.users(c.id) } catch (e) { ElMessage.error(errMsg(e)) }
}
function setScanChannel(c: ChannelItem) { scanChannel.value = c }
async function revealEditToken() {
  if (!edit.value) return
  try {
    const r = await channelApi.secrets(edit.value.id)
    const s = (r.secrets || {}) as Record<string, string>
    const plain = String(s.token ?? s.bot_token ?? s.access_token ?? Object.values(s)[0] ?? '')
    if (!plain) { ElMessage.info('该渠道未保存 token'); return }
    wxToken.value = plain
    ElMessage.success('已读取 token 明文')
  } catch (e) { ElMessage.error(errMsg(e)) }
}
function onScanToken(token: string) {
  wxToken.value = token
  ElMessage.success('token 已填入，可继续填写其它项并保存')
}

onMounted(load)
</script>

<style scoped>
.ch-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
.ch-card { padding: 16px 18px; }
.ch-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.ch-title { display: inline-flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 600; color: var(--text-title); }
.ch-ico { color: var(--color-primary); }
.ch-badge { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--text-secondary); flex-shrink: 0; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #cbd5e1; }
.dot.on { background: #10b981; }
.dot.warn { background: #f59e0b; }
.ch-hint { font-size: 12px; color: var(--text-muted); margin: 8px 0 12px; min-height: 32px; line-height: 1.5; }
.ch-info { border-top: 1px solid var(--border-subtle); padding-top: 10px; }
.ci-row { display: flex; gap: 8px; font-size: 13px; color: var(--text-body); margin-bottom: 6px; align-items: center; }
.ci-k { color: var(--text-secondary); width: 66px; flex-shrink: 0; }
.ci-row code { background: var(--bg-muted); padding: 1px 6px; border-radius: 4px; font-size: 12px; }
.token-cell { display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.token-plain { font-family: monospace; font-size: 12px; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ch-actions { display: flex; align-items: center; gap: 2px; padding-top: 12px; border-top: 1px solid var(--border-subtle); margin-top: 12px; flex-wrap: wrap; }
.cfg-row { display: flex; gap: 8px; width: 100%; }
.cu-row { display: flex; align-items: center; gap: 8px; padding: 8px 4px; border-bottom: 1px solid var(--border-subtle); flex-wrap: wrap; }
</style>
