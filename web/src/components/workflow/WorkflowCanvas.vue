<template>
  <div
    ref="wrapRef"
    class="wf-canvas"
    @mousedown="onPaneDown"
    @wheel.prevent="onWheel"
  >
    <div class="wf-viewport" :style="viewportStyle">
      <div class="wf-layer" :style="{ width: size.w + 'px', height: size.h + 'px' }">
        <!-- 连线层 -->
        <svg class="wf-svg" :width="size.w" :height="size.h">
          <defs>
            <marker id="wf-arrow" markerWidth="12" markerHeight="12" refX="10" refY="4" orient="auto">
              <path d="M0,0 L10,4 L0,8 z" fill="#b1b1b7" />
            </marker>
          </defs>
          <g v-for="e in edges" :key="e.id">
            <path
              :d="edgePath(e)"
              class="wf-edge-hit"
              @mousedown.stop
              @click.stop="selectEdge(e.id)"
            />
            <path
              :d="edgePath(e)"
              class="wf-edge"
              :class="{ on: selEdge === e.id, taken: isTaken(e), active: isActive(e) }"
              marker-end="url(#wf-arrow)"
            />
            <text v-if="e.sourceHandle" class="wf-edge-label" :x="mid(e).x" :y="mid(e).y - 6" text-anchor="middle">
              {{ e.sourceHandle }}
            </text>
          </g>
          <path v-if="connecting" :d="tempPath" class="wf-edge temp" />
        </svg>

        <!-- 连线删除按钮 -->
        <button
          v-for="e in edges"
          :key="'del-' + e.id"
          class="wf-edge-del"
          :class="{ on: selEdge === e.id }"
          :style="{ left: mid(e).x + 'px', top: mid(e).y + 'px' }"
          title="删除连线"
          @mousedown.stop
          @click.stop="removeEdge(e.id)"
        >×</button>

        <!-- 节点层 -->
        <div
          v-for="n in nodes"
          :key="n.id"
          class="wf-node"
          :class="{ sel: selectedId === n.id }"
          :style="nodeStyle(n)"
          @mousedown.stop="startDrag($event, n)"
          @click.stop="selectNode(n.id)"
        >
          <div class="wf-node-id">{{ n.id }}</div>
          <div class="wf-node-label">{{ meta(n.type).icon }} {{ meta(n.type).label }}</div>

          <!-- 运行时进度：已执行节点显示耗时，当前节点显示旋转环 -->
          <span
            v-if="status?.[n.id] === 'running'"
            class="wf-node-spinner"
            :style="{ borderTopColor: meta(n.type).color }"
          />
          <span v-if="latencyOf(n.id)" class="wf-node-latency">{{ latencyOf(n.id) }}</span>

          <!-- 输入端口 -->
          <span v-if="n.type !== 'start'" class="wf-port in" :data-inport="n.id" @mousedown.stop />

          <!-- 输出端口 -->
          <span
            v-for="h in outHandles(n)"
            :key="'out-' + h"
            class="wf-port out"
            :style="outPortStyle(n, h)"
            :title="outHandles(n).length > 1 ? `出口：${h}` : '拖动可连线'"
            @mousedown.stop="startConnect($event, n, h)"
          >
            <span v-if="outHandles(n).length > 1" class="wf-port-tag">{{ h }}</span>
          </span>

          <button
            v-if="selectedId === n.id && n.type !== 'start' && n.type !== 'end'"
            class="wf-node-del"
            title="删除节点"
            @mousedown.stop
            @click.stop="removeNode(n.id)"
          >×</button>
        </div>
      </div>
    </div>

    <div class="wf-tools">
      <el-tooltip content="适应视图" placement="top">
        <el-button size="small" :icon="FullScreen" @click="fit" />
      </el-tooltip>
      <span class="wf-zoom">{{ Math.round(zoom * 100) }}%</span>
      <el-tooltip content="拖拽节点移动 · 拖动右侧圆点连线 · 点击连线选中后删除" placement="top">
        <el-icon class="wf-help"><QuestionFilled /></el-icon>
      </el-tooltip>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { FullScreen, QuestionFilled } from '@element-plus/icons-vue'
import { NODE_META, STATUS_COLOR, defaultData, type NodeMeta } from './meta'

const NODE_W = 170
const NODE_H = 64

interface GNode { id: string; type: string; data: any; position: { x: number; y: number } }
interface GEdge { id: string; source: string; target: string; sourceHandle?: string }

