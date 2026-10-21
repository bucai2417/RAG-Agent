<template>
  <el-drawer
    :model-value="modelValue"
    title="运行工作流"
    size="600px"
    append-to-body
    @update:model-value="(v: any) => emit('update:modelValue', v)"
  >
    <el-tabs v-model="tab" @tab-change="onTab">
      <!-- 调试对话 -->
      <el-tab-pane label="调试对话" name="chat">
        <p class="rp-hint">
          像客户一样对话，实时看到工作流走到哪一步。调试不写入运行历史。
        </p>
        <div class="rp-chat-wrap">
          <WorkflowDebugChat
            :send="debugSend"
            :busy="debugBusy"
            :reset-key="debugResetKey"
          />
        </div>
      </el-tab-pane>

      <!-- 运行 -->
      <el-tab-pane label="运行" name="run">
        <div v-if="inputDefs.length" class="rp-form">
          <div v-for="d in inputDefs" :key="d.name" class="rp-field">
            <label class="rp-label">{{ d.name }}<span v-if="d.required" class="rp-req">*</span></label>
            <el-input-number v-if="d.type === 'number'" v-model="vals[d.name]" style="width: 100%" />
            <el-switch v-else-if="d.type === 'boolean'" v-model="vals[d.name]" />
            <el-input v-else-if="d.type === 'json'" v-model="vals[d.name]" type="textarea" :rows="3" class="rp-mono" />
            <el-input v-else v-model="vals[d.name]" :placeholder="d.default != null ? String(d.default) : ''" />
          </div>
        </div>
        <el-alert v-else type="info" show-icon :closable="false" title="该工作流未声明输入（start 节点无 inputs），可直接运行。" style="margin-bottom: 12px" />

        <el-button type="primary" :icon="VideoPlay" :loading="running" :disabled="!!pending" @click="run">运行</el-button>
        <el-button
          v-if="running && currentRunId"
          type="danger"
          plain
          :icon="VideoPause"
          :loading="cancelling"
          @click="cancelRun"
        >停止</el-button>
        <span v-if="timeoutHint" class="rp-timeout-hint">{{ timeoutHint }}</span>

        <el-alert
          v-if="pending"
          type="warning"
          show-icon
          :closable="false"
          style="margin: 12px 0"
          :title="pending.title || '需要人工审批'"
        >
          <div class="rp-pending-text">{{ pending.content }}</div>
          <div class="rp-pending-actions">
            <el-button size="small" type="primary" :icon="Check" @click="approve('approve')">通过</el-button>
            <el-button size="small" type="danger" :icon="Close" @click="approve('reject')">拒绝</el-button>
          </div>
        </el-alert>

        <div v-if="order.length" class="rp-logs">
          <el-collapse>
            <el-collapse-item v-for="nid in order" :key="nid">
              <template #title>
                <span class="rp-log-title">
                  <el-icon :class="'st-' + (logs[nid]?.status || 'pending')"><component :is="statusIcon(logs[nid]?.status)" /></el-icon>
                  <b>{{ nid }}</b>
                  <el-tag v-if="logs[nid]?.node_type" size="small" :color="metaColor(logs[nid].node_type)" effect="light">
                    {{ meta(logs[nid].node_type).icon }} {{ meta(logs[nid].node_type).label }}
                  </el-tag>
                  <span v-if="logs[nid]?.latency_ms != null" class="rp-lat">{{ logs[nid].latency_ms }}ms</span>
                  <el-tag v-if="logs[nid]?.error" size="small" type="danger">失败</el-tag>
                </span>
              </template>
              <div class="rp-log-body">
                <template v-if="logs[nid]?.input !== undefined">
                  <div class="rp-log-label">输入：</div>
                  <pre class="rp-pre">{{ pretty(logs[nid].input) }}</pre>
                </template>
                <template v-if="logs[nid]?.output !== undefined">
                  <div class="rp-log-label">输出：</div>
                  <pre class="rp-pre">{{ pretty(logs[nid].output) }}</pre>
                </template>
                <div v-if="logs[nid]?.error" class="rp-error">{{ logs[nid].error }}</div>
              </div>
            </el-collapse-item>
          </el-collapse>
        </div>
        <el-empty v-else description="尚无运行" :image-size="60" />

        <div v-if="result" class="rp-result">
          <el-alert
            :type="result.status === 'success' ? 'success' : result.status === 'waiting' ? 'warning' : 'error'"
            :closable="false"
            show-icon
            :title="resultTitle"
          />
          <div v-if="resultText" style="margin-top: 8px"><MarkdownBody :content="resultText" /></div>
          <AttachmentView
            v-for="f in resultFiles"
            :key="f.artifact_id || f.file_key"
            artifact
            :att="{ artifact_id: f.artifact_id, file_key: f.file_key, name: f.name, mime: f.mime, size: f.size }"
          />
        </div>
      </el-tab-pane>

      <!-- 历史 -->
      <el-tab-pane label="历史" name="history">
        <div class="rp-history-bar">
          <el-radio-group v-model="histFilter" size="small" @change="loadHistory">
            <el-radio-button value="">全部</el-radio-button>
            <el-radio-button value="failed">失败<span v-if="counts.failed" class="rp-count">{{ counts.failed }}</span></el-radio-button>
            <el-radio-button value="waiting">待审批<span v-if="counts.waiting" class="rp-count">{{ counts.waiting }}</span></el-radio-button>
            <el-radio-button value="success">成功<span v-if="counts.success" class="rp-count">{{ counts.success }}</span></el-radio-button>
          </el-radio-group>
        </div>
        <el-table :data="history" size="small" v-loading="histLoading" empty-text="暂无运行记录">
          <el-table-column label="时间" width="160">
            <template #default="{ row }">{{ row.created_at ? new Date(row.created_at).toLocaleString('zh-CN') : '-' }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.status === 'success' ? 'success' : row.status === 'waiting' ? 'warning' : 'danger'">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="80">
            <template #default="{ row }">{{ row.latency_ms ? `${row.latency_ms}ms` : '-' }}</template>
          </el-table-column>
          <el-table-column label="渠道回推" width="130">
            <template #default="{ row }">
              <!-- channel_reply 由后端在审批结束/回推后写入 run.output -->
              <template v-if="row.channel_reply">
                <el-tag size="small" :type="row.channel_reply.sent ? 'success' : 'danger'">
                  {{ row.channel_reply.sent ? '已送达' : '未送达' }}
                </el-tag>
                <el-button
                  v-if="!row.channel_reply.sent"
                  size="small"
                  link
                  :loading="resending === row.id"
                  @click="resend(row.id)"
                >重发</el-button>
              </template>
              <span v-else class="rf-muted">-</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="80">
            <template #default="{ row }"><el-button size="small" link @click="openHistoryRun(row.id)">查看</el-button></template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  VideoPlay, VideoPause, Check, Close, Loading, CircleCheck, CircleClose, Remove, Minus,
} from '@element-plus/icons-vue'
import MarkdownBody from '@/components/MarkdownBody.vue'
import AttachmentView from '@/components/AttachmentView.vue'
import WorkflowDebugChat from './WorkflowDebugChat.vue'
import { workflowApi, streamWorkflowRun, API_BASE } from '@/api'
import { errMsg } from '@/api/http'
import { NODE_META, type NodeMeta } from './meta'

