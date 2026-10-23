<template>
  <div
    class="chat-page"
    @dragenter="onDragEnter"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <!-- 拖入文件遮罩 -->
    <div v-if="dragging" class="drop-mask">
      <el-icon :size="40"><UploadFilled /></el-icon>
      <span>松开即可添加到附件</span>
    </div>

    <div class="chat-shell">
      <!-- ================= 历史会话侧栏 ================= -->
      <aside class="conv-side app-card">
        <div class="cs-head">
          <span class="cs-title">会话</span>
          <el-button size="small" type="primary" :icon="Plus" @click="newConversation">新对话</el-button>
        </div>
        <el-select
          v-if="canReadAll"
          v-model="viewUser"
          size="small"
          clearable
          placeholder="查看指定用户的会话"
          class="cs-user-select"
          @change="loadConvs"
        >
          <el-option
            v-for="u in viewUsers"
            :key="u.user_id"
            :value="u.user_id"
            :label="u.channel_label ? `[${u.channel_label}] ${u.external_id || u.name}（${u.conversation_count}）` : `${u.name}（${u.conversation_count}）`"
          />
        </el-select>
        <div v-if="viewUser" class="cs-readonly-tag">只读：查看他人会话</div>
        <el-scrollbar class="cs-scroll">
          <div
            v-for="c in convs"
            :key="c.id"
            class="cs-item"
            :class="{ active: c.id === convId }"
            @click="openConversation(c.id)"
          >
            <el-icon v-if="streamingKeys.has(String(c.id))" class="cs-loading is-loading"><Loading /></el-icon>
            <el-icon v-else class="cs-icon"><ChatLineRound /></el-icon>
            <span class="cs-name">{{ c.title || '未命名会话' }}</span>
            <el-dropdown trigger="click" @command="(cmd: string) => onConvCmd(cmd, c)" @click.stop>
              <el-icon class="cs-more" @click.stop><MoreFilled /></el-icon>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="rename"><el-icon><Edit /></el-icon>重命名</el-dropdown-item>
                  <el-dropdown-item command="md" divided>导出 Markdown</el-dropdown-item>
                  <el-dropdown-item command="pdf">导出 PDF</el-dropdown-item>
                  <el-dropdown-item command="docx">导出 Word</el-dropdown-item>
                  <el-dropdown-item command="share">生成分享链接</el-dropdown-item>
                  <el-dropdown-item command="delete" divided>
                    <el-icon><Delete /></el-icon>删除对话
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
          <el-empty v-if="!convs.length" description="暂无历史" :image-size="64" />
        </el-scrollbar>
      </aside>

      <!-- ================= 主对话区 ================= -->
      <section class="chat-main app-card">
        <!-- 控制栏：模型 | 检索模式 | 工具与视图 -->
        <div class="control-bar">
          <div class="cb-group">
            <span class="cb-label">模型</span>
            <el-select v-model="modelId" size="small" style="width: 200px" placeholder="默认模型" @change="changeModel">
              <el-option v-for="m in models" :key="m.id" :value="m.id" :label="m.display_name || m.model_name" />
            </el-select>
          </div>
          <el-divider direction="vertical" />
          <el-segmented
            v-model="retrievalMode"
            size="small"
            :options="[
              { label: '全库检索', value: 'auto' },
              { label: '指定知识库', value: 'custom' },
              { label: '不检索', value: 'off' },
            ]"
            @change="onRetrievalChange"
          />
          <el-select
            v-if="retrievalMode === 'custom'"
            v-model="selectedKbs"
            multiple
            collapse-tags
            :max-collapse-tags="2"
            size="small"
            class="kb-picker"
            placeholder="选择知识库"
            @change="onKbsChange"
          >
            <el-option v-for="k in kbs" :key="k.id" :value="k.id" :label="k.name" />
          </el-select>

          <div class="cb-right">
            <!-- 参数设置（温度）按钮移入输入框工具栏，这里不再重复放一个 -->
            <el-tooltip v-if="msgs.length >= 4" content="对话目录" placement="top">
              <el-button size="small" text :icon="List" @click="outlineOpen = true" />
            </el-tooltip>
            <el-tooltip content="清空当前对话" placement="top">
              <el-button size="small" text :icon="Delete" @click="newConversation" />
            </el-tooltip>
            <el-dropdown trigger="click">
              <el-button size="small" text :icon="Tools" />
              <template #dropdown>
                <div class="tools-pop" @click.stop>
                  <div class="tp-row">
                    <div>
                      <div class="tp-title">AI 操作</div>
                      <div class="tp-desc">允许 AI 调用平台工具（查用量 / 建库 / 传文档等）</div>
                    </div>
                    <el-switch v-model="aiTools" size="small" />
                  </div>
                  <div v-if="aiTools" class="tp-row tp-row-top">
                    <div>
                      <div class="tp-title">免确认</div>
                      <div class="tp-desc">写操作直接执行（选择会被记住）</div>
                    </div>
                    <el-switch v-model="autoWrite" size="small" @change="onAutoWrite" />
                  </div>
                </div>
              </template>
            </el-dropdown>
          </div>
        </div>

        <div v-if="showParams" class="param-bar">
          <span>温度</span>
          <el-slider v-model="temperature" :min="0" :max="1" :step="0.1" style="width: 180px" />
          <span class="param-val">{{ temperature }}</span>
          <span class="param-tip">越低越稳定、越高越发散</span>
        </div>

        <!-- 消息列表 + 右侧迷你时间轴 -->
        <div class="msg-area">
          <el-scrollbar ref="scrollRef" class="msg-scroll" @scroll="onScroll">
          <div v-if="!msgs.length" class="chat-empty">
            <div class="ce-logo">AI</div>
            <h3 class="ce-title">有什么可以帮你？</h3>
            <p class="ce-sub">自动检索知识库辅助回答；也可切到「不检索（纯聊天）」。可拖入图片 / 文档。</p>
            <div class="ce-suggests">
              <button v-for="s in suggests" :key="s" class="ce-chip" @click="quickAsk(s)">{{ s }}</button>
            </div>
          </div>

          <div v-if="hasMoreHistory[curKey]" class="load-earlier">
            <el-button text :loading="loadingEarlier" @click="loadEarlier">加载更早的消息</el-button>
          </div>

          <div
            v-for="(m, i) in msgs"
            :id="`msg-${i}`"
            :key="i"
            class="msg"
            :class="[m.role, { hl: highlightIdx === i }]"
          >
            <div class="msg-avatar">
              <span v-if="m.role === 'user'" class="ava-user">{{ userInitial }}</span>
              <span v-else class="ava-ai">AI</span>
            </div>
            <div class="msg-card">
              <!-- 思考过程 -->
              <ReasoningBlock
                v-if="m.role === 'assistant'"
                :reasoning="m.reasoning"
                :streaming="m.streaming"
                :duration-ms="(m as any).reasoningMs"
              />
              <!-- 工具调用步骤 -->
              <ToolSteps v-if="m.role === 'assistant' && (m.steps || []).length" :steps="(m.steps as any)" />
              <!-- 正文 -->
              <MarkdownBody v-if="m.role === 'assistant' && !m.streaming" :content="m.content" />
              <div v-else class="plain-text">{{ m.content || (m.streaming ? '…' : '') }}</div>

              <!-- 附件（用户上传） -->
              <AttachmentView v-for="a in m.attachments || []" :key="a.file_key" :att="a" />

              <!-- 生成的文件（产物） -->
              <div v-if="(m.artifacts || []).length" class="artifacts">
                <div class="artifacts-label">生成的文件：</div>
                <AttachmentView
                  v-for="a in m.artifacts || []"
                  :key="a.artifact_id"
                  artifact
                  :att="{ artifact_id: a.artifact_id, name: a.name, mime: a.mime, size: a.size }"
                />
              </div>

              <!-- HITL 待确认 -->
              <div v-if="m.pending" class="msg-pending">
                <PendingActionCard :action="m.pending" :resolved="m.pendingResolved" @confirm="(aid, d) => confirmAction(i, aid, d)" />
              </div>

              <!-- 引用来源 -->
              <div v-if="m.citations?.length" class="cites">
                <div class="cites-label">引用来源（点击查看）：</div>
                <div class="cite-tags">
                  <el-tag
                    v-for="(c, ci) in m.citations"
                    :key="ci"
                    :type="c.source_uri ? 'warning' : 'primary'"
                    class="cite-tag"
                    @click="c.source_uri ? windowOpen(c.source_uri) : openCitation(c)"
                  >
                    [{{ ci + 1 }}] {{ c.doc_title || '文档' }}{{ c.page ? ` · 第${c.page}页` : '' }}{{ c.source_uri ? ' ↗' : '' }}
                  </el-tag>
                </div>
              </div>

              <!-- 操作条：悬浮显示，不常驻占位 -->
              <div v-if="m.role === 'assistant' && !m.streaming && m.content" class="msg-actions">
                <div class="msg-actions-inner">
                  <el-tooltip content="复制" placement="top">
                    <el-button size="small" text :icon="DocumentCopy" @click="copy(m.content)" />
                  </el-tooltip>
                  <el-tooltip content="重新生成" placement="top">
                    <el-button size="small" text :icon="RefreshRight" @click="regenerate" />
                  </el-tooltip>
                  <el-tooltip content="有帮助" placement="top">
                    <el-button size="small" text :icon="Select" :class="{ 'fb-on': m.feedback === 1 }" @click="doFeedback(i, 1)" />
                  </el-tooltip>
                  <el-tooltip content="没帮助" placement="top">
                    <el-button size="small" text :icon="CloseBold" :class="{ 'fb-off': m.feedback === -1 }" @click="doFeedback(i, -1)" />
                  </el-tooltip>
                  <span v-if="m.usage" class="usage-inline" :title="usageFull(m)">
                    ↑{{ compactNum(m.usage.prompt_tokens) }} ↓{{ compactNum(m.usage.completion_tokens) }}
                    <template v-if="(m.usage.cached_tokens ?? 0) > 0"> · 缓存 {{ compactNum(m.usage.cached_tokens) }}</template>
                    <template v-if="m.model"> · {{ m.model }}</template>
                  </span>
                </div>
              </div>
            </div>
          </div>
        </el-scrollbar>

          <!-- 右侧迷你时间轴：每轮一根横杠，hover 预览、点击跳转、当前轮高亮 -->
          <MessageTimeline
            :items="timelineItems"
            :get-root="() => scrollRef?.wrapRef"
            :on-jump="jumpToMessage"
          />

          <!-- 回到最新 -->
          <button v-if="showJump" class="jump-latest" @click="jumpToLatest">
            <el-icon><ArrowDown /></el-icon>回到最新
          </button>
        </div>

        <!-- 用量统计条 -->
        <div class="usage-bar">
          <span>本会话累计：<b>{{ convUsage ? `${convUsage.total_tokens} tokens` : '-' }}</b>{{ convUsage ? ` / ${convUsage.calls} 次` : '' }}</span>
          <span>今日消耗：<b>{{ todayUsage ? `${todayUsage.total_tokens} tokens` : '-' }}</b>{{ todayUsage ? ` / ${todayUsage.calls} 次` : '' }}</span>
          <span>命中缓存：<b>{{ todayUsage?.cached_tokens ?? 0 }}</b></span>
          <el-tooltip v-if="convSummary" :content="convSummary" placement="top">
            <el-tag type="primary" size="small">已压缩历史</el-tag>
          </el-tooltip>
          <span v-if="todayUsage" class="usage-right">↑{{ todayUsage.prompt_tokens }} ↓{{ todayUsage.completion_tokens }}</span>
        </div>

        <!-- 待发送附件 -->
        <div v-if="pendingAtts.length" class="pending-atts">
          <el-tag
            v-for="(a, i) in pendingAtts"
            :key="a.file_key"
            closable
            @close="pendingAtts.splice(i, 1)"
          >
            {{ a.name }}
            <PendingAttachmentProgress v-if="a.doc_id" :doc-id="a.doc_id" />
          </el-tag>
        </div>

        <!-- 输入区：圆角卡片 + 内嵌底部工具栏（发送键收在卡片右下角） -->
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
              <el-select
                v-if="kbs.length"
                v-model="docKb"
                size="small"
                class="cp-kb"
                clearable
                placeholder="文档入库到（可空）"
              >
                <el-option v-for="k in kbs" :key="k.id" :value="k.id" :label="k.name" />
              </el-select>
            </template>
            <template #context>
              <ContextRing
                :stats="ctxStats"
                :has-conv="!!convId"
                :compressing="!!ctxStats?.compressing"
                @refresh="refreshContext"
              />
            </template>
            <template #right>
              <el-tooltip content="参数设置（温度等）" placement="top">
                <button type="button" class="cp-btn" :class="{ on: showParams }" @click="showParams = !showParams">
                  <el-icon><Setting /></el-icon>
                </button>
              </el-tooltip>
            </template>
          </ChatComposer>
        </div>
      </section>
    </div>

    <!-- 引用原文预览抽屉 -->
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
  Plus, MoreFilled, Edit, Delete, Setting, List, Tools, Paperclip,
  DocumentCopy, RefreshRight, Select, CloseBold, ArrowDown, Loading,
  UploadFilled,
} from '@element-plus/icons-vue'
import MarkdownBody from '@/components/MarkdownBody.vue'
import AttachmentView from '@/components/AttachmentView.vue'
import ReasoningBlock from '@/components/ReasoningBlock.vue'
import ToolSteps from '@/components/ToolSteps.vue'
import MessageTimeline from '@/components/MessageTimeline.vue'
import PendingActionCard from '@/components/PendingActionCard.vue'
import PendingAttachmentProgress from '@/components/PendingAttachmentProgress.vue'
import ChatComposer from '@/components/ChatComposer.vue'
import ContextRing from '@/components/ContextRing.vue'
import {
  chatApi, confirmToolAction, docApi, kbApi, providerApi, streamChat, usageApi,
  type Attachment, type ArtifactRef, type Citation, type ContextStats, type Conversation, type KB,
  type Message, type ModelConfig, type PendingActionEvt, type StreamHandlers, type UsageTotals,
} from '@/api'
import { errMsg } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

