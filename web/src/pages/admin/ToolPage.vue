<template>
  <PageContainer title="工具" subtitle="内置工具清单 / 可选组件安装 / 自定义 HTTP 工具（实验）">
    <template #extra>
      <el-button :icon="Box" @click="openComponents">可选组件</el-button>
      <el-button v-if="canManage" plain type="info" :icon="Plus" @click="open = true">
        自定义工具·实验
      </el-button>
    </template>

    <div class="app-card panel">
      <el-table :data="tools" v-loading="loading" row-key="id">
        <el-table-column label="工具" min-width="220">
          <template #default="{ row }">
            <span class="tool-name">
              <el-icon class="tool-ico"><Tools /></el-icon>
              <b>{{ row.display_name || row.name }}</b>
              <el-tag size="small" type="info">{{ row.kind }}</el-tag>
              <el-tag v-if="!row.builtin" size="small" type="warning">实验 · 未接入</el-tag>
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="标识" width="180" />
        <el-table-column label="描述" show-overflow-tooltip>
          <template #default="{ row }">{{ row.description || '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button size="small" @click="test(row)">测试</el-button>
            <template v-if="canManage && !row.builtin">
              <el-popconfirm title="删除该工具？" @confirm="removeTool(row)">
                <template #reference><el-button size="small" danger :icon="Delete" /></template>
              </el-popconfirm>
            </template>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !tools.length" description="暂无工具" :image-size="70" />
      <div v-if="hasCustomTools" class="foot-note">
        当前版本自定义工具尚未接入智能体调用，仅供接口预演；浏览器等能力请用「可选组件」安装。
      </div>
    </div>

    <!-- 可选组件 -->
    <el-drawer v-model="compOpen" title="可选组件" size="560px" @closed="stopPolling">
      <p class="comp-intro">
        这些是内置的工具运行时，需要时点安装即可（装完 AI 即可调用，无需重启）。
      </p>

      <el-skeleton v-if="compLoading" :rows="4" animated />

      <div v-for="c in components" :key="c.key" class="comp-card" :data-comp="c.key">
        <div class="comp-head">
          <span class="comp-name">{{ c.name }}</span>
          <el-tag size="small" type="info">v{{ c.version }}</el-tag>
          <el-tag size="small">{{ fmtSize(c.size_bytes) }}</el-tag>
          <div class="spacer" />
          <el-tag v-if="c.progress.phase === 'failed'" size="small" type="danger">安装失败</el-tag>
          <el-tag v-else-if="isBusy(c)" size="small" type="warning">{{ phaseLabel(c.progress.phase) }}</el-tag>
          <el-tag v-else-if="c.installed" size="small" type="success">已安装</el-tag>
          <el-tag v-else size="small" type="info">未安装</el-tag>
          <el-tag v-if="c.installed && !c.verified" size="small" type="warning" effect="plain">未验证</el-tag>
        </div>

        <p class="comp-summary">{{ c.summary }}</p>

        <el-progress
          v-if="isBusy(c)"
          :percentage="pct(c.progress)"
          :stroke-width="10"
          :status="c.progress.phase === 'failed' ? 'exception' : undefined"
        >
          <span class="pct-text">{{ fmtSize(c.progress.received) }} / {{ fmtSize(c.progress.total) || '—' }}</span>
        </el-progress>

        <el-alert
          v-if="c.progress.phase === 'failed' && c.progress.error"
          type="error" :closable="false" show-icon :title="c.progress.error" class="comp-alert"
        />
        <el-alert
          v-if="!c.supported"
          type="warning" :closable="false" show-icon class="comp-alert"
          :title="`当前平台（${c.platform || '未知'}）没有可用的安装包`"
        />

        <div class="comp-ops">
          <template v-if="c.installed">
            <el-button size="small" type="primary" :loading="launching" @click="launchBrowser">启动浏览器</el-button>
            <el-button size="small" :icon="Monitor" @click="goBrowserPage">浏览器设置</el-button>
            <el-popconfirm
              title="卸载该组件？已保存的登录态会保留在浏览器配置目录。"
              @confirm="uninstall(c)"
            >
              <template #reference><el-button size="small" danger plain>卸载</el-button></template>
            </el-popconfirm>
          </template>
          <template v-else>
            <el-button
              size="small" type="primary" :loading="isBusy(c)"
              :disabled="!canManage || !c.supported"
              @click="openInstall(c)"
            >安装</el-button>
            <span v-if="!canManage" class="dim">需要「管理工具」权限</span>
          </template>
        </div>

        <div class="comp-meta">
          <div v-if="c.installed">
            安装位置：<span class="mono">{{ c.install_path }}</span>
            <template v-if="c.source === 'upload'"> · 来源：本地上传</template>
            <template v-else-if="c.source === 'url'"> · 来源：镜像地址</template>
            <template v-else-if="c.source === 'official'"> · 来源：官方源</template>
          </div>
          <div v-if="c.installed && c.hint" class="dim">{{ c.hint }}</div>
          <div v-if="!c.installed" class="dim comp-detail">{{ c.detail }}</div>
        </div>
      </div>

      <div class="comp-foot">
        <template v-if="systemBrowsers.length">
          已检测到系统浏览器：<b>{{ systemBrowsers.map((b) => b.name).join('、') }}</b>（可直接使用，无需安装）
        </template>
        <template v-else-if="!components.some((c) => c.installed)">
          本机未检测到 Chrome / Edge，建议安装上面的内置运行时，否则 AI 的网页工具不可用。
        </template>
      </div>
    </el-drawer>

    <!-- 安装方式 -->
    <el-dialog v-model="installOpen" :title="`安装 ${installTarget?.name || ''}`" width="560px">
      <el-radio-group v-model="installMode" class="mode-group">
        <el-radio value="official">官方源（Google 存储）</el-radio>
        <el-radio value="url">镜像地址</el-radio>
        <el-radio value="upload">本地安装包</el-radio>
      </el-radio-group>

      <el-alert
        v-if="installMode === 'official'" type="info" :closable="false" show-icon class="comp-alert"
        :title="`约 ${fmtSize(installTarget?.size_bytes)}，从 Google 官方存储下载`"
        :description="installTarget?.hash_pinned
          ? '已内置 sha256，下载后自动校验，不一致会中止安装'
          : '该平台尚未锁定官方包哈希，本次安装结果会标记为「未验证」'"
      />
      <el-alert
        v-else-if="installMode === 'url'"
        type="warning" :closable="false" show-icon class="comp-alert"
        title="镜像地址不做哈希校验（安装后会标记为「未验证」）"
      />
      <el-alert
        v-else
        type="warning" :closable="false" show-icon class="comp-alert"
        title="本地安装包不做哈希校验（安装后会标记为「未验证」）"
      />

      <el-input
        v-if="installMode === 'url'"
        v-model="installUrl"
        placeholder="https://…/chrome-win64.zip"
        class="url-input"
        clearable
      />
      <div v-if="installMode === 'upload'" class="upload-row">
        <input ref="fileInput" type="file" accept=".zip" @change="onPickFile" />
        <span v-if="pickedFile" class="dim">{{ pickedFile.name }} · {{ fmtSize(pickedFile.size) }}</span>
      </div>

      <template #footer>
        <el-button @click="installOpen = false">取消</el-button>
        <el-button type="primary" :loading="installBusy" @click="doInstall">开始安装</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="testOpen" :title="`测试工具：${cur?.display_name || cur?.name || ''}`" width="560px">
      <el-input v-model="testArgs" type="textarea" :rows="4" placeholder='参数 JSON，如 {"query":"xxx"}' />
      <pre v-if="testResult" class="test-result">{{ testResult }}</pre>
      <template #footer>
        <el-button @click="testOpen = false">关闭</el-button>
        <el-button type="primary" :loading="testing" @click="runTest">执行</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="open" title="新建自定义工具（实验）" width="520px">
      <el-alert
        type="warning" :closable="false" show-icon style="margin-bottom: 12px"
        title="当前版本自定义工具尚未接入智能体调用，仅供接口预演"
      />
      <el-form :model="form" label-position="top">
        <el-form-item label="标识（name）" required><el-input v-model="form.name" placeholder="如 my_tool" /></el-form-item>
        <el-form-item label="显示名"><el-input v-model="form.display_name" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="form.description" type="textarea" :rows="2" /></el-form-item>
        <el-form-item label="类型"><el-input v-model="form.kind" placeholder="如 http / builtin" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="open = false">取消</el-button>
        <el-button type="primary" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </PageContainer>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Plus, Delete, Tools, Box, Monitor } from '@element-plus/icons-vue'