interface NodeLog {
  node_id: string
  node_type?: string
  status: string
  latency_ms?: number
  input?: any
  output?: any
  error?: string
}
interface InputDef { name: string; type?: string; required?: boolean; default?: any }

const props = defineProps<{ agentId: number; graph: any; modelValue: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  nodeStatus: [Record<string, string>]
  /** 执行路径给画布做高亮：{ takenEdges, latency } */
  trace: [{ takenEdges: string[]; latency: Record<string, number> }]
  finish: []
}>()

const tab = ref('run')
const running = ref(false)
const logs = reactive<Record<string, NodeLog>>({})
const order = ref<string[]>([])
const statusMap = ref<Record<string, string>>({})
const result = ref<any>(null)
const pending = ref<{ run_id: number; node_id: string; title?: string; content?: string } | null>(null)
const history = ref<any[]>([])
const histLoading = ref(false)
const histFilter = ref('')
const counts = ref<Record<string, number>>({})
const resending = ref<number | null>(null)
const cancelling = ref(false)
/** 当前运行的 run_id（从 meta 事件拿到，用于「停止」） */
const currentRunId = ref<number | null>(null)
const timeoutHint = ref('')
/** 执行路径高亮：已走过的边 + 节点耗时（供画布实时演示） */
const takenEdges = ref<string[]>([])
const latencyMap = ref<Record<string, number>>({})
const vals = reactive<Record<string, any>>({})

