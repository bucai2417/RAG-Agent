<template>
  <PageContainer :title="`工作流编排：${agent?.name || ''}`" subtitle="拖拽搭建 DAG：节点 → 连线 → 属性配置 → 保存 / 发布 / 运行">
    <template #extra>
      <el-button :icon="ArrowLeft" @click="$router.push('/agents')">返回</el-button>
      <el-tag>v{{ version }}</el-tag>
      <el-button :icon="Document" @click="toggleJson">{{ showJson ? '返回画布' : 'JSON 视图' }}</el-button>
      <el-button type="warning" plain :icon="MagicStick" :disabled="!canEdit" @click="openAi">AI 生成</el-button>
      <el-button :icon="Connection" @click="integrationOpen = true">集成与测试</el-button>
      <el-button :icon="Files" @click="templateOpen = true">模板</el-button>
      <el-button :icon="Star" :disabled="!canEdit" @click="saveAsTemplate">存为模板</el-button>
      <el-button :icon="DocumentAdd" :disabled="!canEdit" @click="save">保存</el-button>
      <el-button type="primary" :icon="Upload" :disabled="!canEdit" @click="publish">发布</el-button>
      <el-button type="success" :icon="VideoPlay" @click="openRunPanel">运行</el-button>
    </template>

    <div class="wf-layout">
      <!-- 左：节点面板（分组 + 搜索） -->
      <aside class="app-card wf-palette">
        <div class="wf-palette-title">
          <span>节点</span>
          <el-input
            v-model="nodeFilter"
            size="small"
            placeholder="搜索节点"
            clearable
            :prefix-icon="Search"
            class="wf-palette-search"
          />
        </div>
        <el-scrollbar class="wf-palette-scroll">
          <div v-for="g in visibleGroups" :key="g.key" class="wf-group">
            <div class="wf-group-head" @click="toggleGroup(g.key)">
              <el-icon class="wf-group-arrow" :class="{ open: !collapsed.has(g.key) }"><ArrowRight /></el-icon>
              <span>{{ g.label }}</span>
              <span class="wf-group-count">{{ g.items.length }}</span>
            </div>
            <div v-show="!collapsed.has(g.key)" class="wf-group-body">
              <button
                v-for="it in g.items"
                :key="it.type"
                class="wf-palette-btn"
                :style="{ borderColor: it.meta.color, color: it.meta.color }"
                :title="it.meta.hint || it.meta.label"
                @click="addNode(it.type)"
              >
                <span class="wf-palette-label">{{ it.meta.icon }} {{ it.meta.label }}</span>
                <el-icon><Plus /></el-icon>
              </button>
            </div>
          </div>
          <el-empty v-if="!visibleGroups.length" description="无匹配节点" :image-size="50" />
        </el-scrollbar>
      </aside>

      <!-- 中：画布 / JSON -->
      <section class="wf-center">
        <div v-if="showJson" class="app-card wf-json">
          <el-input v-model="jsonText" type="textarea" class="wf-json-area" />
          <div class="wf-json-actions">
            <el-button @click="showJson = false">取消</el-button>
            <el-button type="primary" @click="applyJson">应用 JSON</el-button>
          </div>
        </div>
        <WorkflowCanvas
          v-else
          ref="canvasRef"
          :graph="graph"
          :selected-id="selectedId"
          :status="nodeStatus"
          :taken-edges="takenEdges"
          :latency="latencyMap"
          @change="onGraphChange"
          @select="selectedId = $event"
        />
      </section>

      <!-- 右：属性面板 -->
      <aside class="app-card wf-props">
        <div class="wf-props-title">节点配置</div>
        <el-scrollbar class="wf-props-scroll">
          <PropertyPanel
            :node="selectedNode"
            :all-node-ids="allNodeIds"
            @change="onNodeChange"
            @delete="onNodeDelete"
          />
        </el-scrollbar>
      </aside>
    </div>

    <WorkflowRunPanel
      v-model="runPanelOpen"
      :agent-id="agentId"
      :graph="graph"
      @node-status="(m: any) => (nodeStatus = m)"
      @trace="onTrace"
      @finish="() => {}"
    />

    <WorkflowIntegrationDrawer
      v-model="integrationOpen"
      :agent-id="agentId"
      :can-edit="canEdit"
      @reload="load"
    />

    <WorkflowTemplateDrawer v-model="templateOpen" :can-edit="canEdit" />

    <!-- AI 生成工作流 -->
    <el-dialog v-model="aiOpen" title="AI 生成工作流" width="620px">
      <p class="text-secondary" style="font-size: 12.5px; margin: 0 0 10px">
        用一句话描述你的业务流程，AI 会生成节点与连线（布局自动排布）。生成后可继续手工调整。
      </p>
      <el-input
        v-model="aiPrompt"
        type="textarea"
        :rows="4"
        placeholder="例如：接收客户消息 → 判断是售后还是咨询 → 售后走知识库检索后生成回复并推送企业微信；咨询直接由大模型回答"
      />
      <div class="ai-examples">
        <span class="text-muted">示例：</span>
        <el-tag
          v-for="ex in AI_EXAMPLES"
          :key="ex.label"
          size="small"
          class="ai-example"
          @click="aiPrompt = ex.prompt"
        >{{ ex.label }}</el-tag>
      </div>
      <el-alert v-if="aiWarnings.length" type="warning" :closable="false" show-icon style="margin-top: 10px">
        <template #title>生成时自动修正了 {{ aiWarnings.length }} 处</template>
        <div v-for="(w, i) in aiWarnings.slice(0, 5)" :key="i" class="ai-warn">{{ w }}</div>
      </el-alert>
      <template #footer>
        <el-button @click="aiOpen = false">取消</el-button>
        <el-button type="primary" :icon="MagicStick" :loading="aiBusy" @click="doAiGenerate">
          {{ aiBusy ? '生成中…' : '生成' }}
        </el-button>
      </template>
    </el-dialog>
  </PageContainer>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft, ArrowRight, Upload, VideoPlay, Plus, Document, DocumentAdd,
  Search, MagicStick, Connection, Files, Star,
} from '@element-plus/icons-vue'
import PageContainer from '@/components/PageContainer.vue'
import WorkflowCanvas from '@/components/workflow/WorkflowCanvas.vue'
import PropertyPanel from '@/components/workflow/PropertyPanel.vue'
import WorkflowRunPanel from '@/components/workflow/WorkflowRunPanel.vue'
import WorkflowIntegrationDrawer from '@/components/workflow/WorkflowIntegrationDrawer.vue'
import WorkflowTemplateDrawer from '@/components/workflow/WorkflowTemplateDrawer.vue'
import { groupNodes } from '@/components/workflow/meta'
import { agentApi, workflowApi, type Agent } from '@/api'
import { errMsg } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const SAMPLE_GRAPH = {
  nodes: [
    { id: 'start', type: 'start', position: { x: 60, y: 120 }, data: { inputs: [{ name: 'query', type: 'string', required: true }] } },
    { id: 'ret', type: 'knowledge_retrieval', position: { x: 300, y: 120 }, data: { query: '{{start.query}}', top_k: 5 } },
    { id: 'llm', type: 'llm', position: { x: 540, y: 120 }, data: { system: '你是企业助手，依据资料回答。', prompt: '资料：\n{{ret.output}}\n\n问题：{{start.query}}' } },
    { id: 'end', type: 'end', position: { x: 780, y: 120 }, data: { output: '{{llm.output}}' } },
  ],
  edges: [
    { id: 'e1', source: 'start', target: 'ret' },
    { id: 'e2', source: 'ret', target: 'llm' },
    { id: 'e3', source: 'llm', target: 'end' },
  ],
}

