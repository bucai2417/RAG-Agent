<template>
  <aside class="blp">
    <!-- 顶部：标签页 + 动作 -->
    <div class="blp-bar">
      <el-dropdown trigger="click" @command="switchTab">
        <button type="button" class="blp-tab" :title="activeTab?.url || '选择标签页'">
          <el-icon><Monitor /></el-icon>
          <span class="blp-tab-title">{{ activeTab?.title || '选择标签页' }}</span>
          <el-icon class="blp-caret"><ArrowDown /></el-icon>
        </button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item v-for="t in tabs" :key="t.id" :command="t.id">
              <div class="blp-dd-item">
                <div class="blp-dd-title">{{ t.title || '(无标题)' }}</div>
                <div class="blp-dd-url">{{ t.url }}</div>
              </div>
            </el-dropdown-item>
            <el-dropdown-item v-if="!tabs.length" disabled>没有打开的标签页</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>

      <div class="blp-spacer" />
      <el-tooltip content="刷新页面" placement="bottom">
        <button type="button" class="blp-icon" :disabled="!connected" @click="reload">
          <el-icon><RefreshRight /></el-icon>
        </button>
      </el-tooltip>
      <el-tooltip content="关闭面板" placement="bottom">
        <button type="button" class="blp-icon" @click="emit('close')">
          <el-icon><Close /></el-icon>
        </button>
      </el-tooltip>
    </div>

    <!-- 地址栏 -->
    <div class="blp-url">
      <el-icon class="blp-url-ic" @click="navigate"><Position /></el-icon>
      <input
        v-model="urlInput"
        class="blp-url-input"
        :disabled="!connected"
        placeholder="输入网址后回车"
        @keydown.enter.prevent="navigate"
      />
    </div>

    <!-- 画面区 -->
    <div ref="stageRef" class="blp-stage">
      <canvas
        ref="canvasRef"
        class="blp-canvas"
        :class="{ grabbing }"
        @mousedown="onMouseDown"
        @mousemove="onMouseMove"
        @mouseup="onMouseUp"
        @mouseleave="onMouseLeave"
        @wheel.prevent="onWheel"
        @contextmenu.prevent="focusInput"
      />

      <!--
        输入承接层：canvas 既收不到键盘事件也收不到输入法组合事件，
        所有键盘输入统一从这里转发给页面。它铺在画面之上但 pointer-events:none，
        鼠标事件仍然落到 canvas。
      -->
      <textarea
        ref="inputRef"
        class="blp-input"
        aria-label="页面输入"
        @keydown="onKeyDown"
        @keyup="onKeyUp"
        @input="onInput"
        @compositionstart="composing = true"
        @compositionend="onCompositionEnd"
        @blur="onInputBlur"
      />

      <!-- 未连接 / 出错的蒙层：画面区始终占位，避免布局跳动 -->
      <div v-if="overlay" class="blp-overlay">
        <el-icon v-if="state === 'connecting'" class="is-loading" :size="22"><Loading /></el-icon>
        <el-icon v-else :size="22"><WarningFilled /></el-icon>
        <span>{{ overlay }}</span>
        <div class="blp-ov-actions">
          <el-button v-if="state === 'unavailable' && noBrowser" size="small" type="primary" @click="gotoInstall">
            安装内置浏览器
          </el-button>
          <el-button v-if="state !== 'connecting'" size="small" @click="reconnect">重试</el-button>
        </div>
      </div>

      <div v-if="focused && connected" class="blp-typing">
        <el-icon><EditPen /></el-icon>可直接输入，Esc 退出
      </div>
    </div>

    <!-- 底栏：状态 -->
    <div class="blp-foot">
      <span class="blp-dot" :class="connected ? 'on' : 'off'" />
      <span>{{ stateText }}</span>
      <span v-if="connected && fps" class="blp-fps">{{ fps }}fps</span>
      <div class="blp-spacer" />
      <span class="blp-hint">{{ focused ? '输入中' : '点击画面即可操作' }}</span>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowDown, Close, EditPen, Loading, Monitor,
  Position, RefreshRight, WarningFilled,
} from '@element-plus/icons-vue'
import { browserApi, type BrowserLiveTab } from '@/api'