// ---- 调试对话 ----
const debugBusy = ref(false)
const debugResetKey = ref(0)
const debugTurns = ref<{ role: string; content: string }[]>([])

async function debugSend(text: string): Promise<{ text: string; meta?: string }> {
  debugBusy.value = true
  // 先把本轮用户消息纳入历史（服务端只收此前的对话）
  const history = [...debugTurns.value]
  debugTurns.value = [...debugTurns.value, { role: 'user', content: text }]
  reset()
  try {
    const r = await workflowApi.debug(props.agentId, text, history)
    // 用 trace 驱动画布上的路径高亮
    applyTrace(r.trace || [])
    debugTurns.value = [...debugTurns.value, { role: 'assistant', content: r.text || '（无回复）' }]
    const metaParts: string[] = []
    if (r.latency_ms != null) metaParts.push(`${r.latency_ms}ms`)
    if (r.trace?.length) metaParts.push(`${r.trace.length} 个节点`)
    if (r.paused) metaParts.push('停在审批')
    return { text: r.text || '（无回复）', meta: metaParts.join(' · ') }
  } finally {
    debugBusy.value = false
  }
}

/** 把调试返回的执行轨迹映射成画布状态 + 已走路径 */
function applyTrace(trace: any[]) {
  const sm: Record<string, string> = {}
  const lat: Record<string, number> = {}
  for (const t of trace) {
    if (!t.node_id) continue
    sm[t.node_id] = t.status || 'success'
    if (t.latency_ms) lat[t.node_id] = t.latency_ms
  }
  statusMap.value = { ...sm }
  latencyMap.value = { ...lat }
  takenEdges.value = reconstructPath(props.graph, sm)
  emit('nodeStatus', { ...sm })
}

/**
 * 从已成功节点集合反推走过的边。
 * 条件/多路/路由节点只走命中的分支，这里按「目标节点也成功」来判断。
 */
function reconstructPath(graph: any, ok: Record<string, string>): string[] {
  const out: string[] = []
  for (const e of graph?.edges || []) {
    const id = e.id || `e-${e.source}-${e.target}`
    if (ok[e.source] === 'success' && ok[e.target] === 'success') out.push(id)
  }
  return out
}

const inputDefs = computed<InputDef[]>(() => {
  const start = (props.graph?.nodes || []).find((n: any) => n.type === 'start')
  return (start?.data?.inputs || []) as InputDef[]
})

const resultText = computed(() => {
  const out = result.value?.output
  if (out && typeof out === 'object') return String(out.output ?? out.text ?? JSON.stringify(out, null, 2))
  return String(out ?? result.value?.error ?? '')
})
const resultFiles = computed<any[]>(() => {
  const out = result.value?.output
  return out && typeof out === 'object' && (out.file_key || out.artifact_id) ? [out] : []
})
const resultTitle = computed(() => {
  const s = result.value?.status
  if (s === 'success') return '执行成功'
  if (s === 'waiting') return '等待审批'
  if (s === 'canceled') return '已取消'
  return '执行失败'
})

function meta(type?: string): NodeMeta {
  return NODE_META[type || ''] || { label: type || '节点', color: '#999', icon: '' }
}
function metaColor(type?: string) { return meta(type).color }
function pretty(v: any) { return typeof v === 'string' ? v : JSON.stringify(v, null, 2) }
function statusIcon(s?: string) {
  if (s === 'running') return Loading
  if (s === 'success') return CircleCheck
  if (s === 'failed') return CircleClose
  if (s === 'skipped') return Minus
  return Remove
}