import PageContainer from '@/components/PageContainer.vue'
import { browserApi, componentApi, toolApi, type ComponentStatus, type ToolItem } from '@/api'
import { errMsg } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const canManage = computed(() => auth.hasPermission('tool:manage'))

const tools = ref<ToolItem[]>([])
const loading = ref(true)
const open = ref(false)
const form = reactive<any>({ name: '', display_name: '', description: '', kind: 'http' })
const testOpen = ref(false)
const cur = ref<ToolItem | null>(null)
const testArgs = ref('{}')
const testResult = ref('')
const testing = ref(false)

const hasCustomTools = computed(() => tools.value.some((t) => !t.builtin))

async function load() {
  loading.value = true
  try { tools.value = await toolApi.list() } catch (e) { ElMessage.error(errMsg(e)) } finally { loading.value = false }
}
async function submit() {
  if (!form.name.trim()) { ElMessage.warning('请输入标识'); return }
  try { await toolApi.create({ ...form } as any); ElMessage.success('已创建'); open.value = false; load() }
  catch (e) { ElMessage.error(errMsg(e)) }
}
async function removeTool(t: ToolItem) {
  try { await toolApi.remove(t.id); ElMessage.success('已删除'); load() } catch (e) { ElMessage.error(errMsg(e)) }
}
function test(t: ToolItem) {
  cur.value = t
  testArgs.value = '{}'
  testResult.value = ''
  testOpen.value = true
}
async function runTest() {
  if (!cur.value) return
  testing.value = true
  try {
    let args: any = {}
    try { args = JSON.parse(testArgs.value || '{}') } catch { ElMessage.warning('参数不是合法 JSON'); return }
    const r = await toolApi.test(cur.value.id, args)
    testResult.value = typeof r === 'string' ? r : JSON.stringify(r, null, 2)
  } catch (e) { testResult.value = errMsg(e) } finally { testing.value = false }
}

