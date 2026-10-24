<template>
  <div
    class="agent-chat"
    @dragenter="onDragEnter"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <div v-if="dragging" class="drop-mask">
      <el-icon :size="36"><UploadFilled /></el-icon>
      <span>松开即可添加附件</span>
    </div>

    <div class="chat-shell" :class="{ 'with-browser': browserPanel.open.value }">
      <!-- ================= 历史会话侧栏 ================= -->
      <aside class="conv-side app-card">
        <div class="cs-head">
          <span class="cs-title">历史对话</span>
          <el-button size="small" type="primary" :icon="Plus" @click="newConv">新对话</el-button>
        </div>
        <el-scrollbar class="cs-scroll">
          <div
            v-for="c in convs"
            :key="c.id"
            class="cs-item"
            :class="{ active: c.id === convId }"
            @click="openConversation(c.id)"
          >
            <el-icon class="cs-icon"><ChatLineRound /></el-icon>
            <span class="cs-name">{{ c.title || '未命名' }}</span>
            <el-dropdown trigger="click" @command="(cmd: string) => onConvCmd(cmd, c)" @click.stop>
              <el-icon class="cs-more" @click.stop><MoreFilled /></el-icon>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="rename"><el-icon><Edit /></el-icon>重命名</el-dropdown-item>
                  <el-dropdown-item command="delete" divided><el-icon><Delete /></el-icon>删除对话</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
          <el-empty v-if="!convs.length" description="暂无历史" :image-size="60" />
        </el-scrollbar>
      </aside>

      <!-- ================= 主对话区 ================= -->
      <section class="chat-main app-card">
        <div class="control-bar">
          <el-button size="small" :icon="ArrowLeft" @click="$router.push('/agents')">返回</el-button>
          <div class="cb-agent">
            <span class="cb-name">{{ agent?.name || '智能体' }}</span>
            <span v-if="agent?.description" class="cb-desc">{{ agent.description }}</span>
          </div>
          <div v-if="modes.length" class="cb-group">
            <span class="cb-label">模式</span>
            <el-select v-model="modeId" size="small" clearable placeholder="默认模式" style="width: 160px">
              <el-option v-for="m in modes" :key="m.id" :value="m.id" :label="m.name" />
            </el-select>
          </div>
          <div v-if="models.length" class="cb-group">
            <span class="cb-label">模型</span>
            <el-select v-model="modelId" size="small" clearable placeholder="默认模型" style="width: 180px">
              <el-option v-for="m in models" :key="m.id" :value="m.id" :label="m.display_name || m.model_name" />
            </el-select>
          </div>
          <div class="cb-right">
            <el-tooltip :content="browserPanel.open.value ? '收起浏览器画面' : '查看浏览器画面'" placement="top">
              <el-button
                size="small" text :icon="Monitor"
                :class="{ 'cb-on': browserPanel.open.value }"
                @click="browserPanel.toggle()"
              />
            </el-tooltip>
            <el-tooltip v-if="msgs.length >= 4" content="对话目录" placement="top">
              <el-button size="small" text :icon="List" @click="outlineOpen = true" />
            </el-tooltip>
            <el-button v-if="canEdit" size="small" text :icon="Edit" @click="$router.push(`/agents/${agentId}/edit`)">编辑</el-button>
            <el-button size="small" text :icon="Plus" @click="newConv">新对话</el-button>
          </div>
        </div>

        <div class="msg-area">
          <el-scrollbar ref="listRef" class="msg-scroll" @scroll="onScroll">
            <div v-if="!msgs.length" class="chat-empty">
              <div class="ce-logo">AI</div>
              <h3 class="ce-title">{{ agent?.name || '智能体' }}</h3>
              <p class="ce-sub">{{ agent?.description || '向智能体提问，它会自动调用工具与知识库获取答案。' }}</p>
            </div>

            <div v-for="(m, i) in msgs" :id="`msg-${i}`" :key="i" class="msg" :class="[m.role, { hl: highlightIdx === i }]">
              <div class="msg-avatar">
                <span v-if="m.role === 'user'" class="ava-user">我</span>
                <span v-else class="ava-ai">AI</span>
              </div>
              <div class="msg-card">
                <!-- 思考过程 -->
                <ReasoningBlock v-if="m.role === 'assistant'" :reasoning="m.reasoning" :streaming="m.streaming" />
                <!-- 工具调用步骤 -->
                <ToolSteps v-if="m.role === 'assistant' && (m.steps || []).length" :steps="(m.steps as any)" />
                <!-- 正文 -->
                <MarkdownBody v-if="m.role === 'assistant' && !m.streaming" :content="m.content" />
                <div v-else class="plain-text">{{ m.content || (m.streaming ? '…' : '') }}</div>

                <!-- 用户附件 -->
                <AttachmentView v-for="a in m.attachments || []" :key="a.file_key" :att="a" />

                <!-- HITL 待确认 -->
                <div v-if="m.pending" class="msg-pending">
                  <PendingActionCard :action="m.pending" :resolved="m.pendingResolved" @confirm="(aid, d) => confirmAction(i, aid, d)" />
                </div>

                <!-- 引用来源：区分「答案里标注过」与「只是检索到」 -->
                <CitationList
                  :citations="m.citations || []"
                  :used-ids="m.usedCitationIds"
                  @open="openCitation"
                />

                <!-- 操作条 -->
                <div v-if="m.role === 'assistant' && !m.streaming && m.content" class="msg-actions">
                  <el-tooltip content="复制" placement="top">
                    <el-button size="small" text :icon="DocumentCopy" @click="copy(m.content)" />
                  </el-tooltip>
                  <el-tooltip content="有帮助" placement="top">
                    <el-button size="small" text :icon="Select" :style="m.feedback === 1 ? 'color:#10b981' : ''" @click="doFeedback(i, 1)" />
                  </el-tooltip>
                  <el-tooltip content="没帮助" placement="top">
                    <el-button size="small" text :icon="CloseBold" :style="m.feedback === -1 ? 'color:#ef4444' : ''" @click="doFeedback(i, -1)" />
                  </el-tooltip>
                </div>
              </div>
            </div>
          </el-scrollbar>

          <button v-if="showJump" class="jump-latest" @click="jumpToLatest">
            <el-icon><ArrowDown /></el-icon>回到最新
          </button>
        </div>

        <!-- 待发送附件 -->
        <div v-if="pendingAtts.length" class="pending-atts">
          <el-tag v-for="(a, i) in pendingAtts" :key="a.file_key" closable @close="pendingAtts.splice(i, 1)">{{ a.name }}</el-tag>
        </div>

        <!-- 输入区：圆角卡片 + 内嵌底部工具栏（与「智能问答」同一套组件） -->
        <div class="composer-wrap">
          <ChatComposer
            v-model="input"
            :streaming="streaming"
            :can-send="canSend"
            placeholder="输入问题，Enter 发送，Shift+Enter 换行"
            @send="send"
            @stop="stop"
          >
            <template #left>
              <el-tooltip content="上传图片/文档/视频，也可直接把文件拖进来" placement="top">
                <button type="button" class="cp-btn" @click="pickFile">
                  <el-icon><Paperclip /></el-icon>
                </button>
              </el-tooltip>
              <input ref="fileRef" type="file" hidden multiple
                accept=".pdf,.docx,.doc,.xlsx,.xls,.pptx,.csv,.txt,.md,image/*,video/*" @change="onPick" />
            </template>
            <template #context>
              <ContextRing
                :stats="ctxStats"
                :has-conv="!!convId"
                :compressing="!!ctxStats?.compressing"
                @refresh="refreshContext"
              />
            </template>
          </ChatComposer>
        </div>
      </section>

      <!-- 右侧：浏览器实时画面。只在展开时挂载（挂载即连 WS 并推帧） -->
      <BrowserLivePanel
        v-if="browserPanel.open.value"
        :open="browserPanel.open.value"
        :follow-url="browserPanel.followUrl.value"
        :follow-seq="browserPanel.followSeq.value"
        class="browser-col"
        @close="browserPanel.close()"
      />
    </div>

    <!-- 引用原文预览 -->
    <el-drawer v-model="docPreviewOpen" :title="docPreview?.title || '文档预览'" size="640px" append-to-body>
      <el-tag v-if="docPreview?.page != null" type="primary" style="margin-bottom: 8px">第 {{ docPreview.page }} 页</el-tag>
      <template v-if="docPreview?.snippet">
        <div class="dp-label">引用的原文片段：</div>
        <div class="dp-snippet">{{ docPreview.snippet }}</div>
        <el-divider />
      </template>
      <div class="dp-full">{{ docPreview?.content || '(无内容)' }}</div>
    </el-drawer>

    <!-- 对话目录 -->
    <el-drawer v-model="outlineOpen" title="对话目录" size="360px" append-to-body>
      <div v-for="(m, i) in msgs" :key="i" class="outline-item" @click="jumpToMessage(i)">
        <el-tag size="small" :type="m.role === 'user' ? 'primary' : 'success'">{{ m.role === 'user' ? '我' : 'AI' }}</el-tag>
        <span class="outline-text">{{ (m.content || '').slice(0, 40) || '（空）' }}</span>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Plus, MoreFilled, Edit, Delete, List, Paperclip,
  DocumentCopy, Select, CloseBold, ArrowDown, ArrowLeft, ChatLineRound, UploadFilled,
  Monitor,
} from '@element-plus/icons-vue'
import MarkdownBody from '@/components/MarkdownBody.vue'
import CitationList from '@/components/CitationList.vue'
import BrowserLivePanel from '@/components/BrowserLivePanel.vue'
import AttachmentView from '@/components/AttachmentView.vue'
import ReasoningBlock from '@/components/ReasoningBlock.vue'
import ToolSteps from '@/components/ToolSteps.vue'
import PendingActionCard from '@/components/PendingActionCard.vue'
import ChatComposer from '@/components/ChatComposer.vue'
import ContextRing from '@/components/ContextRing.vue'
import {
  agentApi, chatApi, confirmToolAction, docApi, providerApi, streamChat, API_BASE,
  type Agent, type AgentMode, type Attachment, type Citation, type ContextStats, type Conversation,
  type ModelConfig, type PendingActionEvt, type StreamHandlers,
} from '@/api'
import { errMsg } from '@/api/http'
import { usedCiteIds } from '@/utils/citation'
import { useAuthStore } from '@/stores/auth'
import { useBrowserPanel, isBrowserTool } from '@/composables/useBrowserPanel'