const props = defineProps<{
  /** 面板打开时才连 WS；关闭即断开，不在后台白烧 CPU */
  open: boolean
  /**
   * AI 正在操作的页面地址（来自 browser_snapshot / browser_action 的结果）。
   *
   * 存在理由：AI 可能在别的标签页里干活。面板只显示"第一个标签"时，
   * 用户看到的画面和 AI 实际操作的不是同一个页面 —— 面板会误导人。
   */
  followUrl?: string
  /** 每次 follow 递增；URL 相同时靠它触发重新对齐（标签可能被关掉重开） */
  followSeq?: number
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

const router = useRouter()

type LiveState = 'idle' | 'connecting' | 'live' | 'error' | 'unavailable'

const state = ref<LiveState>('idle')
const tabs = ref<BrowserLiveTab[]>([])
const activeTabId = ref<string | null>(null)
/** AI 要求跟随的地址：标签列表还没到时先存下，拿到后补一次对齐 */
const pendingFollow = ref('')
const urlInput = ref('')
const noBrowser = ref(false)
const overlayMsg = ref('')
const fps = ref(0)
const focused = ref(false)
const grabbing = ref(false)

const stageRef = ref<HTMLElement>()
const canvasRef = ref<HTMLCanvasElement>()
const inputRef = ref<HTMLTextAreaElement>()

let ws: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let retry = 0

// ---- 派生状态 ----

const connected = computed(() => state.value === 'live')
const overlay = computed(() => {
  if (state.value === 'live') return ''
  if (state.value === 'connecting') return overlayMsg.value || '正在连接浏览器…'
  return overlayMsg.value || (state.value === 'unavailable' ? '浏览器不可用' : '连接已断开')
})
const activeTab = computed(() => tabs.value.find((t) => t.id === activeTabId.value) || null)
const stateText = computed(() => ({
  live: '已连接', connecting: '连接中', unavailable: '不可用', error: '已断开', idle: '未连接',
} as Record<LiveState, string>)[state.value])

// ---- 连接 ----

function connect() {
  if (!props.open) return
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return
  clearReconnect()

  state.value = 'connecting'
  overlayMsg.value = '正在连接浏览器…'

  let sock: WebSocket
  try {
    sock = new WebSocket(browserApi.liveUrl())
  } catch {
    state.value = 'error'
    overlayMsg.value = '无法创建连接'
    return
  }
  sock.binaryType = 'arraybuffer'
  ws = sock

  sock.onopen = () => {
    retry = 0
    // 连上就请求挂到当前标签；后端会先推一次标签列表
    sock.send(JSON.stringify({ type: 'attach' }))
  }
  sock.onmessage = (ev) => {
    if (typeof ev.data === 'string') onControl(ev.data)
    else onFrame(ev.data as ArrayBuffer)
  }
  sock.onclose = (ev) => {
    if (ws !== sock) return  // 已被新连接取代
    ws = null
    // 4401：后端鉴权失败。重连只会重复失败，必须让用户重新登录
    if (ev.code === 4401) {
      state.value = 'error'
      overlayMsg.value = '登录已失效，请重新登录'
      return
    }
    // 1011：后端已用 unavailable 说明过原因，不覆盖
    if (ev.code === 1011 && state.value === 'unavailable') return
    if (!props.open) return
    scheduleReconnect()
  }
}

function scheduleReconnect() {
  clearReconnect()
  if (!props.open) return
  state.value = 'error'
  overlayMsg.value = '连接已断开，正在重试…'
  // 指数退避：浏览器刚崩时秒级重连只会刷出同样的失败
  const delay = Math.min(1000 * 2 ** Math.min(retry, 3), 8000)
  retry += 1
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    connect()
  }, delay)
}

function clearReconnect() {
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
}

function reconnect() {
  retry = 0
  overlayMsg.value = ''
  connect()
}

function disconnect() {
  clearReconnect()
  const sock = ws
  ws = null
  if (sock) {
    sock.onopen = null
    sock.onmessage = null
    sock.onclose = null
    sock.onerror = null
    try { sock.close() } catch { /* ignore */ }
  }
  state.value = 'idle'
  fps.value = 0
  focused.value = false
}