interface Msg {
  id?: number
  role: 'user' | 'assistant'
  content: string
  reasoning?: string
  citations?: Citation[]
  attachments?: Attachment[]
  artifacts?: ArtifactRef[]
  usage?: Record<string, number>
  model?: string
  feedback?: number | null
  streaming?: boolean
  /** 思考耗时（ms），流式结束后用于折叠成「思考了 N 秒」 */
  reasoningMs?: number
  steps?: { call: any; result?: any }[]
  pending?: PendingActionEvt
  pendingResolved?: boolean
}

const route = useRoute()
const auth = useAuthStore()
const userInitial = computed(() =>
  (auth.user?.display_name || auth.user?.username || 'U').slice(0, 1).toUpperCase(),
)

const kbs = ref<KB[]>([])
const models = ref<ModelConfig[]>([])
const modelId = ref<number>()
const convs = ref<Conversation[]>([])
const convId = ref<number>()
const canReadAll = computed(() => auth.hasPermission('chat:read_all'))
const viewUsers = ref<{ user_id: number; name: string; conversation_count: number; channel_label?: string | null; external_id?: string | null }[]>([])
const viewUser = ref<number | undefined>()
const input = ref('')
const temperature = ref(0.2)
const showParams = ref(false)
const pendingAtts = ref<Attachment[]>([])
const docKb = ref<number>()
const todayUsage = ref<UsageTotals | null>(null)
const convUsage = ref<UsageTotals | null>(null)
const convSummary = ref<string | null>(null)
/** 上下文占用（输入框圆环）：随每轮回答的 context 事件刷新，切会话时重拉 */
const ctxStats = ref<ContextStats | null>(null)
let ctxTimer: ReturnType<typeof setTimeout> | null = null
const fileRef = ref<HTMLInputElement>()
const scrollRef = ref<any>(null)
const showJump = ref(false)
const highlightIdx = ref<number | null>(null)
const outlineOpen = ref(false)
const dragging = ref(false)