interface Msg {
  id?: number
  role: 'user' | 'assistant'
  content: string
  reasoning?: string
  citations?: Citation[]
  /** 答案里真正标注过的引用编号（1 基）；空 = 检索到但没用上 */
  usedCitationIds?: number[]
  attachments?: Attachment[]
  feedback?: number | null
  streaming?: boolean
  steps?: { call: any; result?: any }[]
  pending?: PendingActionEvt
  pendingResolved?: boolean
}

const route = useRoute()
const auth = useAuthStore()
const browserPanel = useBrowserPanel()
const agentId = Number(route.params.id)
const canEdit = computed(() => auth.hasPermission('agent:edit'))

const agent = ref<Agent | null>(null)
const modes = ref<AgentMode[]>([])
const modeId = ref<number>()
const models = ref<ModelConfig[]>([])
const modelId = ref<number>()
const input = ref('')
const msgs = ref<Msg[]>([])
const convId = ref<number>()
const convs = ref<Conversation[]>([])
const ctxStats = ref<ContextStats | null>(null)
let ctxTimer: ReturnType<typeof setTimeout> | null = null
const pendingAtts = ref<Attachment[]>([])
const streaming = ref(false)
/** 有正文或只有附件都可以发；纯空白且无附件时禁用发送键 */
const canSend = computed(() => !!input.value.trim() || pendingAtts.value.length > 0)
const fileRef = ref<HTMLInputElement>()
const listRef = ref<any>(null)
const dragging = ref(false)
const showJump = ref(false)
const highlightIdx = ref<number | null>(null)
const outlineOpen = ref(false)
const ctrl = ref<AbortController | null>(null)