const props = withDefaults(
  defineProps<{
    graph: any
    selectedId?: string | null
    status?: Record<string, string>
    /** 已走过的边 id 集合（边 id 与 edges 里的 id 一致），用于高亮执行路径 */
    takenEdges?: string[]
    /** 节点耗时（ms），画在节点右下角 */
    latency?: Record<string, number>
  }>(),
  { selectedId: null, status: () => ({}), takenEdges: () => [], latency: () => ({}) },
)
const emit = defineEmits<{ change: [graph: any]; select: [id: string | null] }>()

const wrapRef = ref<HTMLElement>()
const nodes = ref<GNode[]>([])
const edges = ref<GEdge[]>([])
const selEdge = ref<string | null>(null)
const zoom = ref(1)
const panX = ref(0)
const panY = ref(0)
const connecting = ref<{ source: string; handle: string; x: number; y: number } | null>(null)

let lastEmit = ''
let seq = 0

function meta(type: string): NodeMeta {
  return NODE_META[type] || { label: type || '节点', color: '#999', icon: '?' }
}

function buildNodes(graph: any): GNode[] {
  return (graph?.nodes || []).map((n: any, i: number) => ({
    id: String(n.id),
    type: n.type || 'llm',
    data: n.data || {},
    position: n.position && typeof n.position.x === 'number'
      ? { x: n.position.x, y: n.position.y }
      : { x: 60 + (i % 5) * 220, y: 60 + Math.floor(i / 5) * 130 },
  }))
}

function buildEdges(graph: any): GEdge[] {
  return (graph?.edges || []).map((e: any, i: number) => ({
    id: String(e.id || `e-${e.source}-${e.target}-${i}`),
    source: String(e.source),
    target: String(e.target),
    sourceHandle: e.sourceHandle || undefined,
  }))
}

function graphOf() {
  return {
    ...(props.graph || {}),
    nodes: nodes.value.map((n) => ({ id: n.id, type: n.type, position: { ...n.position }, data: n.data })),
    edges: edges.value.map((e) => ({ id: e.id, source: e.source, target: e.target, sourceHandle: e.sourceHandle || undefined })),
  }
}

function emitGraph() {
  const g = graphOf()
  lastEmit = sigOf(g)
  emit('change', g)
}

function sigOf(graph: any): string {
  return JSON.stringify([
    (graph?.nodes || []).map((n: any) => [n.id, n.type, n.position?.x, n.position?.y, n.data]),
    (graph?.edges || []).map((e: any) => [e.source, e.target, e.sourceHandle]),
  ])
}

function syncFromProps() {
  nodes.value = buildNodes(props.graph)
  edges.value = buildEdges(props.graph)
  if (props.selectedId && !nodes.value.some((n) => n.id === props.selectedId)) emit('select', null)
}

// 仅当外部图与本地上一帧不一致时重同步（自己的回显跳过）
watch(
  () => sigOf(props.graph),
  (s) => {
    if (s === sigOf(graphOf())) return
    if (s === lastEmit) return
    syncFromProps()
  },
)
onMounted(syncFromProps)

// ---------- 几何 ----------
const size = computed(() => {
  let w = 800, h = 560
  for (const n of nodes.value) {
    w = Math.max(w, n.position.x + NODE_W + 120)
    h = Math.max(h, n.position.y + NODE_H + 120)
  }
  return { w, h }
})

function nodeStyle(n: GNode) {
  const st = props.status?.[n.id]
  return {
    left: n.position.x + 'px',
    top: n.position.y + 'px',
    width: NODE_W + 'px',
    height: NODE_H + 'px',
    borderColor: meta(n.type).color,
    boxShadow: st ? `0 0 0 3px ${STATUS_COLOR[st] || '#d9d9d9'}` : undefined,
  }
}

// 出口 handle：条件/多路分支/分类路由多出口，其余单出口（空 handle）
function outHandles(n: GNode): string[] {
  if (n.type === 'end') return []
  if (n.type === 'condition') return ['true', 'false']
  if (n.type === 'switch') {
    const cases = (n.data?.cases || []).map((c: any) => c.handle).filter(Boolean)
    return [...cases, 'default']
  }
  if (n.type === 'router') {
    const cases = (n.data?.cases || []).map((c: any) => c.handle).filter(Boolean)
    return [...cases, 'default']
  }
  return ['']
}