// 检索模式 / 工具开关（记忆上次选择）
const retrievalMode = ref<'auto' | 'custom' | 'off'>(
  (localStorage.getItem('chat_retrieval_mode') as any) || 'auto',
)
const selectedKbs = ref<number[]>(readSaved<number[]>('chat_selected_kbs', []))
const aiTools = ref(true)
const autoWrite = ref(localStorage.getItem('chat_auto_write') === '1')

// 引用原文预览
const docPreviewOpen = ref(false)
const docPreview = ref<{ title: string; content: string; page?: number | null; snippet?: string | null } | null>(null)

// 会话隔离的消息与流式状态（切换对话不打断后台生成）
const DRAFT = 'draft'
const convMsgs = ref<Record<string, Msg[]>>({})
const streamingKeys = ref<Set<string>>(new Set())
let draftMovedTo: string | null = null
const curKey = computed(() => (convId.value != null ? String(convId.value) : DRAFT))
const msgs = computed(() => convMsgs.value[curKey.value] || [])
const streaming = computed(() => streamingKeys.value.has(curKey.value))
/** 有正文或只有附件都可以发；纯空白且无附件时禁用发送键 */
const canSend = computed(() => !!input.value.trim() || pendingAtts.value.length > 0)
const abortMap = new Map<string, AbortController>()

