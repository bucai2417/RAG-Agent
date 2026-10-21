<template>
  <el-drawer
    :model-value="modelValue"
    title="集成与测试"
    size="680px"
    append-to-body
    @update:model-value="(v: any) => emit('update:modelValue', v)"
  >
    <el-tabs v-model="tab">
      <!-- 版本 -->
      <el-tab-pane label="版本" name="versions">
        <p class="hint">
          每次「发布」都会存一份完整快照。改坏了可以一键回滚；回滚本身也会留下快照，可再回退。
        </p>
        <div class="case-actions">
          <el-button :icon="RefreshRight" :loading="verLoading" @click="loadVersions">刷新</el-button>
        </div>
        <el-table :data="versions" size="small" v-loading="verLoading" empty-text="还没有发布过版本">
          <el-table-column label="版本" width="90">
            <template #default="{ row }"><b>v{{ row.version }}</b></template>
          </el-table-column>
          <el-table-column label="节点/连线" width="110">
            <template #default="{ row }">{{ row.node_count }} / {{ row.edge_count }}</template>
          </el-table-column>
          <el-table-column label="备注" min-width="140" show-overflow-tooltip>
            <template #default="{ row }">{{ row.note || '-' }}</template>
          </el-table-column>
          <el-table-column label="时间" width="160">
            <template #default="{ row }">{{ row.created_at || '-' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="100">
            <template #default="{ row }">
              <el-popconfirm title="回滚到该版本？当前未发布内容会丢失" @confirm="doRollback(row)">
                <template #reference>
                  <el-button size="small" link type="warning" :disabled="!canEdit">回滚</el-button>
                </template>
              </el-popconfirm>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- 导入导出 -->
      <el-tab-pane label="导入导出" name="port">
        <p class="hint">
          导出为可移植 JSON（不含密钥与租户信息），可跨环境迁移或备份。
          导入会创建<b>新的</b>工作流型智能体，不会覆盖现有工作流。
        </p>
        <div class="case-actions">
          <el-button type="primary" :icon="Download" :loading="exporting" @click="doExport">导出当前工作流</el-button>
          <el-button :icon="Upload" :disabled="!canEdit" @click="fileRef?.click()">从文件导入</el-button>
          <input ref="fileRef" type="file" accept=".json" hidden @change="onFilePick" />
        </div>
        <el-alert v-if="importResult" type="success" :closable="false" show-icon style="margin-top: 12px">
          <template #title>{{ importResult.message }}</template>
          <div class="hint-sm">
            节点 {{ importResult.node_count }} 个 · 智能体 #{{ importResult.agent_id }}
            <el-button size="small" link type="primary" @click="gotoAgent(importResult.agent_id)">打开</el-button>
          </div>
        </el-alert>
      </el-tab-pane>

      <!-- 用量 -->
      <el-tab-pane label="用量" name="usage">
        <WorkflowUsagePanel :agent-id="agentId" />
      </el-tab-pane>

      <!-- 外部接入 -->
      <el-tab-pane label="外部接入" name="hooks">
        <p class="hint">
          外部系统（客服台 / ERP / 自建 App）通过 HTTP 直接调用这个工作流。
          调用会<b>同步返回工作流结果</b>，适合被其他系统当作接口使用。
        </p>

        <div class="hook-create">
          <el-input v-model="newPath" placeholder="自定义路径（可选），如 order-sync" style="width: 220px" clearable />
          <el-input-number v-model="newRateLimit" :min="0" :max="10000" :step="10" style="width: 130px"
            placeholder="限流/分钟" />
          <el-button type="primary" :icon="Plus" :loading="creating" :disabled="!canEdit" @click="createHook">
            创建接入点
          </el-button>
        </div>
        <div class="hint-sm">路径只能用小写字母、数字、下划线、短横线；留空则使用随机 token。限流 0 = 不限。</div>

        <el-input
          v-model="newCallback"
          placeholder="异步回调地址（可选），如 https://crm.example.com/hook"
          style="margin-top: 8px"
          clearable
        />
        <div class="hint-sm">
          配置回调后才能使用 <code>mode=async</code>：立即返回 run_id，跑完 POST 结果到该地址。
          内网地址需在「系统设置 → 外部知识库连接器」开启「允许工作流回调内网地址」。
        </div>

        <el-alert v-if="createdToken" type="success" :closable="false" show-icon style="margin: 12px 0">
          <template #title>接入点已创建（token 仅显示一次，请立即保存）</template>
          <div class="token-box">
            <code>{{ createdToken.token }}</code>
            <el-button size="small" :icon="CopyDocument" @click="copy(createdToken.token)">复制</el-button>
          </div>
          <div class="hint-sm">调用地址：<code>{{ createdToken.hook_url }}</code></div>
        </el-alert>

        <el-table :data="hooks" size="small" v-loading="hookLoading" empty-text="还没有接入点">
          <el-table-column label="名称" min-width="120">
            <template #default="{ row }">{{ row.name }}</template>
          </el-table-column>
          <el-table-column label="地址" min-width="200">
            <template #default="{ row }">
              <code>{{ row.hook_url || '（随机 token）' }}</code>
            </template>
          </el-table-column>
          <el-table-column label="调用" width="80">
            <template #default="{ row }">{{ row.call_count || 0 }}</template>
          </el-table-column>
          <el-table-column label="限流" width="90">
            <template #default="{ row }">
              {{ row.rate_limit_per_min ? `${row.rate_limit_per_min}/分` : '不限' }}
            </template>
          </el-table-column>
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="140">
            <template #default="{ row }">
              <el-button size="small" link @click="toggleHook(row)">
                {{ row.enabled ? '停用' : '启用' }}
              </el-button>
              <el-button size="small" link :icon="DocumentCopy" @click="showCurl(row)">示例</el-button>
            </template>
          </el-table-column>
        </el-table>

        <h4 class="sec-title">同步调用（默认）</h4>
        <pre class="code-block">{{ sampleCurl }}</pre>

        <h4 class="sec-title">异步调用（长流程推荐）</h4>
        <pre class="code-block">{{ sampleAsync }}</pre>
        <div class="hint-sm">
          异步模式要求接入点已配置回调地址。回调会带 <code>X-Signature: sha256=...</code>，
          用你提供的密钥对请求体做 HMAC-SHA256 即可校验来源。
        </div>
      </el-tab-pane>

      <!-- 测试用例 -->
      <el-tab-pane label="测试用例" name="cases">
        <p class="hint">
          把调试过的输入存成用例，改完工作流一键回归。
          断言是「输出包含期望片段」——LLM 输出不确定，精确相等会永远失败。
        </p>

        <div class="case-actions">
          <el-button type="primary" :icon="Plus" :disabled="!canEdit" @click="openCase">新建用例</el-button>
          <el-button :icon="VideoPlay" :loading="runningCases" @click="runAll">全部运行</el-button>
          <span v-if="lastSummary" class="summary">
            通过 {{ lastSummary.passed }} / {{ lastSummary.total }}
            <el-tag v-if="lastSummary.failed" size="small" type="danger" style="margin-left: 6px">
              {{ lastSummary.failed }} 个失败
            </el-tag>
          </span>
        </div>

        <el-table :data="cases" size="small" v-loading="caseLoading" empty-text="还没有测试用例">
          <el-table-column label="名称" min-width="130">
            <template #default="{ row }">{{ row.name }}</template>
          </el-table-column>
          <el-table-column label="输入" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ JSON.stringify(row.input) }}</template>
          </el-table-column>
          <el-table-column label="期望包含" min-width="130" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.expected">{{ row.expected }}</span>
              <el-tag v-else size="small" type="info">冒烟</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="结果" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.last_status" size="small" :type="statusType(row.last_status)">
                {{ statusLabel(row.last_status) }}
              </el-tag>
              <span v-else class="muted">未运行</span>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="80">
            <template #default="{ row }">{{ row.last_latency_ms ? `${row.last_latency_ms}ms` : '-' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="150">
            <template #default="{ row }">
              <el-button size="small" link @click="runOne(row.id)">运行</el-button>
              <el-button size="small" link @click="editCase(row)">编辑</el-button>
              <el-popconfirm title="删除该用例？" @confirm="removeCase(row.id)">
                <template #reference><el-button size="small" link type="danger">删除</el-button></template>
              </el-popconfirm>
            </template>
          </el-table-column>
        </el-table>

        <!-- 运行结果明细 -->
        <template v-if="runResults.length">
          <h4 class="sec-title">最近一次运行</h4>
          <div v-for="r in runResults" :key="r.id" class="case-result" :class="r.status">
            <div class="cr-head">
              <el-tag size="small" :type="statusType(r.status)">{{ statusLabel(r.status) }}</el-tag>
              <b>{{ r.name }}</b>
              <span class="muted">{{ r.latency_ms }}ms</span>
            </div>
            <div v-if="r.error" class="cr-error">{{ r.error }}</div>
            <details v-if="r.actual">
              <summary class="muted">实际输出</summary>
              <pre class="code-block">{{ r.actual }}</pre>
            </details>
          </div>
        </template>
      </el-tab-pane>
    </el-tabs>

    <!-- 用例编辑弹窗 -->
    <el-dialog v-model="caseOpen" :title="editingId ? '编辑用例' : '新建用例'" width="560px" append-to-body>
      <el-form label-position="top" size="small">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="如：查询订单" />
        </el-form-item>
        <el-form-item label="输入（JSON，对应 start 的输入名）" required>
          <el-input v-model="form.inputText" type="textarea" :rows="4" class="mono" placeholder='{"query": "我要查订单"}' />
        </el-form-item>
        <el-form-item label="期望输出包含（留空 = 只做冒烟检查）">
          <el-input v-model="form.expected" type="textarea" :rows="2" class="mono" placeholder="订单" />
        </el-form-item>
        <el-form-item label="多轮历史（可选，用于测试「刚才那个」这类指代）">
          <div v-for="(h, i) in form.history" :key="i" class="hist-row">
            <el-select v-model="h.role" style="width: 92px">
              <el-option value="user" label="客户" />
              <el-option value="assistant" label="客服" />
            </el-select>
            <el-input v-model="h.content" placeholder="这一轮的内容" />
            <el-button size="small" type="danger" :icon="Delete" @click="form.history.splice(i, 1)" />
          </div>
          <el-button size="small" :icon="Plus" @click="form.history.push({ role: 'user', content: '' })">
            添加一轮
          </el-button>
          <div class="hint-sm">留空表示单轮用例。历史会拼进工作流的 history 变量。</div>
        </el-form-item>
        <el-form-item label="启用断言">
          <el-switch v-model="form.assert_contains" />
          <span class="hint-sm" style="margin-left: 8px">关闭后只要能跑完就算通过</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="caseOpen = false">取消</el-button>
        <el-button type="primary" @click="submitCase">保存</el-button>
      </template>
    </el-dialog>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete, VideoPlay, CopyDocument, DocumentCopy, Download, Upload, RefreshRight } from '@element-plus/icons-vue'