function onControl(raw: string) {
  let d: any
  try { d = JSON.parse(raw) } catch { return }

  switch (d.type) {
    case 'tabs':
      tabs.value = Array.isArray(d.tabs) ? d.tabs : []
      if (d.active) activeTabId.value = d.active
      // 有一次被推迟的跟随请求：现在有列表了，兑现它。
      // `followRetried` 保证只重试一次：AI 报回的地址可能压根不存在
      // （比如它自己算错了），反复请求标签列表只会空转。
      if (pendingFollow.value) {
        const want = pendingFollow.value
        pendingFollow.value = ''
        followAiPage(want, false)
      }
      return
    case 'attached':
      activeTabId.value = d.tab_id
      urlInput.value = d.url || ''
      overlayMsg.value = ''
      // 后端默认连第一个标签；若 AI 正在别的页面上干活，这里要纠正过来。
      // 用 nextTick 之外的直接调用即可：followAiPage 只依赖 tabs 与 activeTabId。
      if (props.followUrl) followAiPage(props.followUrl)
      if (!tabs.value.length) send({ type: 'tabs' })
      return
    case 'navigated':
      // 只跟随主框架跳转：iframe 里的广告换地址不该改我们的地址栏
      urlInput.value = d.url || urlInput.value
      if (activeTab.value) activeTab.value.url = d.url || activeTab.value.url
      return
    case 'title':
      if (d.url) urlInput.value = d.url
      if (activeTab.value) {
        activeTab.value.title = d.title || activeTab.value.title
        activeTab.value.url = d.url || activeTab.value.url
      }
      return
    case 'unavailable':
      state.value = 'unavailable'
      noBrowser.value = !!d.no_browser
      overlayMsg.value = d.message || '浏览器不可用'
      return
    case 'error':
      // 动作级错误不该把面板打成"断开"：画面还在，只是这次没成功
      if (state.value === 'live') ElMessage.warning(d.message || '操作失败')
      else {
        state.value = 'error'
        overlayMsg.value = d.message || '操作失败'
      }
      return
    default:
      return
  }
}

// ---- 画面渲染 ----

let frameCount = 0
let fpsTimer: ReturnType<typeof setInterval> | null = null
let pendingFrame: ArrayBuffer | null = null
let decoding = false

function onFrame(buf: ArrayBuffer) {
  if (!buf || buf.byteLength === 0) return
  if (state.value !== 'live') {
    state.value = 'live'
    overlayMsg.value = ''
  }
  pendingFrame = buf
  // 只保留最新一帧：解码是异步的，排队只会越积越旧
  if (!decoding) void drainFrames()
}

async function drainFrames() {
  decoding = true
  try {
    while (pendingFrame) {
      const buf = pendingFrame
      pendingFrame = null
      await drawFrame(buf)
      frameCount += 1
    }
  } finally {
    decoding = false
  }
}

async function drawFrame(buf: ArrayBuffer) {
  const canvas = canvasRef.value
  if (!canvas) return
  let bmp: ImageBitmap
  try {
    // createImageBitmap 走原生解码，比 new Image()+ObjectURL 快且不占主线程
    bmp = await createImageBitmap(new Blob([buf], { type: 'image/jpeg' }))
  } catch {
    return
  }
  // 只在尺寸变化时改 canvas：写 width/height 会清空画布并重置上下文
  if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
    canvas.width = bmp.width
    canvas.height = bmp.height
    fitCanvas()
  }
  canvas.getContext('2d')?.drawImage(bmp, 0, 0)
  bmp.close()
}

/** 让 canvas 在面板里等比铺满，不留黑边 */
function fitCanvas() {
  const canvas = canvasRef.value
  const stage = stageRef.value
  if (!canvas || !stage || !canvas.width || !canvas.height) return
  const scale = Math.min(stage.clientWidth / canvas.width, stage.clientHeight / canvas.height)
  if (!Number.isFinite(scale) || scale <= 0) return
  canvas.style.width = `${Math.floor(canvas.width * scale)}px`
  canvas.style.height = `${Math.floor(canvas.height * scale)}px`
}

let resizeObs: ResizeObserver | null = null

// ---- 坐标与修饰键 ----

type ModLike = { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }
const MOD_NAMES = ['Alt', 'Control', 'Meta', 'Shift'] as const

function mods(e: ModLike): string[] {
  const out: string[] = []
  if (e.altKey) out.push('Alt')
  if (e.ctrlKey) out.push('Control')
  if (e.metaKey) out.push('Meta')
  if (e.shiftKey) out.push('Shift')
  return out
}

/**
 * 指针位置 → 画面内归一化坐标（0~1）。
 * 用 canvas 的**实际渲染矩形**换算：面板缩放、留白、等比铺满都不影响准确性。
 */
function norm(e: MouseEvent): { nx: number; ny: number } | null {
  const canvas = canvasRef.value
  if (!canvas) return null
  const r = canvas.getBoundingClientRect()
  if (r.width <= 0 || r.height <= 0) return null
  return { nx: (e.clientX - r.left) / r.width, ny: (e.clientY - r.top) / r.height }
}

// ---- 鼠标 ----