function emitTrace() {
  emit('trace', { takenEdges: [...takenEdges.value], latency: { ...latencyMap.value } })
}

function reset() {
  for (const k of Object.keys(logs)) delete logs[k]
  order.value = []
  statusMap.value = {}
  result.value = null
  pending.value = null
  takenEdges.value = []
  latencyMap.value = {}
  currentRunId.value = null
  timeoutHint.value = ''
  emit('nodeStatus', {})
  emitTrace()
}

function upsert(nid: string, patch: Partial<NodeLog>) {
  logs[nid] = { ...(logs[nid] || { node_id: nid, status: 'running' }), ...patch }
  if (!order.value.includes(nid)) order.value = [...order.value, nid]
  if (patch.status) {
    statusMap.value = { ...statusMap.value, [nid]: patch.status }
    emit('nodeStatus', { ...statusMap.value })
  }
}

function handleEvent(event: string, d: any) {
  if (event === 'meta') {
    // 运行开始时后端推送 run_id，供「停止」按钮使用
    if (d?.run_id) currentRunId.value = d.run_id
  } else if (event === 'node_started') upsert(d.node_id, { node_type: d.node_type, status: 'running' })
  else if (event === 'node_finished') {
    upsert(d.node_id, { node_type: d.node_type, status: d.status, latency_ms: d.latency_ms, input: d.input, output: d.output, error: d.error })
    if (d.latency_ms != null) latencyMap.value = { ...latencyMap.value, [d.node_id]: d.latency_ms }
    // 实时刷新已走路径
    takenEdges.value = reconstructPath(props.graph, { ...statusMap.value })
    emitTrace()
  } else if (event === 'approval_required') {
    pending.value = { run_id: d.run_id, node_id: d.node_id, title: d.title, content: d.content }
    running.value = false
  } else if (event === 'run_timeout') {
    timeoutHint.value = `已超时（${d.timeout_seconds}s），已自动中止`
  } else if (event === 'run_finished') {
    result.value = d
    running.value = false
    takenEdges.value = reconstructPath(props.graph, { ...statusMap.value })
    emitTrace()
    if (d.status !== 'waiting') emit('finish')
  }
}

async function consume(url: string, body: any) {
  const token = localStorage.getItem('access_token')
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
  })
  if (!resp.body) throw new Error('无响应流')
  const reader = resp.body.getReader()
  const dec = new TextDecoder()
  let buf = '', event = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    const lines = buf.split('\n'); buf = lines.pop() || ''
    for (const line of lines) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) {
        const raw = line.slice(5).trim(); if (!raw) continue
        let d: any; try { d = JSON.parse(raw) } catch { continue }
        handleEvent(event, d)
      }
    }
  }
}

async function run() {
  // 收集 start 输入（按类型转换）
  const inputs: Record<string, any> = {}
  for (const def of inputDefs.value) {
    let v = vals[def.name]
    if (def.required && (v === undefined || v === '')) { ElMessage.warning(`请输入 ${def.name}`); return }
    if (def.type === 'json' && typeof v === 'string') { try { v = JSON.parse(v) } catch { /* keep */ } }
    inputs[def.name] = v
  }
  reset()
  running.value = true
  try {
    await workflowApi.save(props.agentId, props.graph).catch(() => {})
    await streamWorkflowRun(props.agentId, inputs, {
      onNodeStarted: (d) => handleEvent('node_started', d),
      onNodeFinished: (d) => handleEvent('node_finished', d),
      onApprovalRequired: (d) => handleEvent('approval_required', d),
      onFinished: (d) => handleEvent('run_finished', d),
    })
  } catch (e) {
    ElMessage.error(errMsg(e))
    running.value = false
  }
}

async function approve(decision: 'approve' | 'reject') {
  if (!pending.value) return
  running.value = true
  try {
    await consume(workflowApi.approveUrl(pending.value.run_id), { decision })
    pending.value = null
  } catch (e) {
    ElMessage.error(errMsg(e))
    running.value = false
  }
}