function outPortTop(n: GNode, handle: string): number {
  const hs = outHandles(n)
  const i = Math.max(0, hs.indexOf(handle))
  return (NODE_H * (i + 1)) / (hs.length + 1)
}

function outPortStyle(n: GNode, handle: string) {
  return { left: NODE_W - 5 + 'px', top: outPortTop(n, handle) - 5 + 'px' }
}

function nodeById(id: string): GNode | undefined {
  return nodes.value.find((x) => x.id === id)
}

// ---------- 执行路径高亮 ----------
// takenEdges 是已执行完的边；active 边 = 源节点已完成且目标节点正在运行。
function isTaken(e: GEdge): boolean {
  return props.takenEdges.includes(e.id)
}
function isActive(e: GEdge): boolean {
  if (isTaken(e)) return false
  const src = props.status?.[e.source]
  const tgt = props.status?.[e.target]
  return src === 'success' && (tgt === 'running' || tgt === undefined)
}
function latencyOf(id: string): string {
  const v = props.latency?.[id]
  if (!v) return ''
  return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`
}

function edgeEnds(e: GEdge) {
  const s = nodeById(e.source)
  const t = nodeById(e.target)
  if (!s || !t) return null
  const hs = outHandles(s)
  const handle = e.sourceHandle && hs.includes(e.sourceHandle) ? e.sourceHandle : hs[0] || ''
  return {
    x1: s.position.x + NODE_W,
    y1: s.position.y + outPortTop(s, handle),
    x2: t.position.x,
    y2: t.position.y + NODE_H / 2,
  }
}

function edgePath(e: GEdge): string {
  const p = edgeEnds(e)
  if (!p) return ''
  const cx = (p.x1 + p.x2) / 2
  return `M ${p.x1} ${p.y1} C ${cx} ${p.y1}, ${cx} ${p.y2}, ${p.x2} ${p.y2}`
}

function mid(e: GEdge) {
  const p = edgeEnds(e)
  if (!p) return { x: 0, y: 0 }
  return { x: (p.x1 + p.x2) / 2, y: (p.y1 + p.y2) / 2 }
}

const tempPath = computed(() => {
  const c = connecting.value
  if (!c) return ''
  const s = nodeById(c.source)
  if (!s) return ''
  const x1 = s.position.x + NODE_W
  const y1 = s.position.y + outPortTop(s, c.handle)
  const cx = (x1 + c.x) / 2
  return `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${c.y}, ${c.x} ${c.y}`
})

const viewportStyle = computed(() => ({
  transform: `translate(${panX.value}px, ${panY.value}px) scale(${zoom.value})`,
}))

// ---------- 坐标换算 ----------
function toCanvas(clientX: number, clientY: number) {
  const el = wrapRef.value
  if (!el) return { x: 0, y: 0 }
  const r = el.getBoundingClientRect()
  return {
    x: (clientX - r.left - r.width / 2 - panX.value) / zoom.value + r.width / 2,
    y: (clientY - r.top - r.height / 2 - panY.value) / zoom.value + r.height / 2,
  }
}

// ---------- 平移 / 缩放 ----------
function onPaneDown(e: MouseEvent) {
  if (e.button !== 0) return
  const t = e.target as HTMLElement
  if (t.closest('.wf-node') || t.closest('.wf-edge-del')) return
  if (t.closest('.wf-edge-hit')) return
  clearSel()
  const sx = e.clientX, sy = e.clientY
  const px = panX.value, py = panY.value
  const move = (ev: MouseEvent) => {
    panX.value = px + (ev.clientX - sx)
    panY.value = py + (ev.clientY - sy)
  }
  const up = () => {
    window.removeEventListener('mousemove', move)
    window.removeEventListener('mouseup', up)
  }
  window.addEventListener('mousemove', move)
  window.addEventListener('mouseup', up)
}

function onWheel(e: WheelEvent) {
  const next = zoom.value + (e.deltaY < 0 ? 0.1 : -0.1)
  zoom.value = Math.min(2, Math.max(0.3, Number(next.toFixed(2))))
}

function fit() {
  panX.value = 0
  panY.value = 0
  zoom.value = 1
}

// ---------- 选择 ----------
function clearSel() {
  selEdge.value = null
  if (props.selectedId) emit('select', null)
}
function selectNode(id: string) {
  selEdge.value = null
  emit('select', id)
}
function selectEdge(id: string) {
  selEdge.value = id
  emit('select', null)
}

// ---------- 拖拽节点 ----------
function startDrag(e: MouseEvent, n: GNode) {
  const t = e.target as HTMLElement
  if (t.closest('.wf-port') || t.closest('.wf-node-del')) return
  selectNode(n.id)
  const sx = e.clientX, sy = e.clientY
  const ox = n.position.x, oy = n.position.y
  let moved = false
  const move = (ev: MouseEvent) => {
    const dx = (ev.clientX - sx) / zoom.value
    const dy = (ev.clientY - sy) / zoom.value
    if (Math.abs(dx) > 1 || Math.abs(dy) > 1) moved = true
    n.position.x = Math.max(0, Math.round(ox + dx))
    n.position.y = Math.max(0, Math.round(oy + dy))
  }
  const up = () => {
    window.removeEventListener('mousemove', move)
    window.removeEventListener('mouseup', up)
    if (moved) emitGraph()
  }
  window.addEventListener('mousemove', move)
  window.addEventListener('mouseup', up)
}

// ---------- 连线 ----------
function startConnect(e: MouseEvent, n: GNode, handle: string) {
  const start = toCanvas(e.clientX, e.clientY)
  connecting.value = { source: n.id, handle, x: start.x, y: start.y }
  const move = (ev: MouseEvent) => {
    if (!connecting.value) return
    const p = toCanvas(ev.clientX, ev.clientY)
    connecting.value.x = p.x
    connecting.value.y = p.y
  }
  const up = (ev: MouseEvent) => {
    window.removeEventListener('mousemove', move)
    window.removeEventListener('mouseup', up)
    const c = connecting.value
    connecting.value = null
    if (!c) return
    const el = (ev.target as HTMLElement)?.closest?.('[data-inport]') as HTMLElement | null
    const target = el?.getAttribute('data-inport') || ''
    if (!target || target === c.source) return
    if (edges.value.some((x) => x.source === c.source && x.target === target && (x.sourceHandle || '') === (c.handle || ''))) {
      ElMessage.info('该连线已存在')
      return
    }
    seq += 1
    edges.value = [...edges.value, { id: `e-${c.source}-${c.handle || 'o'}-${target}-${seq}`, source: c.source, target, sourceHandle: c.handle || undefined }]
    emitGraph()
  }
  window.addEventListener('mousemove', move)
  window.addEventListener('mouseup', up)
}

// ---------- 增删 ----------
function removeEdge(id: string) {
  if (selEdge.value === id) selEdge.value = null
  edges.value = edges.value.filter((e) => e.id !== id)
  emitGraph()
}

function removeNode(id: string) {
  const n = nodeById(id)
  if (n && (n.type === 'start' || n.type === 'end')) {
    ElMessage.warning('起止节点不可删除')
    return
  }
  nodes.value = nodes.value.filter((x) => x.id !== id)
  edges.value = edges.value.filter((e) => e.source !== id && e.target !== id)
  if (props.selectedId === id) emit('select', null)
  emitGraph()
}

/** 供父组件调用：新增节点 */
function addNode(type: string) {
  const used = new Set(nodes.value.map((n) => n.id))
  let n = 1
  let id = type === 'start' ? 'start' : type === 'end' ? 'end' : `${type}_${n}`
  while (used.has(id)) { n += 1; id = `${type}_${n}` }
  const count = nodes.value.length
  nodes.value = [...nodes.value, {
    id,
    type,
    data: defaultData(type),
    position: { x: 60 + (count % 5) * 220, y: 60 + Math.floor(count / 5) * 130 },
  }]
  emitGraph()
  emit('select', id)
}

/** 供父组件调用：更新选中节点（改名/改配置） */
function updateNode(oldId: string, newId: string, data: any) {
  nodes.value = nodes.value.map((n) => (n.id === oldId ? { ...n, id: newId, data } : n))
  edges.value = edges.value.map((e) => ({
    ...e,
    source: e.source === oldId ? newId : e.source,
    target: e.target === oldId ? newId : e.target,
  }))
  emitGraph()
  emit('select', newId)
}

/** 供父组件调用：删除节点 */
function deleteNode(id: string) {
  removeNode(id)
}

defineExpose({ addNode, updateNode, deleteNode, fit, removeNode })
</script>

<style scoped>
.wf-canvas {
  position: relative;
  flex: 1;
  min-height: 320px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 8px;
  background:
    radial-gradient(circle, var(--border) 1px, transparent 1px) 0 0 / 18px 18px,
    var(--bg-subtle);
  cursor: grab;
}
.wf-canvas:active { cursor: grabbing; }
.wf-viewport { position: absolute; inset: 0; transform-origin: center center; }
.wf-layer { position: absolute; left: 0; top: 0; }
.wf-svg { position: absolute; left: 0; top: 0; overflow: visible; pointer-events: none; }

.wf-edge { fill: none; stroke: #b1b1b7; stroke-width: 2; transition: stroke 0.2s; }
.wf-edge.on { stroke: var(--color-primary); stroke-width: 3; }
/* 已执行的路径：加粗高亮 */
.wf-edge.taken { stroke: #52c41a; stroke-width: 3.5; }
/* 正在推进的边：流动虚线动画 */
.wf-edge.active {
  stroke: #1677ff;
  stroke-width: 3;
  stroke-dasharray: 8 6;
  animation: wf-flow 0.7s linear infinite;
}
@keyframes wf-flow { to { stroke-dashoffset: -14; } }
.wf-edge.temp { stroke: var(--color-primary); stroke-dasharray: 5 4; }
.wf-edge-hit { fill: none; stroke: transparent; stroke-width: 14; pointer-events: stroke; cursor: pointer; }
.wf-edge-label { font-size: 11px; fill: var(--text-secondary); }

.wf-edge-del {
  position: absolute; transform: translate(-50%, -50%);
  width: 18px; height: 18px; padding: 0; border: none; border-radius: 50%;
  background: #ff4d4f; color: #fff; font-size: 13px; line-height: 1; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  opacity: 0; transition: opacity 0.15s; box-shadow: 0 1px 4px rgba(0, 0, 0, 0.25); z-index: 5;
}
.wf-edge-del.on { opacity: 1; }
.wf-layer:hover .wf-edge-del { opacity: 0.55; }
.wf-layer:hover .wf-edge-del:hover { opacity: 1; background: #cf1322; }

.wf-node {
  position: absolute; box-sizing: border-box;
  background: #fff; border: 2px solid #999; border-radius: 8px;
  padding: 6px 10px; display: flex; flex-direction: column; justify-content: center;
  cursor: move; user-select: none;
}
.wf-node.sel { box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.25); }
/* 运行中的节点：脉冲光圈 */
.wf-node:has(.wf-node-spinner) { animation: wf-pulse 1.4s ease-in-out infinite; }
@keyframes wf-pulse {
  0%, 100% { box-shadow: 0 0 0 3px rgba(22, 119, 255, 0.22); }
  50% { box-shadow: 0 0 0 8px rgba(22, 119, 255, 0.06); }
}
.wf-node-spinner {
  position: absolute; right: 6px; top: 6px;
  width: 11px; height: 11px; border-radius: 50%;
  border: 2px solid var(--border); border-top-color: var(--color-primary);
  animation: wf-spin 0.7s linear infinite;
}
@keyframes wf-spin { to { transform: rotate(360deg); } }
.wf-node-latency {
  position: absolute; right: 6px; bottom: 3px;
  font-size: 10px; color: var(--text-muted); font-variant-numeric: tabular-nums;
}
.wf-node-id { font-size: 12px; font-weight: 600; color: var(--text-title); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.wf-node-label { font-size: 11px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.wf-node-del {
  position: absolute; right: -9px; top: -9px; width: 18px; height: 18px; padding: 0;
  border: none; border-radius: 50%; background: #ff4d4f; color: #fff; font-size: 13px;
  line-height: 1; cursor: pointer; display: flex; align-items: center; justify-content: center;
}

.wf-port {
  position: absolute; width: 10px; height: 10px; border-radius: 50%;
  background: #fff; border: 2px solid var(--color-primary); z-index: 3;
}
.wf-port.in { left: -5px; top: calc(50% - 5px); }
.wf-port.out { cursor: crosshair; }
.wf-port.out:hover { background: var(--color-primary); }
.wf-port-tag {
  position: absolute; left: 12px; top: -4px; font-size: 10px; color: var(--text-muted); white-space: nowrap;
}

.wf-tools {
  position: absolute; right: 10px; bottom: 10px; z-index: 10;
  display: flex; align-items: center; gap: 8px;
  background: var(--bg-surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 4px 10px; box-shadow: var(--shadow-card);
}
.wf-zoom { font-size: 12px; color: var(--text-secondary); }
.wf-help { color: var(--text-muted); cursor: help; }
</style>