let pressed = false
let lastMoveAt = 0

function buttonName(b: number): string {
  if (b === 1) return 'middle'
  if (b === 2) return 'right'
  return 'left'
}

function sendMouse(event: string, e: MouseEvent, clickCount: number) {
  const p = norm(e)
  if (!p) return
  send({
    type: 'mouse', event, button: buttonName(e.button),
    nx: p.nx, ny: p.ny, clickCount, modifiers: mods(e),
  })
}

function onMouseDown(e: MouseEvent) {
  e.preventDefault()
  // 点画面即把键盘焦点交给承接层，之后打字直接进页面
  focusInput()
  const clickCount = e.detail >= 2 ? 2 : 1
  sendMouse('mousePressed', e, clickCount)
  // 单击：不立刻抬起 —— 让用户能按住拖动页面上的滑块/文本选区，
  // 真正的抬起由 onMouseUp / onMouseLeave 补发。
  // 双击：成对发送 pressed+released（页面靠 clickCount=2 识别 dblclick），
  // 否则第二次按下会与第一次的抬起错配。
  if (clickCount === 2) {
    sendMouse('mouseReleased', e, 2)
    pressed = false
    grabbing.value = false
    return
  }
  pressed = true
  grabbing.value = true
}

function onMouseUp(e: MouseEvent) {
  if (!pressed) return
  pressed = false
  grabbing.value = false
  sendMouse('mouseReleased', e, 1)
}

function onMouseLeave(e: MouseEvent) {
  // 指针离开画面时补发抬起，否则页面会一直以为键按着（拖拽/文本选择卡死）
  if (!pressed) return
  pressed = false
  grabbing.value = false
  sendMouse('mouseReleased', e, 1)
}

function onMouseMove(e: MouseEvent) {
  // 拖拽时全量转发（页面要收到完整轨迹），否则限速
  const now = performance.now()
  if (!pressed && now - lastMoveAt < 50) return
  lastMoveAt = now
  sendMouse('mouseMoved', e, 0)
}

function onWheel(e: WheelEvent) {
  const p = norm(e)
  if (!p) return
  send({
    type: 'wheel', nx: p.nx, ny: p.ny,
    deltaX: e.deltaX, deltaY: e.deltaY, modifiers: mods(e),
  })
}

// ---- 键盘 ----

const modHeld = new Set<string>()
const composing = ref(false)

// 后端映射表覆盖的键；其余（F1~F12、媒体键等）走 insertText 兜底
const NAMED_KEYS = new Set([
  'Enter', 'Tab', 'Backspace', 'Escape', 'Delete',
  'ArrowLeft', 'ArrowUp', 'ArrowRight', 'ArrowDown',
  'Home', 'End', 'PageUp', 'PageDown',
])

function forwardable(key: string): boolean {
  return key.length === 1 || NAMED_KEYS.has(key)
}

/** 修饰键按下/抬起必须单独上报，页面才能在按住期间识别组合键 */
function trackModifier(e: KeyboardEvent, dir: 'down' | 'up'): boolean {
  if (!(MOD_NAMES as readonly string[]).includes(e.key)) return false
  if (dir === 'down') modHeld.add(e.key)
  else modHeld.delete(e.key)
  send({ type: 'key', key: e.key, mod: dir, modifiers: mods(e) })
  return true
}

function onKeyDown(e: KeyboardEvent) {
  // Esc：交还键盘焦点给面板（否则页面吃掉 Esc 就没法退出输入）
  if (e.key === 'Escape' && modHeld.size === 0) {
    e.preventDefault()
    send({ type: 'key', key: 'Escape', mod: 'press', text: null, modifiers: [] })
    inputRef.value?.blur()
    return
  }
  if (trackModifier(e, 'down')) {
    e.preventDefault()
    return
  }
  // 组合期间交给输入法，不要抢键（否则拼音会被当成英文字符发出去）
  if (composing.value) return
  if (!forwardable(e.key)) return

  // 拦掉浏览器默认行为：Ctrl+S 存网页、空格滚面板、Backspace 后退等
  e.preventDefault()

  const withMod = e.ctrlKey || e.metaKey || e.altKey
  if (e.key.length === 1) {
    // 有 Ctrl/Meta/Alt 时是快捷键，不能补字符
    send({ type: 'key', key: e.key, mod: 'press', text: withMod ? null : e.key, modifiers: mods(e) })
    return
  }
  send({ type: 'key', key: e.key, mod: 'press', text: null, modifiers: mods(e) })
}