// 时间轴条目（每轮）
const timelineItems = computed(() =>
  msgs.value.map((m, i) => ({ index: i, role: m.role, text: m.content })),
)

const suggests = ['总结一下知识库里的核心内容', '帮我画一个阳光明媚的页面', '把关键流程整理成清单']

function readSaved<T>(key: string, dft: T): T {
  try {
    const v = JSON.parse(localStorage.getItem(key) || 'null')
    return v ?? dft
  } catch {
    return dft
  }
}

function resolveKey(key: string) {
  return key === DRAFT && draftMovedTo ? draftMovedTo : key
}
function patchLast(key: string, fn: (m: Msg) => Msg) {
  const k = resolveKey(key)
  const arr = convMsgs.value[k]
  if (!arr?.length) return
  const c = arr.slice()
  c[c.length - 1] = fn(c[c.length - 1])
  convMsgs.value = { ...convMsgs.value, [k]: c }
}
function patchAt(key: string, idx: number, fn: (m: Msg) => Msg) {
  const k = resolveKey(key)
  const arr = convMsgs.value[k]
  if (!arr) return
  const c = arr.slice()
  c[idx] = fn(c[idx])
  convMsgs.value = { ...convMsgs.value, [k]: c }
}
function setStreamingKey(key: string, on: boolean) {
  const k = resolveKey(key)
  const n = new Set(streamingKeys.value)
  if (on) n.add(k)
  else n.delete(k)
  streamingKeys.value = n
}
function adoptDraft(realId: number) {
  const k = String(realId)
  draftMovedTo = k
  const draftMsgs = convMsgs.value[DRAFT] || []
  const next = { ...convMsgs.value }
  delete next[DRAFT]
  next[k] = draftMsgs
  convMsgs.value = next
  if (streamingKeys.value.has(DRAFT)) {
    const n = new Set(streamingKeys.value)
    n.delete(DRAFT)
    n.add(k)
    streamingKeys.value = n
  }
  const ctrl = abortMap.get(DRAFT)
  if (ctrl) {
    abortMap.set(k, ctrl)
    abortMap.delete(DRAFT)
  }
  convId.value = realId
}