async function loadHistory() {
  histLoading.value = true
  try {
    const r = await workflowApi.listRuns(props.agentId, 30, histFilter.value || undefined)
    history.value = r.runs || []
    counts.value = r.status_counts || {}
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    histLoading.value = false
  }
}

/** 停止正在执行的工作流 */
async function cancelRun() {
  if (!currentRunId.value) return
  cancelling.value = true
  try {
    const r = await workflowApi.cancelRun(currentRunId.value)
    ElMessage[r.ok ? 'success' : 'warning'](r.message || (r.ok ? '已请求取消' : '取消失败'))
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    cancelling.value = false
  }
}

/** 补发工作流结果到原渠道客户（上次未送达时） */
async function resend(runId: number) {
  resending.value = runId
  try {
    const r = await workflowApi.resend(runId)
    if (r.ok) ElMessage.success(r.message || '已发送')
    else ElMessage.warning(r.message || '发送失败')
    await loadHistory()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    resending.value = null
  }
}

async function openHistoryRun(runId: number) {
  try {
    const r = await workflowApi.getRun(runId)
    reset()
    for (const n of r.nodes || []) {
      logs[n.node_id] = { node_id: n.node_id, node_type: n.node_type, status: n.status, input: n.input, output: n.output, error: n.error_msg }
      if (!order.value.includes(n.node_id)) order.value = [...order.value, n.node_id]
      if (n.status) statusMap.value = { ...statusMap.value, [n.node_id]: n.status }
    }
    emit('nodeStatus', { ...statusMap.value })
    result.value = { status: r.run?.status, output: r.run?.output, error: r.run?.error_msg }
    tab.value = 'run'
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

function onTab(name: any) {
  if (name === 'history') loadHistory()
}

// 打开时按需加载历史 / 预填默认值
watch(
  () => props.modelValue,
  (v) => { if (v && inputDefs.value.length) for (const d of inputDefs.value) if (vals[d.name] === undefined) vals[d.name] = d.default },
)
onMounted(() => {
  if (inputDefs.value.length) for (const d of inputDefs.value) if (vals[d.name] === undefined) vals[d.name] = d.default
})

// 引用 API_BASE 以保留与其他页一致的基址写法（approveUrl 已含 API_BASE）
void API_BASE
</script>

<style scoped>
.rp-history-bar { margin-bottom: 10px; }
.rp-count { margin-left: 4px; font-size: 11px; opacity: 0.8; }
.rp-timeout-hint { margin-left: 10px; font-size: 12px; color: #d46b08; }
.rp-form { margin-bottom: 14px; }
.rp-field { margin-bottom: 10px; }
.rp-label { display: block; font-size: 13px; color: var(--text-body); margin-bottom: 4px; }
.rp-req { color: #ef4444; margin-left: 3px; }
.rp-mono :deep(textarea) { font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px; }
.rp-pending-text { white-space: pre-wrap; max-height: 160px; overflow: auto; font-size: 13px; margin: 6px 0; }
.rp-pending-actions { display: flex; gap: 8px; margin-top: 8px; }
.rp-logs { margin: 14px 0; }
.rp-log-title { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; }
.rp-log-title .el-icon { color: var(--text-secondary); }
.rp-log-title .st-running { color: #1677ff; }
.rp-log-title .st-success { color: #52c41a; }
.rp-log-title .st-failed { color: #ff4d4f; }
.rp-lat { font-size: 12px; color: var(--text-muted); }
.rp-log-body { font-size: 12px; }
.rp-log-label { color: var(--text-secondary); margin: 4px 0 2px; }
.rp-pre { margin: 0; white-space: pre-wrap; word-break: break-word; background: var(--bg-subtle); padding: 8px 10px; border-radius: 6px; max-height: 220px; overflow: auto; font-family: 'SFMono-Regular', Consolas, monospace; }
.rp-error { color: #ef4444; margin-top: 6px; }
.rp-result { margin-top: 12px; }
</style>