const docPreviewOpen = ref(false)
const docPreview = ref<{ title: string; content: string; page?: number | null; snippet?: string | null } | null>(null)

async function loadConvs() {
  try {
    const all = await chatApi.conversations()
    convs.value = all.filter((c) => (c as any).settings?.agent_id === agentId)
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

/**
 * 拉取上下文占用（输入框圆环）。压缩是后台异步任务，
 * 高水位时补拉一次，让环口能自己回落。
 */
async function refreshContext(afterMs = 0) {
  const cid = convId.value
  if (!cid) {
    ctxStats.value = null
    return
  }
  if (ctxTimer) {
    clearTimeout(ctxTimer)
    ctxTimer = null
  }
  try {
    const s = await chatApi.context(cid)
    if (convId.value === cid) ctxStats.value = s
  } catch {
    /* ignore */
  }
  if (afterMs) {
    ctxTimer = setTimeout(() => {
      ctxTimer = null
      if (convId.value === cid) refreshContext()
    }, afterMs)
  }
}

async function load() {
  try {
    agent.value = await agentApi.get(agentId)
    modes.value = await agentApi.modes(agentId)
    if (agent.value.default_mode_id) modeId.value = agent.value.default_mode_id
    const cs = await providerApi.configs()
    models.value = cs.filter((c) => c.purpose === 'chat')
    await loadConvs()
    newConv()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

function newConv() {
  convId.value = undefined
  const greeting = (agent.value?.config as any)?.greeting
  msgs.value = greeting ? [{ role: 'assistant', content: greeting }] : []
  pendingAtts.value = []
  ctxStats.value = null
  if (ctxTimer) {
    clearTimeout(ctxTimer)
    ctxTimer = null
  }
}

async function openConversation(cid: number) {
  try {
    convId.value = cid
    const ms = await chatApi.messages(cid)
    msgs.value = ms
      .filter((m) => m.role === 'user' || m.role === 'assistant')
      .map((m) => ({
        id: m.id,
        role: m.role as 'user' | 'assistant',
        content: m.content,
        reasoning: m.reasoning,
        citations: m.citations,
        // 历史消息不落 usedCitationIds，按正文现算（口径与后端一致）
        usedCitationIds: usedCiteIds(m.content, m.citations),
        attachments: m.attachments,
        feedback: m.feedback,
        steps: (m as any).tool_calls,
      }))
    scrollToBottom()
    refreshContext()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

function onConvCmd(cmd: string, c: Conversation) {
  if (cmd === 'rename') renameConv(c)
  else if (cmd === 'delete') confirmDel(c)
}

function renameConv(c: Conversation) {
  ElMessageBox.prompt('请输入新的对话名称', '重命名对话', {
    inputValue: c.title || '',
    inputValidator: (v) => (v && v.trim() ? true : '名称不能为空'),
  })
    .then(async ({ value }) => {
      await chatApi.rename(c.id, value.trim())
      loadConvs()
    })
    .catch(() => {})
}

function confirmDel(c: Conversation) {
  ElMessageBox.confirm('对话记录与消息将一并删除，不可恢复。', '删除该对话？', {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消',
  })
    .then(async () => {
      await chatApi.remove(c.id)
      loadConvs()
      if (convId.value === c.id) newConv()
    })
    .catch(() => {})
}

// ---- 上传 ----
function pickFile() {
  fileRef.value?.click()
}
async function onPick(e: Event) {
  const files = (e.target as HTMLInputElement).files
  if (files) await uploadAll(Array.from(files))
  if (fileRef.value) fileRef.value.value = ''
}
async function uploadAll(files: File[]) {
  let ok = 0
  for (const f of files) {
    try {
      const kind = f.type.startsWith('image/') ? 'image' : f.type.startsWith('video/') || f.type.startsWith('audio/') ? 'video' : 'document'
      const att = await chatApi.uploadAttachment(f, { kind })
      pendingAtts.value.push(att)
      ok += 1
    } catch (e) {
      ElMessage.error(`${f.name}：${errMsg(e)}`)
    }
  }
  if (ok) ElMessage.success(`已添加 ${ok} 个附件`)
}

// ---- 拖拽 ----
let dragDepth = 0
function hasFiles(e: DragEvent) {
  const dt = e.dataTransfer
  return !!dt && (dt.types ? Array.from(dt.types).includes('Files') : (dt.files?.length || 0) > 0)
}
function onDragEnter(e: DragEvent) {
  if (!hasFiles(e)) return
  e.preventDefault()
  dragDepth++
  dragging.value = true
}
function onDragOver(e: DragEvent) {
  if (!hasFiles(e)) return
  e.preventDefault()
  e.dataTransfer!.dropEffect = 'copy'
}
function onDragLeave(e: DragEvent) {
  if (!hasFiles(e)) return
  dragDepth = Math.max(0, dragDepth - 1)
  if (dragDepth === 0) dragging.value = false
}
async function onDrop(e: DragEvent) {
  e.preventDefault()
  dragDepth = 0
  dragging.value = false
  const files = Array.from(e.dataTransfer?.files || [])
  if (files.length) await uploadAll(files)
}

// ---- 发送 / 流式 ----
function patchLast(fn: (m: Msg) => Msg) {
  const arr = msgs.value.slice()
  if (!arr.length) return
  arr[arr.length - 1] = fn(arr[arr.length - 1])
  msgs.value = arr
}
function patchAt(idx: number, fn: (m: Msg) => Msg) {
  const arr = msgs.value.slice()
  arr[idx] = fn(arr[idx])
  msgs.value = arr
}

async function send() {
  const text = input.value.trim()
  if ((!text && !pendingAtts.value.length) || streaming.value) return
  input.value = ''
  const atts = pendingAtts.value
  pendingAtts.value = []
  msgs.value = [...msgs.value, { role: 'user', content: text, attachments: atts }, { role: 'assistant', content: '', streaming: true }]
  streaming.value = true
  ctrl.value = new AbortController()

  try {
    await streamChat(
      {
        conversation_id: convId.value,
        kb_ids: agent.value?.kb_ids || [],
        message: text || '（见附件）',
        mode_id: modeId.value,
        model_config_id: modelId.value,
        attachments: atts,
      } as any,
      {
        onMeta: (d: any) => { if (d.conversation_id) convId.value = d.conversation_id },
        onDelta: (t) => patchLast((m) => ({ ...m, content: m.content + t })),
        onReasoning: (t) => patchLast((m) => ({ ...m, reasoning: (m.reasoning || '') + t })),
        onCitations: (c, used) =>
          patchLast((m) => ({ ...m, citations: c, usedCitationIds: used })),
        onToolCall: (evt) => {
          // AI 一动浏览器就把实时画面顶出来，用户才知道它在哪个页面
          browserPanel.autoShowForTool(evt?.name)
          patchLast((m) => ({ ...m, steps: [...(m.steps || []), { call: evt }] }))
        },
        onToolResult: (evt) => {
          // 结果里带着操作完所在页面的地址：面板据此切到那个标签，
          // 否则 AI 在第二个标签里干活时面板还停在第一个页面
          if (isBrowserTool(evt?.name)) browserPanel.follow(evt?.data?.url)
          return patchLast((m) => {
            const steps = [...(m.steps || [])]
            const i = steps.findIndex((s) => s.call.id === evt.id && !s.result)
            if (i >= 0) steps[i] = { ...steps[i], result: evt }
            else steps.push({ call: { id: evt.id, name: evt.name, arguments: '' }, result: evt })
            return { ...m, steps }
          })
        },
        onPendingAction: (evt) => patchLast((m) => ({ ...m, pending: evt, streaming: false })),
        onContext: (s) => {
          ctxStats.value = s
          if (s.percent >= 70 || s.compressing) refreshContext(2500)
        },
        onDone: (mid) => patchLast((m) => ({ ...m, id: mid, streaming: false })),
        onError: (msg) => ElMessage.error(msg),
      },
      ctrl.value.signal,
      `${API_BASE}/agents/${agentId}/run`,
    )
  } catch (e: any) {
    if (e?.name !== 'AbortError') ElMessage.error(errMsg(e))
  } finally {
    streaming.value = false
    ctrl.value = null
    patchLast((m) => ({ ...m, streaming: false }))
    loadConvs()
    // 兜底：用户中途停止时可能收不到 context 事件
    if (!ctxStats.value) refreshContext()
  }
}

function stop() {
  ctrl.value?.abort()
  streaming.value = false
}

async function confirmAction(idx: number, actionId: number, decision: 'approve' | 'reject') {
  patchAt(idx, (m) => ({ ...m, pendingResolved: true, streaming: decision === 'approve' }))
  if (decision === 'approve') streaming.value = true
  try {
    await confirmToolAction(actionId, decision, {
      onDelta: (t) => patchAt(idx, (m) => ({ ...m, content: m.content + t })),
      onReasoning: (t) => patchAt(idx, (m) => ({ ...m, reasoning: (m.reasoning || '') + t })),
      onCitations: (c, used) =>
        patchAt(idx, (m) => ({ ...m, citations: c, usedCitationIds: used })),
      onToolResult: (evt) => {
        // 审批通过后才真正执行（browser_action 是写工具），此时画面才有变化
        browserPanel.autoShowForTool(evt?.name)
        if (isBrowserTool(evt?.name)) browserPanel.follow(evt?.data?.url)
        patchAt(idx, (m) => {
          const steps = [...(m.steps || [])]
          const i = steps.findIndex((s) => s.call.id === evt.id && !s.result)
          if (i >= 0) steps[i] = { ...steps[i], result: evt }
          else steps.push({ call: { id: evt.id, name: evt.name, arguments: '' }, result: evt })
          return { ...m, steps }
        })
      },
      onPendingAction: (p) => patchAt(idx, (m) => ({ ...m, pending: p, pendingResolved: false, streaming: false })),
      onContext: (s) => {
        ctxStats.value = s
        if (s.percent >= 70 || s.compressing) refreshContext(2500)
      },
      onDone: (mid) => patchAt(idx, (m) => ({ ...m, id: mid, streaming: false, pending: undefined })),
      onError: (msg) => ElMessage.error(msg),
    })
    patchAt(idx, (m) => ({ ...m, streaming: false }))
    if (decision === 'approve') ElMessage.success('已执行')
  } catch (e) {
    patchAt(idx, (m) => ({ ...m, streaming: false }))
    ElMessage.error(errMsg(e))
  } finally {
    streaming.value = false
    loadConvs()
    if (!ctxStats.value) refreshContext()
  }
}

function copy(text: string) {
  navigator.clipboard?.writeText(text)
  ElMessage.success('已复制')
}
async function doFeedback(idx: number, val: number) {
  const m = msgs.value[idx]
  if (!m.id) {
    ElMessage.info('请等回答完成后再评价')
    return
  }
  const next = m.feedback === val ? 0 : val
  try {
    await chatApi.feedback(m.id, next)
    patchAt(idx, (msg) => ({ ...msg, feedback: next }))
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function openCitation(c: Citation) {
  try {
    const r = await docApi.content(c.doc_id)
    docPreview.value = { title: r.title, content: r.content || '', page: c.page ?? null, snippet: c.snippet || null }
    docPreviewOpen.value = true
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

// ---- 滚动 ----
function scrollToBottom() {
  nextTick(() => listRef.value?.setScrollTop?.(1e9))
}
function onScroll() {
  const wrap = listRef.value?.wrapRef as HTMLElement | undefined
  if (!wrap) return
  showJump.value = wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight >= 80
}
function jumpToLatest() {
  scrollToBottom()
  showJump.value = false
}
function jumpToMessage(idx: number) {
  const el = document.getElementById(`msg-${idx}`)
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    highlightIdx.value = idx
    setTimeout(() => {
      if (highlightIdx.value === idx) highlightIdx.value = null
    }, 1800)
  }
}

onMounted(load)
onBeforeUnmount(() => {
  ctrl.value?.abort()
  if (ctxTimer) clearTimeout(ctxTimer)
})
</script>

<style scoped>
.agent-chat { position: relative; height: 100%; }
.drop-mask {
  position: absolute; inset: 0; z-index: 60; pointer-events: none;
  border: 2px dashed var(--color-primary); border-radius: 12px;
  background: rgba(37, 99, 235, 0.08); color: var(--color-primary);
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; font-size: 14px;
}

.chat-shell {
  display: grid; grid-template-columns: 232px 1fr; gap: 16px;
  height: calc(100vh - var(--header-h) - 48px); min-height: 520px;
}
/* 展开浏览器画面：右侧让出固定宽度的一列（与「智能问答」保持一致的断点行为） */
.chat-shell.with-browser {
  grid-template-columns: 232px minmax(320px, 1fr) clamp(360px, 34vw, 640px);
}
.browser-col { min-width: 0; }

.conv-side { display: flex; flex-direction: column; overflow: hidden; padding: 12px; }
.cs-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.cs-title { font-size: 13px; font-weight: 600; color: var(--text-title); }
.cs-scroll { flex: 1; min-height: 0; }
.cs-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 8px; cursor: pointer; margin-bottom: 2px; }
.cs-item:hover { background: var(--bg-subtle); }
.cs-item.active { background: var(--color-primary-subtle); }
.cs-icon { color: var(--text-muted); font-size: 14px; }
.cs-item.active .cs-icon { color: var(--color-primary); }
.cs-name { flex: 1; font-size: 13px; color: var(--text-body); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cs-more { color: var(--text-muted); font-size: 14px; opacity: 0; }
.cs-item:hover .cs-more { opacity: 1; }

.chat-main { display: flex; flex-direction: column; overflow: hidden; }
.control-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 8px 12px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
.cb-agent { display: flex; flex-direction: column; min-width: 0; }
.cb-name { font-size: 14px; font-weight: 600; color: var(--text-title); }
.cb-desc { font-size: 12px; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 320px; }
.cb-group { display: flex; align-items: center; gap: 6px; }
.cb-label { font-size: 12px; color: var(--text-secondary); }
.cb-right { margin-left: auto; display: flex; align-items: center; gap: 2px; }
/* 浏览器画面开关的"已开启"态 */
.cb-on { color: var(--color-primary); background: var(--color-primary-subtle); }

.msg-area { position: relative; flex: 1; min-height: 0; display: flex; }
.msg-scroll { flex: 1; min-width: 0; min-height: 0; padding: 18px 20px; }

.chat-empty { text-align: center; padding: 56px 20px; display: flex; flex-direction: column; align-items: center; }
.ce-logo {
  width: 54px; height: 54px; border-radius: 16px; margin-bottom: 16px;
  background: linear-gradient(135deg, #2563eb, #3b82f6); color: #fff;
  display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 18px;
  box-shadow: 0 8px 20px -6px rgba(37, 99, 235, 0.5);
}
.ce-title { font-size: 20px; font-weight: 600; color: var(--text-title); margin: 0 0 6px; }
.ce-sub { font-size: 13.5px; color: var(--text-secondary); margin: 0; max-width: 520px; }

.msg { display: flex; gap: 12px; margin-bottom: 18px; border-radius: 8px; padding: 2px; transition: background 0.3s; }
.msg.user { flex-direction: row-reverse; }
.msg.hl { background: var(--warn-soft); padding: 8px; }
.msg-avatar { flex: 0 0 auto; padding-top: 2px; }
.ava-user, .ava-ai { width: 30px; height: 30px; border-radius: 9px; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; }
.ava-user { background: linear-gradient(135deg, #2563eb, #3b82f6); color: #fff; }
.ava-ai { background: linear-gradient(135deg, #8b5cf6, #6366f1); color: #fff; }
.msg-card {
  max-width: 82%; min-width: 0; padding: 10px 14px; border-radius: 12px;
  border: 1px solid var(--border); background: var(--bg-surface); box-shadow: var(--shadow-card);
}
.msg.user .msg-card { background: var(--color-primary-subtle); border-color: var(--color-primary-border); }
.plain-text { white-space: pre-wrap; font-size: 14px; line-height: 1.7; color: var(--text-body); }

.msg-pending { margin-top: 10px; }
.msg-actions { margin-top: 8px; border-top: 1px solid var(--border-subtle); padding-top: 6px; display: flex; align-items: center; gap: 2px; }

.jump-latest {
  position: absolute; left: 50%; transform: translateX(-50%); bottom: 12px; z-index: 20;
  display: inline-flex; align-items: center; gap: 4px; font-size: 12px; cursor: pointer;
  padding: 6px 12px; border-radius: 9999px; border: none; color: #fff;
  background: var(--color-primary); box-shadow: 0 2px 8px rgba(0, 0, 0, 0.18);
}

.pending-atts { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 16px 0; flex-shrink: 0; }

/* 输入区：卡片自带边框与圆角，外层只负责留白 */
.composer-wrap { padding: 10px 16px 12px; flex-shrink: 0; }
.cp-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; flex-shrink: 0;
  border: none; border-radius: 9px; background: transparent;
  color: var(--text-secondary); font-size: 16px; cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}
.cp-btn:hover { background: var(--bg-muted); color: var(--color-primary); }

.dp-label { font-size: 12px; color: var(--text-secondary); }
.dp-snippet { background: #fffbe6; border-left: 3px solid #faad14; padding: 8px 12px; margin: 6px 0 12px; border-radius: 4px; white-space: pre-wrap; font-size: 13px; }
.dp-full { white-space: pre-wrap; font-size: 13px; line-height: 1.7; color: var(--text-body); }

.outline-item { display: flex; align-items: center; gap: 8px; padding: 8px 6px; border-radius: 6px; cursor: pointer; }
.outline-item:hover { background: var(--bg-subtle); }
.outline-text { font-size: 13px; color: var(--text-body); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

@media (max-width: 900px) {
  .chat-shell { grid-template-columns: 1fr; }
  .conv-side { display: none; }
}

@media (max-width: 1440px) {
  .chat-shell.with-browser { grid-template-columns: 200px minmax(300px, 1fr) clamp(320px, 32vw, 480px); }
}

/* 窄屏：三列放不下，画面改成右下浮层（仍可用，不挤没聊天区） */
@media (max-width: 1100px) {
  .chat-shell.with-browser { grid-template-columns: 200px 1fr; }
  .browser-col {
    position: fixed; z-index: 900;
    right: 16px; bottom: 16px;
    width: min(520px, calc(100vw - 32px));
    height: min(70vh, 640px);
    box-shadow: var(--shadow-floating);
  }
}
</style>