import { workflowApi } from '@/api'
import { errMsg } from '@/api/http'
import WorkflowUsagePanel from './WorkflowUsagePanel.vue'

const props = defineProps<{ modelValue: boolean; agentId: number; canEdit: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean]; reload: [] }>()

const router = useRouter()

const tab = ref('versions')

// ---- 版本 ----
const versions = ref<any[]>([])
const verLoading = ref(false)

async function loadVersions() {
  verLoading.value = true
  try {
    versions.value = await workflowApi.listVersions(props.agentId)
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    verLoading.value = false
  }
}

async function doRollback(row: any) {
  try {
    const r = await workflowApi.rollbackVersion(props.agentId, row.id)
    ElMessage.success(r.message)
    emit('reload')
    await loadVersions()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

// ---- 导入导出 ----
const exporting = ref(false)
const importResult = ref<any>(null)
const fileRef = ref<HTMLInputElement>()

async function doExport() {
  exporting.value = true
  try {
    const data = await workflowApi.exportWorkflow(props.agentId)
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${data.name || 'workflow'}.json`
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('已导出')
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    exporting.value = false
  }
}

async function onFilePick(ev: Event) {
  const f = (ev.target as HTMLInputElement).files?.[0]
  if (!f) return
  try {
    const text = await f.text()
    const data = JSON.parse(text)
    importResult.value = await workflowApi.importWorkflow(data)
    ElMessage.success(importResult.value.message)
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    if (fileRef.value) fileRef.value.value = ''
  }
}

function gotoAgent(id: number) {
  router.push(`/agents/${id}/workflow`)
  emit('update:modelValue', false)
}

// ---- 外部接入 ----
const hooks = ref<any[]>([])
const hookLoading = ref(false)
const creating = ref(false)
const newPath = ref('')
const newRateLimit = ref(60)
const newCallback = ref('')
const createdToken = ref<any>(null)

const sampleCurl = computed(() => {
  const url = `${location.origin}/api/v1/hooks/<路径或token>`
  return `curl -X POST "${url}" \\
  -H "Content-Type: application/json" \\
  -d '{"message": "我要查订单 A123", "order_id": "A123"}'

# 返回：
# {"ok": true, "status": "success", "text": "订单 A123：已发货", "run_id": 42}`
})

const sampleAsync = computed(() => {
  const url = `${location.origin}/api/v1/hooks/<路径或token>`
  return `# 1. 提交（立即返回）
curl -X POST "${url}" \\
  -H "Content-Type: application/json" \\
  -d '{"message": "我要查订单 A123", "mode": "async"}'

# {"ok": true, "status": "accepted", "run_id": 42, ...}

# 2. 你的回调地址随后收到：
# POST /your-callback
# X-Signature: sha256=<hmac>
# {"run_id": 42, "ok": true, "status": "success", "text": "订单 A123：已发货"}`
})

async function loadHooks() {
  hookLoading.value = true
  try {
    hooks.value = await workflowApi.listHooks(props.agentId)
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    hookLoading.value = false
  }
}

async function createHook() {
  creating.value = true
  try {
    const r = await workflowApi.createHook(
      props.agentId,
      newPath.value.trim() || undefined,
      { rateLimit: newRateLimit.value, callbackUrl: newCallback.value.trim() || undefined },
    )
    createdToken.value = r
    newPath.value = ''
    newCallback.value = ''
    ElMessage.success('接入点已创建')
    await loadHooks()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    creating.value = false
  }
}

async function toggleHook(row: any) {
  try {
    await workflowApi.toggleHook(row.id, !row.enabled)
    await loadHooks()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

function showCurl(row: any) {
  const url = `${location.origin}/api/v1/hooks/${row.path || '<token>'}`
  ElMessageBox.alert(
    `curl -X POST "${url}" -H "Content-Type: application/json" -d '{"message":"你好"}'`,
    '调用示例',
    { confirmButtonText: '知道了' },
  )
}

function copy(text: string) {
  navigator.clipboard?.writeText(text).then(
    () => ElMessage.success('已复制'),
    () => ElMessage.warning('复制失败，请手动选择'),
  )
}

// ---- 测试用例 ----
const cases = ref<any[]>([])
const caseLoading = ref(false)
const runningCases = ref(false)
const lastSummary = ref<any>(null)
const runResults = ref<any[]>([])

const caseOpen = ref(false)
const editingId = ref<number | null>(null)
const form = reactive({
  name: '', inputText: '{\n  "query": ""\n}', expected: '',
  assert_contains: true, history: [] as { role: string; content: string }[],
})

function statusType(s?: string) {
  return s === 'passed' ? 'success' : s === 'failed' ? 'danger' : s === 'waiting' ? 'warning' : 'info'
}
function statusLabel(s?: string) {
  const m: Record<string, string> = { passed: '通过', failed: '失败', error: '出错', waiting: '待审批' }
  return m[s || ''] || s || '-'
}

async function loadCases() {
  caseLoading.value = true
  try {
    cases.value = await workflowApi.listCases(props.agentId)
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    caseLoading.value = false
  }
}

function openCase() {
  editingId.value = null
  Object.assign(form, {
    name: '', inputText: '{\n  "query": ""\n}', expected: '',
    assert_contains: true, history: [],
  })
  caseOpen.value = true
}

function editCase(row: any) {
  editingId.value = row.id
  Object.assign(form, {
    name: row.name,
    inputText: JSON.stringify(row.input || {}, null, 2),
    expected: row.expected || '',
    assert_contains: row.assert_contains,
    history: JSON.parse(JSON.stringify(row.history || [])),
  })
  caseOpen.value = true
}

async function submitCase() {
  if (!form.name.trim()) {
    ElMessage.warning('请输入用例名称')
    return
  }
  let input: any
  try {
    input = JSON.parse(form.inputText)
  } catch {
    ElMessage.error('输入不是合法 JSON')
    return
  }
  const history = form.history
    .map((h) => ({ role: h.role, content: (h.content || '').trim() }))
    .filter((h) => h.content)
  const payload = {
    name: form.name,
    input,
    expected: form.expected.trim() || null,
    assert_contains: form.assert_contains,
    history,
  }
  try {
    if (editingId.value) await workflowApi.updateCase(editingId.value, payload)
    else await workflowApi.createCase(props.agentId, payload)
    ElMessage.success('已保存')
    caseOpen.value = false
    await loadCases()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function removeCase(id: number) {
  try {
    await workflowApi.removeCase(id)
    ElMessage.success('已删除')
    await loadCases()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function runAll() {
  runningCases.value = true
  try {
    const r = await workflowApi.runCases(props.agentId)
    lastSummary.value = r
    runResults.value = r.results || []
    ElMessage[r.failed ? 'warning' : 'success'](
      r.failed ? `完成：${r.passed} 通过，${r.failed} 失败` : `全部通过（${r.total} 个）`,
    )
    await loadCases()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    runningCases.value = false
  }
}

async function runOne(id: number) {
  runningCases.value = true
  try {
    const r = await workflowApi.runCases(props.agentId, [id])
    lastSummary.value = r
    runResults.value = r.results || []
    await loadCases()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    runningCases.value = false
  }
}

watch(
  () => props.modelValue,
  (v) => {
    if (!v) return
    loadVersions()
    loadHooks()
    loadCases()
  },
)
</script>

<style scoped>
.hint { font-size: 12.5px; color: var(--text-secondary); line-height: 1.7; margin: 0 0 12px; }
.hint-sm { font-size: 11.5px; color: var(--text-muted); margin-top: 4px; line-height: 1.6; }
.hook-create { display: flex; gap: 8px; align-items: center; }
.token-box { display: flex; gap: 8px; align-items: center; margin: 8px 0; }
.token-box code { font-family: monospace; font-size: 12px; word-break: break-all; }
.sec-title { font-size: 13px; font-weight: 600; color: var(--text-title); margin: 18px 0 8px; }
.code-block {
  background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 6px;
  padding: 10px 12px; font-size: 12px; overflow: auto; white-space: pre-wrap;
  font-family: 'SFMono-Regular', Consolas, monospace; margin: 0;
}
.case-actions { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
.summary { font-size: 12.5px; color: var(--text-secondary); margin-left: 4px; }
.case-result { border: 1px solid var(--border-subtle); border-radius: 8px; padding: 8px 12px; margin-bottom: 8px; }
.case-result.failed, .case-result.error { border-color: #ffccc7; background: #fff2f0; }
.cr-head { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.cr-error { color: #cf1322; font-size: 12.5px; margin-top: 6px; }
.mono :deep(textarea) { font-family: monospace; font-size: 12px; }
.hist-row { display: flex; gap: 6px; margin-bottom: 6px; align-items: center; }
.muted { color: var(--text-muted); }
</style>