const AI_EXAMPLES = [
  { label: '电商订单处理', prompt: '接收订单号 → 调用 HTTP 查询订单详情 → 用表格节点筛选未付款订单并汇总金额 → 大模型生成催付话术 → 推送到企业微信群' },
  { label: '客服分流', prompt: '接收客户消息 → 用摘要节点判断是售后还是咨询 → 售后走知识库检索并生成回复；咨询直接由大模型回答 → 统一推送到企业微信' },
  { label: '日报生成', prompt: '开始 → 知识库检索本周工作记录 → 大模型总结成日报 → 文本模板排版 → 发送邮件给主管' },
]

const route = useRoute()
const agentId = Number(route.params.id)

const auth = useAuthStore()
const canEdit = computed(() => auth.hasPermission('workflow:edit') || auth.hasPermission('workflow:manage'))

const agent = ref<Agent | null>(null)
const version = ref(1)
// 乐观锁版本：load 时记下，每次保存成功后用返回值刷新，
// 这样「A 打开 → B 保存 → A 保存」会 409 提示，而不是把 B 的改动覆盖掉
const rowVersion = ref<number | undefined>(undefined)
const graph = ref<any>({ nodes: [], edges: [] })
const selectedId = ref<string | null>(null)
const nodeStatus = ref<Record<string, string>>({})
/** 执行路径高亮与节点耗时（由运行面板推送，画布渲染） */
const takenEdges = ref<string[]>([])
const latencyMap = ref<Record<string, number>>({})
const runPanelOpen = ref(route.query.panel === 'run')
const showJson = ref(false)
const jsonText = ref('')
const canvasRef = ref<any>(null)