// ---------------- 可选组件 ----------------
const compOpen = ref(false)
const compLoading = ref(false)
const components = ref<ComponentStatus[]>([])
const systemBrowsers = ref<{ name: string; path: string }[]>([])
const launching = ref(false)

const installOpen = ref(false)
const installTarget = ref<ComponentStatus | null>(null)
const installMode = ref<'official' | 'url' | 'upload'>('official')
const installUrl = ref('')
const installBusy = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const pickedFile = ref<File | null>(null)

let pollTimer: number | null = null

function isBusy(c: ComponentStatus) {
  return ['downloading', 'verifying', 'extracting'].includes(c.progress.phase)
}
function phaseLabel(phase: string) {
  if (phase === 'downloading') return '下载中'
  if (phase === 'verifying') return '校验中'
  if (phase === 'extracting') return '解压中'
  return '处理中'
}
function pct(p: { received: number; total: number }) {
  if (!p.total) return 0
  return Math.min(100, Math.round((p.received / p.total) * 100))
}
function fmtSize(n?: number | null) {
  if (!n || n <= 0) return ''
  const u = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`
}

async function loadComponents() {
  compLoading.value = true
  try {
    components.value = await componentApi.list()
    const st = await browserApi.status()
    systemBrowsers.value = (st.browsers || []).filter((b) => b.source !== 'bundled')
    syncPolling()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    compLoading.value = false
  }
}

function openComponents() {
  compOpen.value = true
  loadComponents()
}

/**
 * 支持从别处（如「浏览器」页的安装引导）深链跳来：
 * /admin/tools?components=1&key=browser_chromium → 自动打开抽屉并滚到该组件。
 */
async function openFromQuery() {
  if (!route.query.components) return
  openComponents()
  const key = String(route.query.key || '')
  if (!key) return
  // 等列表回来再滚动，否则目标元素还不存在
  await new Promise<void>((r) => {
    const t = window.setInterval(() => {
      if (!compLoading.value) { window.clearInterval(t); r() }
    }, 100)
    window.setTimeout(() => { window.clearInterval(t); r() }, 3000)
  })
  document.querySelector(`[data-comp="${key}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

function syncPolling() {
  const busy = components.value.some(isBusy)
  if (busy && pollTimer === null) {
    pollTimer = window.setInterval(refreshProgress, 1500)
  } else if (!busy) {
    stopPolling()
  }
}

function stopPolling() {
  if (pollTimer !== null) { window.clearInterval(pollTimer); pollTimer = null }
}

async function refreshProgress() {
  const busyKeys = components.value.filter(isBusy).map((c) => c.key)
  if (!busyKeys.length) { stopPolling(); return }
  for (const key of busyKeys) {
    try {
      const p = await componentApi.progress(key)
      const item = components.value.find((c) => c.key === key)
      if (item) item.progress = { ...item.progress, ...p }
      if (p.phase === 'failed' && p.error) ElMessage.error(`${item?.name || key}：${p.error}`)
    } catch { /* 轮询失败不打断，下次再试 */ }
  }
  // 有任务结束时刷新完整状态（拿到 installed / executable）
  if (!components.value.some(isBusy)) {
    stopPolling()
    try {
      components.value = await componentApi.list()
      if (components.value.some((c) => c.progress.phase === 'done')) {
        ElMessage.success('组件安装完成')
      }
    } catch { /* ignore */ }
  }
}

function openInstall(c: ComponentStatus) {
  installTarget.value = c
  installMode.value = 'official'
  installUrl.value = ''
  pickedFile.value = null
  installOpen.value = true
}

function onPickFile(ev: Event) {
  const input = ev.target as HTMLInputElement
  pickedFile.value = input.files && input.files[0] ? input.files[0] : null
}

async function doInstall() {
  const c = installTarget.value
  if (!c) return
  installBusy.value = true
  try {
    if (installMode.value === 'official') {
      const r = await componentApi.install(c.key, { source: 'official' })
      ElMessage.info(r.reason || '已开始下载安装')
    } else if (installMode.value === 'url') {
      if (!installUrl.value.trim()) { ElMessage.warning('请填写镜像地址'); return }
      const r = await componentApi.install(c.key, { source: 'url', url: installUrl.value.trim() })
      ElMessage.info(r.reason || '已开始下载安装')
    } else {
      if (!pickedFile.value) { ElMessage.warning('请选择 .zip 安装包'); return }
      const r = await componentApi.installUpload(c.key, pickedFile.value)
      ElMessage.info(r.reason || '已开始解压安装')
    }
    installOpen.value = false
    await loadComponents()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    installBusy.value = false
  }
}

async function uninstall(c: ComponentStatus) {
  try {
    const r = await componentApi.remove(c.key)
    ElMessage.success(r.message || '已卸载')
    await loadComponents()
  } catch (e) { ElMessage.error(errMsg(e)) }
}

async function launchBrowser() {
  launching.value = true
  try {
    const r = await browserApi.launch({})
    ElMessage.success(r.message || '已启动浏览器')
  } catch (e) { ElMessage.error(errMsg(e)) } finally { launching.value = false }
}

function goBrowserPage() {
  compOpen.value = false
  router.push('/admin/browser')
}

onMounted(async () => {
  await load()
  openFromQuery()
})
onBeforeUnmount(stopPolling)
</script>

<style scoped>
.panel { padding: 18px 20px; }
.tool-name { display: inline-flex; align-items: center; gap: 8px; }
.tool-ico { color: var(--color-primary); }
.foot-note {
  margin-top: 10px; font-size: 12.5px; color: var(--el-text-color-secondary);
}
.test-result {
  margin-top: 12px; background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; font-size: 12.5px; white-space: pre-wrap; max-height: 280px; overflow: auto;
  font-family: 'SFMono-Regular', Consolas, monospace;
}

.comp-intro { margin: 0 0 14px; font-size: 13px; color: var(--el-text-color-regular); }
.comp-card {
  border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin-bottom: 12px;
  background: var(--bg-subtle);
}
.comp-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.comp-name { font-weight: 600; }
.spacer { flex: 1; }
.comp-summary { margin: 8px 0; font-size: 13px; color: var(--el-text-color-regular); }
.comp-detail { white-space: pre-line; }
.comp-alert { margin: 8px 0; }
.comp-ops { display: flex; gap: 8px; align-items: center; margin: 10px 0 8px; flex-wrap: wrap; }
.comp-meta { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.8; }
.comp-foot { font-size: 12.5px; color: var(--el-text-color-secondary); padding-top: 6px; }
.dim { color: var(--el-text-color-secondary); font-size: 12px; }
.mono { font-family: var(--el-font-family-mono, monospace); word-break: break-all; }
.pct-text { font-size: 12px; color: var(--el-text-color-secondary); }
.mode-group { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
.url-input { margin-top: 4px; }
.upload-row { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
</style>