async function loadConvs() {
  try {
    convs.value = viewUser.value
      ? ((await chatApi.adminConversations(viewUser.value)) as Conversation[])
      : await chatApi.conversations()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}
async function refreshUsage() {
  try {
    todayUsage.value = await usageApi.meToday()
  } catch {
    /* ignore */
  }
  if (convId.value) {
    try {
      convUsage.value = await chatApi.usage(convId.value)
    } catch {
      /* ignore */
    }
  } else convUsage.value = null
}

/**
 * 拉取上下文占用。压缩是后台异步任务，刚发完时指标可能还处在高位，
 * 所以 afterMs 内再补拉一次，让圆环能自己回落到压缩后的水位。
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
    // 期间可能已切到别的会话，丢弃过期结果
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

function onRetrievalChange(v: any) {
  localStorage.setItem('chat_retrieval_mode', v)
}
function onKbsChange(v: number[]) {
  localStorage.setItem('chat_selected_kbs', JSON.stringify(v))
}
function onAutoWrite(v: any) {
  localStorage.setItem('chat_auto_write', v ? '1' : '0')
}

async function openConversation(id: number) {
  convId.value = id
  const k = String(id)
  if (!streamingKeys.value.has(k)) {
    try {
      // 只取最近一页（长会话不全量拉）；更早的历史用「加载更早」按钮增量取
      const ms = await chatApi.messages(id, { limit: PAGE_SIZE })
      convMsgs.value = { ...convMsgs.value, [k]: mapMessages(ms) }
      hasMoreHistory.value = { ...hasMoreHistory.value, [k]: ms.length >= PAGE_SIZE }
    } catch (e) {
      ElMessage.error(errMsg(e))
    }
  }
  const c = convs.value.find((x) => x.id === id)
  if (c?.model_config_id) modelId.value = c.model_config_id
  convSummary.value = c?.summary || null
  refreshContext()
  scrollToBottom()
}

const PAGE_SIZE = 50
const hasMoreHistory = ref<Record<string, boolean>>({})
const loadingEarlier = ref(false)

function mapMessages(ms: Message[]) {
  return ms
    .filter((m) => m.role === 'user' || m.role === 'assistant')
    .map((m) => ({
      id: m.id,
      role: m.role as 'user' | 'assistant',
      content: m.content,
      reasoning: m.reasoning,
      citations: m.citations,
      attachments: m.attachments,
      artifacts: m.artifacts,
      feedback: m.feedback,
      usage: m.usage,
      model: m.model,
    }))
}

/** 加载更早的历史消息，前置到当前列表头部。 */
async function loadEarlier() {
  const id = convId.value
  if (!id) return
  const k = String(id)
  const cur = convMsgs.value[k] || []
  const firstId = cur.find((m) => m.id)?.id
  if (!firstId) return
  loadingEarlier.value = true
  try {
    const older = await chatApi.messages(id, { before_id: firstId, limit: PAGE_SIZE })
    const mapped = mapMessages(older)
    if (mapped.length) {
      convMsgs.value = { ...convMsgs.value, [k]: [...mapped, ...cur] }
    }
    hasMoreHistory.value = { ...hasMoreHistory.value, [k]: older.length >= PAGE_SIZE }
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loadingEarlier.value = false
  }
}

function newConversation() {
  convId.value = undefined
  draftMovedTo = null
  convMsgs.value = { ...convMsgs.value, [DRAFT]: [] }
  pendingAtts.value = []
  convUsage.value = null
  // 草稿态没有会话 id，圆环归零（组件会提示"发第一条后开始统计"）
  ctxStats.value = null
  if (ctxTimer) {
    clearTimeout(ctxTimer)
    ctxTimer = null
  }
}

async function changeModel(id: number) {
  if (convId.value) {
    try {
      await chatApi.setModel(convId.value, id)
      loadConvs()
    } catch {
      /* ignore */
    }
  }
}

function onConvCmd(cmd: string, c: Conversation) {
  if (cmd === 'rename') renameConversation(c)
  else if (cmd === 'share') shareConversation(c)
  else if (cmd === 'delete') confirmDelete(c)
  else exportConversation(c, cmd as 'md' | 'pdf' | 'docx')
}
function renameConversation(c: Conversation) {
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
function confirmDelete(c: Conversation) {
  ElMessageBox.confirm('对话记录与消息将一并删除，不可恢复。', '删除该对话？', {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消',
  })
    .then(async () => {
      await chatApi.remove(c.id)
      loadConvs()
      if (convId.value === c.id) newConversation()
    })
    .catch(() => {})
}
async function exportConversation(c: Conversation, fmt: 'md' | 'pdf' | 'docx') {
  try {
    const token = localStorage.getItem('access_token') || ''
    const r = await fetch(chatApi.exportUrl(c.id, fmt), { headers: { Authorization: `Bearer ${token}` } })
    if (!r.ok) throw new Error('导出失败')
    const blob = await r.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${c.title || '对话记录'}.${fmt}`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}
async function shareConversation(c: Conversation) {
  try {
    const r = await chatApi.share(c.id)
    const full = `${window.location.origin}${r.url}`
    ElMessageBox.alert(
      `<div style="word-break:break-all;font-size:12px">${full}</div>` +
        `<div style="color:#94a3b8;font-size:12px;margin-top:8px">有效期约 ${Math.round(r.expires_in / 60)} 分钟，过期需重新生成。</div>`,
      '分享链接（免登录，短期有效）',
      { dangerouslyUseHTMLString: true, confirmButtonText: '知道了' },
    )
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

// ---- 上传（含拖拽 / 多选）----
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
      const kind = f.type.startsWith('image/')
        ? 'image'
        : f.type.startsWith('video/') || f.type.startsWith('audio/')
          ? 'video'
          : 'document'
      const att = await chatApi.uploadAttachment(f, { kind, kbId: kind === 'document' ? docKb.value : undefined })
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
  if (!files.length) return
  await uploadAll(files)
}

// ---- 发送 / 流式 ----
function quickAsk(t: string) {
  input.value = t
  send()
}
async function send() {
  const text = input.value.trim()
  if ((!text && !pendingAtts.value.length) || streaming.value) return
  input.value = ''
  const atts = pendingAtts.value
  pendingAtts.value = []
  const startKey = curKey.value
  const startedAt = performance.now()
  convMsgs.value = {
    ...convMsgs.value,
    [startKey]: [
      ...(convMsgs.value[startKey] || []),
      { role: 'user', content: text, attachments: atts },
      { role: 'assistant', content: '', streaming: true },
    ],
  }
  const handlers: StreamHandlers = {
    onMeta: (d) => {
      if (startKey === DRAFT) adoptDraft(d.conversation_id)
    },
    onDelta: (t) => patchLast(startKey, (m) => ({ ...m, content: m.content + t })),
    onReasoning: (t) => patchLast(startKey, (m) => ({ ...m, reasoning: (m.reasoning || '') + t })),
    onToolCall: (evt) => patchLast(startKey, (m) => ({ ...m, steps: [...(m.steps || []), { call: evt }] })),
    onToolResult: (evt) =>
      patchLast(startKey, (m) => {
        const steps = [...(m.steps || [])]
        const i = steps.findIndex((s) => s.call.id === evt.id && !s.result)
        if (i >= 0) steps[i] = { ...steps[i], result: evt }
        else steps.push({ call: { id: evt.id, name: evt.name, arguments: '' }, result: evt })
        return { ...m, steps }
      }),
    onPendingAction: (evt) => patchLast(startKey, (m) => ({ ...m, pending: evt, streaming: false })),
    onCitations: (cit) => patchLast(startKey, (m) => ({ ...m, citations: cit })),
    onCitationCheck: (evt) => {
      if (evt.has_fake_cite) ElMessage.warning(evt.warning)
    },
    onUsage: (u) => patchLast(startKey, (m) => ({ ...m, usage: u })),
    // 上下文快照：先按即时值刷新圆环，2.5s 后再拉一次——
    // 后台压缩是异步的，补拉才能看到环口从高位回落到压缩后的水位
    onContext: (s) => {
      ctxStats.value = s
      convSummary.value = s.compressed ? convSummary.value : null
      if (s.percent >= 70 || s.compressing) refreshContext(2500)
    },
    // 记录思考耗时，供 ReasoningBlock 折叠成「思考了 N 秒」
    onDone: (mid) =>
      patchLast(startKey, (m) => ({
        ...m, id: mid, streaming: false,
        reasoningMs: m.reasoning ? Math.round(performance.now() - startedAt) : undefined,
      })),
    onError: (msg) => ElMessage.error(msg),
  }
  await runStream(
    {
      conversation_id: convId.value,
      kb_ids: retrievalMode.value === 'custom' ? selectedKbs.value : [],
      use_retrieval: retrievalMode.value !== 'off',
      message: text || '（见附件）',
      model_config_id: modelId.value,
      temperature: temperature.value,
      attachments: atts,
      use_tools: aiTools.value,
      allow_auto_write: autoWrite.value,
    },
    handlers,
    undefined,
    startKey,
  )
  loadConvs()
}

async function runStream(payload: any, handlers: any, url?: string, key: string = curKey.value) {
  const ctrl = new AbortController()
  abortMap.set(key, ctrl)
  setStreamingKey(key, true)
  try {
    await streamChat(payload, handlers, ctrl.signal, url)
  } catch (e: any) {
    if (e?.name !== 'AbortError') ElMessage.error(errMsg(e))
  } finally {
    abortMap.delete(key)
    setStreamingKey(key, false)
    refreshUsage()
    const cid = Number(key)
    if (Number.isFinite(cid) && key !== DRAFT) {
      refreshLastAssistant(cid)
      // 兜底：流被中断（用户点停止）时可能收不到 context 事件
      if (!ctxStats.value) refreshContext()
    }
  }
}

function stop() {
  abortMap.get(curKey.value)?.abort()
  setStreamingKey(curKey.value, false)
}

async function regenerate() {
  if (!convId.value) return
  const startKey = curKey.value
  const c = [...(convMsgs.value[startKey] || [])]
  if (c.length && c[c.length - 1].role === 'assistant') c.pop()
  c.push({ role: 'assistant', content: '', streaming: true })
  convMsgs.value = { ...convMsgs.value, [startKey]: c }
  await runStream(
    {},
    {
      onDelta: (t: string) => patchLast(startKey, (m) => ({ ...m, content: m.content + t })),
      onReasoning: (t: string) => patchLast(startKey, (m) => ({ ...m, reasoning: (m.reasoning || '') + t })),
      onCitations: (cit: Citation[]) => patchLast(startKey, (m) => ({ ...m, citations: cit })),
      onUsage: (u: any) => patchLast(startKey, (m) => ({ ...m, usage: u })),
      onDone: (mid: number) => patchLast(startKey, (m) => ({ ...m, id: mid, streaming: false })),
      onError: (msg: string) => ElMessage.error(msg),
    },
    chatApi.regenerateUrl(convId.value),
    startKey,
  )
}

async function confirmAction(idx: number, actionId: number, decision: 'approve' | 'reject') {
  const key = curKey.value
  patchAt(key, idx, (m) => ({ ...m, pendingResolved: true, streaming: decision === 'approve' }))
  try {
    await confirmToolAction(actionId, decision, {
      onToolResult: (data: any) =>
        patchAt(key, idx, (m) => {
          const steps = [...(m.steps || [])]
          let i = steps.findIndex((s) => s.call.id === data.id && !s.result)
          if (i < 0) for (let k = steps.length - 1; k >= 0; k--) if (!steps[k].result) { i = k; break }
          if (i >= 0) steps[i] = { ...steps[i], result: data }
          else steps.push({ call: { id: data.id, name: data.name, arguments: '' }, result: data })
          return { ...m, steps }
        }),
      onDelta: (t) => patchAt(key, idx, (m) => ({ ...m, content: (m.content || '') + t })),
      onReasoning: (t) => patchAt(key, idx, (m) => ({ ...m, reasoning: (m.reasoning || '') + t })),
      onCitations: (cit) => patchAt(key, idx, (m) => ({ ...m, citations: cit })),
      onPendingAction: (p) => patchAt(key, idx, (m) => ({ ...m, pending: p, pendingResolved: false, streaming: false })),
      onContext: (s) => {
        ctxStats.value = s
        if (s.percent >= 70 || s.compressing) refreshContext(2500)
      },
      onDone: (mid) => patchAt(key, idx, (m) => ({ ...m, id: mid, streaming: false, pending: undefined })),
      onError: (msg) => ElMessage.error(msg),
    })
    patchAt(key, idx, (m) => ({ ...m, streaming: false }))
    if (decision === 'approve') ElMessage.success('已执行')
    const cid = Number(key)
    if (Number.isFinite(cid) && key !== DRAFT) refreshLastAssistant(cid)
  } catch (e) {
    patchAt(key, idx, (m) => ({ ...m, streaming: false }))
    ElMessage.error(errMsg(e))
  }
}

async function refreshLastAssistant(cid: number) {
  try {
    const ms = await chatApi.messages(cid)
    const last = [...ms].reverse().find((x) => x.role === 'assistant')
    if (!last) return
    patchLast(String(cid), (m) => ({ ...m, artifacts: last.artifacts, usage: last.usage || m.usage, model: last.model || m.model }))
  } catch {
    /* ignore */
  }
}

function copy(text: string) {
  navigator.clipboard?.writeText(text)
  ElMessage.success('已复制')
}

/** 大数字压缩：16375 -> 16.4k，避免操作条被超长 token 数字撑开 */
function compactNum(n?: number | null): string {
  if (!n) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

/** 悬浮 title：保留完整数字，压缩显示不影响核对 */
function usageFull(m: any): string {
  const u = m.usage || {}
  const parts = [
    `输入 ${u.prompt_tokens ?? 0} tokens`,
    `输出 ${u.completion_tokens ?? 0} tokens`,
    `合计 ${u.total_tokens ?? 0} tokens`,
  ]
  if ((u.cached_tokens ?? 0) > 0) parts.push(`命中缓存 ${u.cached_tokens} tokens`)
  if (m.model) parts.push(u.model ?? m.model)
  return parts.join(' · ')
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
    patchAt(curKey.value, idx, (msg) => ({ ...msg, feedback: next }))
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
function windowOpen(u: string) {
  window.open(u, '_blank')
}

// ---- 滚动 ----
function scrollToBottom() {
  nextTick(() => scrollRef.value?.setScrollTop?.(1e9))
}
function onScroll() {
  const wrap = scrollRef.value?.wrapRef as HTMLElement | undefined
  if (!wrap) return
  const bottom = wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 80
  showJump.value = !bottom
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

onMounted(async () => {
  kbApi
    .list()
    .then((ks) => {
      kbs.value = ks
      const saved = readSaved<number[]>('chat_selected_kbs', [])
      const valid = new Set(ks.map((k) => k.id))
      const kept = saved.filter((id) => valid.has(id))
      if (kept.length) selectedKbs.value = kept
    })
    .catch(() => {})
  providerApi
    .configs()
    .then((cs) => {
      const chatModels = cs.filter((c) => c.purpose === 'chat')
      models.value = chatModels
      const def = chatModels.find((m) => m.is_default) || chatModels[0]
      if (def) modelId.value = def.id
    })
    .catch(() => {})
  if (auth.hasPermission('chat:read_all')) {
    chatApi.adminChatUsers().then((us) => (viewUsers.value = us)).catch(() => {})
  }
  await loadConvs()
  refreshUsage()
  const q = route.query.conv
  if (q) await openConversation(Number(q))
})

onBeforeUnmount(() => {
  // 离开页面：清掉上下文补拉定时器，避免对已卸载组件写 ref
  if (ctxTimer) clearTimeout(ctxTimer)
})
</script>

<style scoped>
.chat-page { position: relative; height: 100%; }
.drop-mask {
  position: absolute; inset: 0; z-index: 60; pointer-events: none;
  border: 2px dashed var(--color-primary); border-radius: 12px;
  background: rgba(37, 99, 235, 0.08); color: var(--color-primary);
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; font-size: 14px;
}

.chat-shell {
  display: grid;
  grid-template-columns: 232px 1fr;
  gap: 16px;
  height: calc(100vh - var(--header-h) - 48px);
  min-height: 520px;
}

/* 会话侧栏 */
.conv-side { display: flex; flex-direction: column; overflow: hidden; padding: 12px; }
.cs-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.cs-user-select { width: 100%; margin-bottom: 8px; }
.cs-readonly-tag { font-size: 12px; color: var(--warn); background: var(--warn-soft); border: 1px solid var(--warn-border); border-radius: 6px; padding: 3px 8px; margin-bottom: 8px; }
.cs-title { font-size: 13px; font-weight: 600; color: var(--text-title); }
.cs-scroll { flex: 1; min-height: 0; }
.cs-item {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  border-radius: 8px; cursor: pointer; margin-bottom: 2px;
}
.cs-item:hover { background: var(--bg-subtle); }
.cs-item.active { background: var(--color-primary-subtle); }
.cs-icon { color: var(--text-muted); font-size: 14px; }
.cs-loading { color: var(--color-primary); font-size: 14px; }
.cs-item.active .cs-icon { color: var(--color-primary); }
.cs-name { flex: 1; font-size: 13px; color: var(--text-body); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cs-more { color: var(--text-muted); font-size: 14px; opacity: 0; }
.cs-item:hover .cs-more { opacity: 1; }

/* 主区 */
.chat-main { display: flex; flex-direction: column; overflow: hidden; }

.control-bar {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 8px 12px; border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.cb-group { display: flex; align-items: center; gap: 6px; }
.cb-label { font-size: 12px; color: var(--text-secondary); }
.cb-right { margin-left: auto; display: flex; align-items: center; gap: 2px; }
/* 知识库选择器：固定宽度，不独占整行（el-select 默认 width:100% 会撑满 flex 行） */
.kb-picker { width: 240px; flex: 0 0 auto; }
.kb-picker :deep(.el-select__wrapper) { min-height: 24px; }
.tools-pop { padding: 12px; min-width: 260px; }
.tp-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.tp-row-top { padding-top: 10px; margin-top: 10px; border-top: 1px solid var(--border-subtle); }
.tp-title { font-size: 13px; font-weight: 500; color: var(--text-title); }
.tp-desc { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }

.param-bar { display: flex; align-items: center; gap: 12px; padding: 8px 14px; border-bottom: 1px solid var(--border-subtle); font-size: 13px; color: var(--text-secondary); }
.param-val { font-variant-numeric: tabular-nums; color: var(--text-title); font-weight: 500; }
.param-tip { font-size: 12px; color: var(--text-muted); }

.msg-area { position: relative; flex: 1; min-height: 0; display: flex; }
.msg-scroll { flex: 1; min-width: 0; min-height: 0; padding: 18px 20px; }
.load-earlier { display: flex; justify-content: center; margin-bottom: 8px; }

.chat-empty { text-align: center; padding: 56px 20px; display: flex; flex-direction: column; align-items: center; }
.ce-logo {
  width: 54px; height: 54px; border-radius: 16px; margin-bottom: 16px;
  background: linear-gradient(135deg, #2563eb, #3b82f6); color: #fff;
  display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 18px;
  box-shadow: 0 8px 20px -6px rgba(37, 99, 235, 0.5);
}
.ce-title { font-size: 20px; font-weight: 600; color: var(--text-title); margin: 0 0 6px; }
.ce-sub { font-size: 13.5px; color: var(--text-secondary); margin: 0 0 22px; max-width: 520px; }
.ce-suggests { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
.ce-chip {
  padding: 8px 14px; border-radius: 9999px; border: 1px solid var(--border);
  background: var(--bg-surface); color: var(--text-body); font-size: 13px; cursor: pointer; font: inherit;
}
.ce-chip:hover { border-color: var(--color-primary-border); background: var(--color-primary-subtle); color: var(--brand-700); }

/* 消息 */
.msg { display: flex; gap: 12px; margin-bottom: 28px; border-radius: 8px; padding: 2px; transition: background 0.3s; }
.msg.user { flex-direction: row-reverse; }
.msg.hl { background: var(--warn-soft); padding: 8px; }
.msg-avatar { flex: 0 0 auto; padding-top: 2px; }
.ava-user, .ava-ai {
  width: 30px; height: 30px; border-radius: 9px; display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 600;
}
.ava-user { background: linear-gradient(135deg, #2563eb, #3b82f6); color: #fff; }
.ava-ai { background: linear-gradient(135deg, #8b5cf6, #6366f1); color: #fff; }
.msg-card {
  max-width: 80%; min-width: 0; padding: 10px 14px;
  border-radius: 12px; border: 1px solid var(--border); background: var(--bg-surface);
  box-shadow: var(--shadow-card);
}
.msg.user .msg-card { background: var(--color-primary-subtle); border-color: var(--color-primary-border); }
.plain-text { white-space: pre-wrap; font-size: 14px; line-height: 1.7; color: var(--text-body); }

.artifacts { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border-subtle); }
.artifacts-label { font-size: 12px; color: var(--text-secondary); margin-bottom: 4px; }

.msg-pending { margin-top: 10px; }

.cites { margin-top: 10px; border-top: 1px solid var(--border-subtle); padding-top: 8px; }
.cites-label { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
.cite-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.cite-tag { cursor: pointer; }

/* 操作条：悬浮在消息卡片下沿之外，默认透明，hover/聚焦时浮现。
   关键点：气泡宽度跟着内容走，短消息时只有几十像素；若让操作条
   参与这个宽度约束，它就会被压成 3~5 行并向上盖住正文。
   所以这里 width: max-content + nowrap，让它按自身内容撑开。 */
.msg-card { position: relative; }
.msg-actions {
  position: absolute; left: 6px; bottom: -16px; right: auto;
  width: max-content; max-width: calc(100vw - 220px);
  opacity: 0; pointer-events: none;
  transform: translateY(-2px);
  transition: opacity 0.15s ease, transform 0.15s ease;
  z-index: 5;
}
.msg:hover .msg-actions,
.msg-actions:focus-within { opacity: 1; pointer-events: auto; transform: translateY(0); }
/* 触屏/无 hover 环境：始终显示，否则点不到 */
@media (hover: none) { .msg-actions { opacity: 1; pointer-events: auto; transform: none; } }

.msg-actions-inner {
  display: inline-flex; align-items: center; gap: 2px;
  padding: 2px 4px; border-radius: 999px;
  white-space: nowrap;
  background: var(--bg-surface); border: 1px solid var(--border);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}
.msg-actions-inner .fb-on { color: #10b981; }
.msg-actions-inner .fb-off { color: #ef4444; }
.usage-inline { font-size: 12px; color: var(--text-muted); margin-left: 6px; padding-right: 4px; }

/* 回到最新 */
.jump-latest {
  position: absolute; left: 50%; transform: translateX(-50%); bottom: 12px; z-index: 20;
  display: inline-flex; align-items: center; gap: 4px; font-size: 12px; cursor: pointer;
  padding: 6px 12px; border-radius: 9999px; border: none; color: #fff;
  background: var(--color-primary); box-shadow: 0 2px 8px rgba(0, 0, 0, 0.18);
}

/* 用量条 */
.usage-bar {
  display: flex; gap: 18px; align-items: center; flex-wrap: wrap;
  padding: 5px 12px; border-top: 1px solid var(--border-subtle);
  font-size: 12px; color: var(--text-secondary); background: var(--bg-subtle); flex-shrink: 0;
}
.usage-bar b { color: var(--text-body); }
.usage-right { margin-left: auto; }

.pending-atts { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 16px 0; flex-shrink: 0; }

/* 输入区：卡片自带边框与圆角，外层只负责留白，不再画分隔线 */
.composer-wrap { padding: 10px 16px 12px; flex-shrink: 0; }

/* 工具栏里的图标按钮（附件、参数等）：无边框方形，hover 才出底色 */
.cp-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; flex-shrink: 0;
  border: none; border-radius: 9px; background: transparent;
  color: var(--text-secondary); font-size: 16px; cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}
.cp-btn:hover { background: var(--bg-muted); color: var(--color-primary); }
.cp-btn.on { background: var(--color-primary-subtle); color: var(--color-primary); }

/* 知识库选择器：工具栏内固定宽度，不撑满整行 */
.cp-kb { width: 168px; flex: 0 0 auto; }
.cp-kb :deep(.el-select__wrapper) { min-height: 26px; border-radius: 9px; }

/* 引用抽屉 */
.dp-label { font-size: 12px; color: var(--text-secondary); }
.dp-snippet {
  background: #fffbe6; border-left: 3px solid #faad14; padding: 8px 12px;
  margin: 6px 0 12px; border-radius: 4px; white-space: pre-wrap; font-size: 13px;
}
.dp-full { white-space: pre-wrap; font-size: 13px; line-height: 1.7; color: var(--text-body); }

.outline-item { display: flex; align-items: center; gap: 8px; padding: 8px 6px; border-radius: 6px; cursor: pointer; }
.outline-item:hover { background: var(--bg-subtle); }
.outline-text { font-size: 13px; color: var(--text-body); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

@media (max-width: 900px) {
  .chat-shell { grid-template-columns: 1fr; }
  .conv-side { display: none; }
}
</style>