function onKeyUp(e: KeyboardEvent) {
  if (trackModifier(e, 'up')) e.preventDefault()
}

// ---- 文本输入（含中文输入法）----

function focusInput() {
  const el = inputRef.value
  if (!el) return
  el.focus({ preventScroll: true })
  focused.value = true
}

function onInputBlur() {
  focused.value = false
  // 失焦时补发修饰键抬起，否则页面以为 Ctrl 一直按着
  for (const k of modHeld) send({ type: 'key', key: k, mod: 'up' })
  modHeld.clear()
}

function onInput() {
  // 组合期间的中间态不发，否则拼音字母会一个个打进去
  if (composing.value) return
  flushText()
}

function onCompositionEnd() {
  composing.value = false
  flushText()
}

/** 取走承接层里的文本并清空，保证下一次输入的增量语义正确 */
function flushText() {
  const el = inputRef.value
  if (!el) return
  const v = el.value
  el.value = ''
  if (v) send({ type: 'text', text: v })
}

// ---- 动作 ----

function send(payload: Record<string, unknown>) {
  const sock = ws
  if (!sock || sock.readyState !== WebSocket.OPEN) return
  try { sock.send(JSON.stringify(payload)) } catch { /* ignore */ }
}

function switchTab(tabId: string) {
  if (!tabId || tabId === activeTabId.value) return
  activeTabId.value = tabId
  send({ type: 'switch_tab', tab_id: tabId })
}

function reload() {
  send({ type: 'reload' })
}

function navigate() {
  const u = urlInput.value.trim()
  if (!u) return
  // 手敲域名时省略协议是常态，补 https
  const url = /^https?:\/\//i.test(u) ? u : `https://${u}`
  urlInput.value = url
  send({ type: 'navigate', url })
}

function gotoInstall() {
  router.push({ path: '/admin/tools', query: { components: '1', key: 'browser_chromium' } })
}

// ---- 跟随 AI 操作的页面 ----

/**
 * 把面板切到 `url` 指向的标签。
 *
 * 匹配用 URL 前缀而不是全等：AI 报回的地址可能带 anchor 或额外 query，
 * 而标签页自己上报的地址未必一字不差（如末尾斜杠）。
 * 匹配不到时退回"包含主机名"的模糊匹配，再不行就保持当前标签不动 ——
 * 宁可停在原页面，也不要把用户切到一个空白页。
 */
function followAiPage(url: string, retry = true): void {
  const want = (url || '').trim()
  if (!want) return
  if (!tabs.value.length) {
    // 标签列表还没到：等 tabs 消息回来时补一次（见 onControl 的 pendingFollow）
    pendingFollow.value = want
    send({ type: 'tabs' })
    return
  }

  const strip = (u: string) => u.split('#')[0].replace(/\/+$/, '')
  const w = strip(want)

  let hit = tabs.value.find((t) => strip(t.url) === w)
  if (!hit) hit = tabs.value.find((t) => strip(t.url).startsWith(w) || w.startsWith(strip(t.url)))
  if (!hit) {
    try {
      const host = new URL(want).host
      hit = tabs.value.find((t) => {
        try { return new URL(t.url).host === host } catch { return false }
      })
    } catch { /* 非法 URL 直接放弃模糊匹配 */ }
  }
  if (hit && hit.id !== activeTabId.value) {
    switchTab(hit.id)
    return
  }
  if (!hit && retry) {
    // 有可能是 AI 刚点开的新标签，面板的列表还停在点击之前。
    // 刷新一次列表再匹配；已刷新过一次就不再重试，避免空转。
    pendingFollow.value = want
    send({ type: 'tabs' })
  }
}

watch(
  () => props.followSeq,
  () => {
    const url = props.followUrl
    if (!url) return
    // 标签列表可能还没到：拿到 tabs 后补一次对齐
    if (!tabs.value.length) {
      send({ type: 'tabs' })
      pendingFollow.value = url
      return
    }
    followAiPage(url)
  },
)

// ---- 生命周期 ----

function setupObservers() {
  if (resizeObs || !stageRef.value) return
  resizeObs = new ResizeObserver(() => fitCanvas())
  resizeObs.observe(stageRef.value)
  fitCanvas()
}

watch(() => props.open, (v) => {
  if (v) {
    nextTick(() => {
      setupObservers()
      connect()
    })
  } else {
    disconnect()
  }
})