const nodeFilter = ref('')
const collapsed = ref<Set<string>>(new Set())

const aiOpen = ref(false)
const aiPrompt = ref('')
const aiBusy = ref(false)
const aiWarnings = ref<string[]>([])
const integrationOpen = ref(false)
const templateOpen = ref(false)

async function saveAsTemplate() {
  if (!(graph.value.nodes || []).length) {
    ElMessage.warning('工作流为空，无法保存为模板')
    return
  }
  let name = agent.value?.name || '我的工作流'
  try {
    const r = await ElMessageBox.prompt('模板名称', '保存为模板', {
      inputValue: name,
      confirmButtonText: '保存',
      cancelButtonText: '取消',
    })
    name = (r.value || name).trim() || name
  } catch {
    return
  }
  try {
    // 先保存当前图，避免模板存的是旧版本
    rowVersion.value = (await workflowApi.save(agentId, graph.value, rowVersion.value))?.row_version ?? rowVersion.value
    const r = await workflowApi.saveAsTemplate(agentId, { name, category: '自定义' })
    ElMessage.success(`已保存模板「${r.name}」（${r.node_count} 个节点）`)
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

const allNodeIds = computed<string[]>(() => (graph.value.nodes || []).map((n: any) => n.id))
const selectedNode = computed(() => {
  if (!selectedId.value) return null
  const n = (graph.value.nodes || []).find((x: any) => x.id === selectedId.value)
  return n ? { id: n.id, type: n.type, data: n.data } : null
})

const visibleGroups = computed(() => {
  const kw = nodeFilter.value.trim().toLowerCase()
  const groups = groupNodes()
  if (!kw) return groups
  return groups
    .map((g) => ({
      ...g,
      items: g.items.filter(
        (it) => it.meta.label.toLowerCase().includes(kw)
          || it.type.toLowerCase().includes(kw)
          || (it.meta.hint || '').toLowerCase().includes(kw),
      ),
    }))
    .filter((g) => g.items.length)
})

function toggleGroup(key: string) {
  const next = new Set(collapsed.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsed.value = next
}

async function load() {
  try {
    agent.value = await agentApi.get(agentId)
    const wf = await workflowApi.get(agentId)
    version.value = wf.version
    rowVersion.value = wf.row_version
    const hasNodes = wf.graph?.nodes?.length
    graph.value = hasNodes ? wf.graph : SAMPLE_GRAPH
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

// 画布 / 属性面板改动统一回写到 graph
function onGraphChange(g: any) {
  graph.value = g
}
function onNodeChange(oldId: string, newId: string, data: any) {
  canvasRef.value?.updateNode(oldId, newId, data)
}
function onNodeDelete(id: string) {
  canvasRef.value?.deleteNode(id)
}
function addNode(type: string) {
  if (showJson.value) showJson.value = false
  canvasRef.value?.addNode(type)
}

async function save() {
  try {
    const r = await workflowApi.save(agentId, graph.value, rowVersion.value)
    rowVersion.value = r?.row_version ?? rowVersion.value
    ElMessage.success('已保存')
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function publish() {
  try {
    const saved = await workflowApi.save(agentId, graph.value, rowVersion.value)
    rowVersion.value = saved?.row_version ?? rowVersion.value
    const r = await workflowApi.publish(agentId)
    // 发布本身也是一次 UPDATE（会 bump row_version），必须同步本地版本，
    // 否则发布后第一次保存会被自己的旧版本判成 409
    rowVersion.value = r?.row_version ?? rowVersion.value
    version.value = r.version
    ElMessage.success('已发布 v' + r.version)
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

function openRunPanel() {
  nodeStatus.value = {}
  takenEdges.value = []
  latencyMap.value = {}
  runPanelOpen.value = true
}

/** 运行/调试时把执行路径同步到画布 */
function onTrace(t: { takenEdges: string[]; latency: Record<string, number> }) {
  takenEdges.value = t.takenEdges || []
  latencyMap.value = t.latency || {}
}

function toggleJson() {
  if (showJson.value) showJson.value = false
  else {
    jsonText.value = JSON.stringify(graph.value, null, 2)
    showJson.value = true
  }
}
function applyJson() {
  try {
    graph.value = JSON.parse(jsonText.value)
    showJson.value = false
    selectedId.value = null
    ElMessage.success('已应用')
  } catch {
    ElMessage.error('JSON 格式错误')
  }
}

// ---- AI 生成 ----
function openAi() {
  aiOpen.value = true
}
async function doAiGenerate() {
  const prompt = aiPrompt.value.trim()
  if (!prompt) {
    ElMessage.warning('请先描述你的工作流需求')
    return
  }
  aiBusy.value = true
  aiWarnings.value = []
  try {
    const r = await workflowApi.generate(agentId, prompt)
    // 已有节点时先确认覆盖
    if ((graph.value.nodes || []).length) {
      try {
        await ElMessageBox.confirm(
          `当前画布已有 ${graph.value.nodes.length} 个节点，生成结果将替换画布内容（未保存的修改会丢失）。是否继续？`,
          '替换画布',
          { type: 'warning', confirmButtonText: '替换', cancelButtonText: '取消' },
        )
      } catch {
        return
      }
    }
    graph.value = r.graph
    aiWarnings.value = r.warnings || []
    selectedId.value = null
    aiOpen.value = false
    showJson.value = false
    ElMessage.success(`已生成 ${r.node_count} 个节点，记得点「保存」`)
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    aiBusy.value = false
  }
}

onMounted(load)

// 由父级按钮触发（#extra 里的 AI 按钮）
defineExpose({ openAi })
</script>

<style scoped>
.wf-layout {
  display: flex;
  gap: 12px;
  height: calc(100vh - 200px);
  min-height: 420px;
}
.wf-palette { width: 186px; flex: 0 0 auto; display: flex; flex-direction: column; padding: 12px; }
.wf-palette-title {
  display: flex; flex-direction: column; gap: 8px;
  font-size: 13px; font-weight: 600; color: var(--text-title); margin-bottom: 10px;
}
.wf-palette-search { width: 100%; }
.wf-palette-scroll { flex: 1; min-height: 0; }

.wf-group { margin-bottom: 6px; }
.wf-group-head {
  display: flex; align-items: center; gap: 6px;
  padding: 5px 4px; cursor: pointer; user-select: none;
  font-size: 12px; font-weight: 600; color: var(--text-secondary);
  border-radius: 6px;
}
.wf-group-head:hover { background: var(--bg-subtle); }
.wf-group-arrow { transition: transform 0.15s; font-size: 11px; }
.wf-group-arrow.open { transform: rotate(90deg); }
.wf-group-count { margin-left: auto; font-weight: 400; color: var(--text-muted); font-size: 11px; }
.wf-group-body { padding: 2px 0 4px; }

.wf-palette-btn {
  display: flex; align-items: center; justify-content: space-between; width: 100%;
  padding: 6px 9px; margin-bottom: 5px; border: 1px solid; border-radius: 8px;
  background: #fff; font-size: 12.5px; cursor: pointer; font: inherit;
}
.wf-palette-btn:hover { background: var(--bg-subtle); }
.wf-palette-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.wf-center { flex: 1; min-width: 0; display: flex; flex-direction: column; }

.wf-json { flex: 1; display: flex; flex-direction: column; padding: 12px; }
.wf-json-area { flex: 1; }
.wf-json-area :deep(textarea) { height: 100%; font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px; }
.wf-json-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; }

.wf-props { width: 340px; flex: 0 0 auto; display: flex; flex-direction: column; padding: 12px; }
.wf-props-title { font-size: 13px; font-weight: 600; color: var(--text-title); margin-bottom: 10px; }
.wf-props-scroll { flex: 1; min-height: 0; }

.ai-examples { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 10px; font-size: 12px; }
.ai-example { cursor: pointer; }
.ai-warn { font-size: 12px; line-height: 1.6; }
</style>