// 必须显式在挂载时连一次：父组件用 v-if 渲染本组件，挂载瞬间 open 已经是 true，
// watch 收不到"变化"，结果面板一直显示"未连接"且永不重连。
onMounted(() => {
  if (!props.open) return
  nextTick(() => {
    setupObservers()
    connect()
  })
})

fpsTimer = setInterval(() => {
  fps.value = frameCount
  frameCount = 0
}, 1000)

onBeforeUnmount(() => {
  disconnect()
  if (fpsTimer) {
    clearInterval(fpsTimer)
    fpsTimer = null
  }
  resizeObs?.disconnect()
  resizeObs = null
})

defineExpose({ reconnect })
</script>

<style scoped>
.blp {
  display: flex; flex-direction: column; height: 100%; min-height: 0;
  background: var(--bg-surface); border: 1px solid var(--border);
  border-radius: 10px; overflow: hidden;
}

/* 顶栏 */
.blp-bar {
  display: flex; align-items: center; gap: 4px;
  padding: 6px 8px; border-bottom: 1px solid var(--border-subtle); flex-shrink: 0;
}
.blp-tab {
  display: flex; align-items: center; gap: 6px; min-width: 0; max-width: 240px;
  padding: 4px 8px; border: 1px solid var(--border-subtle); border-radius: 7px;
  background: var(--bg-subtle); color: var(--text-body); font: inherit; font-size: 12px;
  cursor: pointer;
}
.blp-tab:hover { border-color: var(--border); }
.blp-tab-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.blp-caret { font-size: 10px; opacity: 0.6; flex-shrink: 0; }
.blp-spacer { flex: 1; }
.blp-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; border: none; border-radius: 6px;
  background: transparent; color: var(--text-secondary); cursor: pointer; font-size: 14px;
}
.blp-icon:hover:not(:disabled) { background: var(--bg-muted); color: var(--color-primary); }
.blp-icon:disabled { opacity: 0.4; cursor: not-allowed; }

.blp-dd-item { max-width: 320px; }
.blp-dd-title { font-size: 13px; color: var(--text-body); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.blp-dd-url { font-size: 11px; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 地址栏 */
.blp-url {
  display: flex; align-items: center; gap: 6px; padding: 6px 10px;
  border-bottom: 1px solid var(--border-subtle); flex-shrink: 0;
}
.blp-url-ic { font-size: 13px; color: var(--text-muted); cursor: pointer; flex-shrink: 0; }
.blp-url-ic:hover { color: var(--color-primary); }
.blp-url-input {
  flex: 1; min-width: 0; border: none; outline: none; background: transparent;
  font-size: 12px; color: var(--text-body);
  font-family: var(--el-font-family-mono, monospace);
}

/* 画面 */
.blp-stage {
  position: relative; flex: 1; min-height: 0;
  display: flex; align-items: center; justify-content: center;
  /* 页面未铺满时露出的格子底，避免误以为画面被裁切 */
  background: repeating-conic-gradient(#f5f6f8 0% 25%, #fff 0% 50%) 50% / 16px 16px;
  overflow: hidden;
}
.blp-canvas { display: block; cursor: default; }
.blp-canvas.grabbing { cursor: grabbing; }

/* 输入承接层：铺满画面区，但不吃鼠标事件 */
.blp-input {
  position: absolute; inset: 0; width: 100%; height: 100%;
  margin: 0; padding: 0; border: none; outline: none; resize: none;
  opacity: 0; pointer-events: none; cursor: default;
  /* 防止 iOS/Safari 自动放大（桌面端无感，但不会有害） */
  font-size: 16px;
}

.blp-overlay {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 10px;
  background: rgba(255, 255, 255, 0.88); backdrop-filter: blur(2px);
  font-size: 13px; color: var(--text-secondary); padding: 16px; text-align: center;
}
.blp-ov-actions { display: flex; gap: 8px; }

.blp-typing {
  position: absolute; left: 8px; bottom: 8px;
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 8px; border-radius: 999px; font-size: 11px;
  background: rgba(37, 99, 235, 0.9); color: #fff; pointer-events: none;
}

/* 底栏 */
.blp-foot {
  display: flex; align-items: center; gap: 6px; padding: 5px 10px;
  border-top: 1px solid var(--border-subtle); font-size: 11px;
  color: var(--text-muted); flex-shrink: 0;
}
.blp-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.blp-dot.on { background: var(--el-color-success); }
.blp-dot.off { background: var(--el-color-info-light-5); }
.blp-fps { font-family: var(--el-font-family-mono, monospace); }
.blp-hint { opacity: 0.75; }
</style>
