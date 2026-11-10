<template>
  <PageContainer title="模型管理" subtitle="配置上游模型 Provider，并按用途（对话 / 向量）指定默认模型">
    <template #extra>
      <el-button v-if="tab === 'provider'" type="primary" :icon="Plus" @click="openProvider()">新建 Provider</el-button>
      <el-button v-else type="primary" :icon="Plus" @click="openConfig()">新建模型配置</el-button>
    </template>

    <div class="app-card panel">
      <el-radio-group v-model="tab" size="small" style="margin-bottom: 16px">
        <el-radio-button value="provider">Provider（{{ providers.length }}）</el-radio-button>
        <el-radio-button value="config">模型配置（{{ configs.length }}）</el-radio-button>
      </el-radio-group>

      <!-- 本地离线向量模型（sentence-transformers） -->
      <div v-if="tab === 'provider'" class="local-card">
        <div class="lc-head">
          <div class="lc-title-row">
            <span class="lc-title">本地离线向量模型</span>
            <el-tag v-if="localStatus" size="small" :type="localStatus.installed ? 'success' : 'warning'">
              {{ localStatus.installed ? '依赖已安装' : '依赖未安装' }}
            </el-tag>
            <span v-if="localStatus?.dependency?.installed" class="lc-dep-size dim">
              占用 {{ mb(localStatus.dependency.size) }} MB
            </span>
            <el-popconfirm
              v-if="localStatus?.dependency?.installed"
              title="卸载依赖后需重新安装才能用本地模型，确定卸载？"
              width="260"
              @confirm="uninstallDependency"
            >
              <template #reference>
                <el-button link size="small" type="danger" :disabled="depInstalling">卸载依赖</el-button>
              </template>
            </el-popconfirm>
            <el-button link size="small" :loading="localLoading" @click="loadLocalStatus">刷新</el-button>
          </div>
          <p class="lc-desc">
            使用 sentence-transformers 在本地计算向量，无需联网、无需 API Key。
            首次使用会下载模型到 <code>{{ localStatus?.cache_dir || 'data/models' }}</code>。
          </p>
        </div>

        <el-alert
          v-if="localStatus && !localStatus.installed"
          type="warning"
          :closable="false"
          show-icon
          style="margin-bottom: 12px"
        >
          <template #title>需要先安装本地推理依赖（torch + sentence-transformers，约 1GB）</template>
          <div class="dep-row">
            <el-button
              type="primary"
              size="small"
              :loading="depInstalling"
              :disabled="depInstalling"
              @click="installDependency"
            >{{ depInstalling ? '安装中…' : '一键安装依赖' }}</el-button>
            <span class="lc-hint dim">
              装到 <code>{{ localStatus.dependency?.dir || localStatus.runtime_dir }}</code>，
              不影响系统 Python；耗时约 5~15 分钟（看网速）。
            </span>
          </div>

          <!-- 安装进度 + pip 日志 -->
          <div v-if="depInstalling || depState.phase === 'failed'" class="dep-progress">
            <div class="dep-step">{{ depState.step || '准备安装…' }}</div>
            <el-progress :percentage="100" :indeterminate="depInstalling" :stroke-width="8" :show-text="false" />
            <div v-if="depState.phase === 'failed'" class="lc-error">安装失败：{{ depState.error }}</div>
            <details v-if="depState.log" class="dep-log" :open="depState.phase === 'failed'">
              <summary>查看安装日志</summary>
              <pre>{{ depState.log }}</pre>
            </details>
          </div>

          <div v-if="!localStatus.dependency?.python" class="lc-hint warn">
            当前环境找不到 Python {{ localStatus.dependency?.required_python }} x64 解释器，无法自动安装；
            请先安装对应版本 Python，或改用「导入本地模型 zip」。
          </div>
        </el-alert>

        <div v-if="localStatus" class="lc-list">
          <div v-for="m in localStatus.models" :key="m.id" class="lc-item">            <div class="lc-main">
              <div class="lc-name">
                {{ m.label }}
                <el-tag v-if="m.cached" size="small" type="success">已缓存</el-tag>
                <el-tag v-else-if="downloadState[m.id]?.phase === 'downloading'" size="small" type="primary">下载中</el-tag>
              </div>
              <div class="lc-meta">
                <span class="mono">{{ m.id }}</span>
                <span class="sep">·</span>
                <span>{{ m.dim ? m.dim + ' 维' : '维度未知' }}</span>
                <span v-if="m.size" class="sep">·</span>
                <span v-if="m.size">{{ m.size }}</span>
              </div>
              <div class="lc-note">{{ m.note }}</div>
              <!-- 下载进度 -->
              <div v-if="downloadState[m.id] && isBusy(m.id)" class="lc-progress">
                <el-progress
                  :percentage="progressPct(m.id)"
                  :status="downloadState[m.id]?.phase === 'failed' ? 'exception' : undefined"
                  :stroke-width="8"
                />
                <span class="lc-bytes">{{ bytesLabel(m.id) }}</span>
              </div>
              <div v-else-if="downloadState[m.id]?.phase === 'failed'" class="lc-error">
                下载失败：{{ downloadState[m.id]?.error }}
              </div>
            </div>
            <div class="lc-ops">
              <el-button
                v-if="!m.cached"
                size="small"
                :loading="downloadState[m.id]?.phase === 'downloading' || downloadState[m.id]?.phase === 'resolving'"
                :disabled="!localStatus.installed || busyAny"
                @click="downloadModel(m.id)"
              >下载</el-button>
              <el-popconfirm v-else title="删除该本地模型文件？" @confirm="removeModel(m.id)">
                <template #reference>
                  <el-button size="small" danger :disabled="busyAny">删除</el-button>
                </template>
              </el-popconfirm>
              <el-button
                size="small"
                type="primary"
                :disabled="!localStatus.installed || !m.cached || busyAny"
                @click="createLocalProvider(m)"
              >添加为向量模型</el-button>
            </div>
          </div>
        </div>
        <el-empty v-else-if="!localLoading" :description="localError ? `无法获取本地模型状态：${localError}` : '无法获取本地模型状态'" :image-size="60">
          <el-button size="small" @click="loadLocalStatus">重试</el-button>
        </el-empty>

        <div class="lc-foot">
          <el-upload :auto-upload="false" :show-file-list="false" accept=".zip" :on-change="onImportZip">
            <el-button size="small" plain :loading="importing" :disabled="busyAny">
              {{ importing ? '导入中…' : '导入本地模型 zip' }}
            </el-button>
          </el-upload>
          <span class="lc-foot-tip dim">
            完全离线环境：把模型目录（含 config.json 与权重）打包成 zip 上传，或事先拷到
            <code>{{ localStatus?.cache_dir || 'data/models' }}/manual/</code>
          </span>
        </div>
      </div>

      <!-- 依赖状态还在探测：独立出来，别让整张卡片等到状态回来才渲染 -->
      <div v-else-if="tab === 'provider'" class="local-card">
        <div class="lc-title-row">
          <span class="lc-title">本地离线向量模型</span>
          <el-tag size="small" type="info">检测中…</el-tag>
        </div>
        <p class="lc-desc">正在检查本地推理依赖与已缓存模型，稍候…</p>
      </div>

      <!-- Provider 列表 -->
      <el-table v-if="tab === 'provider'" v-loading="loading" :data="providers" row-key="id">
        <el-table-column label="名称" min-width="160">
          <template #default="{ row }">
            <b>{{ row.name }}</b>
            <el-tag size="small" type="info" style="margin-left: 6px">{{ row.kind }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="接入地址" min-width="220">
          <template #default="{ row }"><span class="mono">{{ row.base_url || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="密钥" width="110" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.api_key_set ? 'success' : 'info'">{{ row.api_key_set ? '已设置' : '未设置' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="健康状态" width="120" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="healthTag(row.health_status)">{{ healthLabel(row.health_status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'active' ? 'primary' : 'info'">{{ row.status || '—' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="超时" width="80" align="center">
          <template #default="{ row }">{{ row.timeout ?? '—' }}s</template>
        </el-table-column>
        <el-table-column label="操作" width="300" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :loading="testingId === row.id" @click="testProvider(row)">测试</el-button>
            <el-button size="small" @click="healthCheck(row)">健康检查</el-button>
            <el-button size="small" @click="fetchModels(row)">模型</el-button>
            <el-button size="small" :icon="Edit" @click="openProvider(row)" />
            <el-popconfirm title="删除该 Provider？" @confirm="removeProvider(row)">
              <template #reference><el-button size="small" danger :icon="Delete" /></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>

      <!-- 模型配置 -->
      <el-table v-else v-loading="loading" :data="configs" row-key="id">
        <el-table-column label="显示名" min-width="160">
          <template #default="{ row }">
            <b>{{ row.display_name || row.model_name }}</b>
            <el-tag v-if="row.is_default" size="small" type="success" style="margin-left: 6px">默认</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="模型标识" min-width="180">
          <template #default="{ row }"><span class="mono">{{ row.model_name }}</span></template>
        </el-table-column>
        <el-table-column label="用途" width="120" align="center">
          <template #default="{ row }"><el-tag size="small">{{ PURPOSE_LABEL[row.purpose] || row.purpose }}</el-tag></template>
        </el-table-column>
        <el-table-column label="Provider" min-width="140">
          <template #default="{ row }">{{ providerName(row.provider_id) }}</template>
        </el-table-column>
        <el-table-column label="向量维度" width="100" align="center">
          <template #default="{ row }">{{ row.embedding_dim ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="优先级" width="80" align="center">
          <template #default="{ row }">{{ row.priority ?? 0 }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'active' ? 'primary' : 'info'">{{ row.status || '—' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button v-if="!row.is_default" size="small" @click="setDefault(row)">设为默认</el-button>
            <el-button size="small" :icon="Edit" @click="openConfig(row)" />
            <el-popconfirm title="删除该模型配置？" @confirm="removeConfig(row)">
              <template #reference><el-button size="small" danger :icon="Delete" /></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- Provider 新建 / 编辑 -->
    <el-dialog v-model="pOpen" :title="editProvider ? '编辑 Provider' : '新建 Provider'" width="560px">
      <el-form :model="pForm" label-position="top">
        <el-form-item label="名称" required><el-input v-model="pForm.name" placeholder="如：OpenAI 官方" /></el-form-item>
        <el-form-item label="类型" required>
          <el-select v-model="pForm.kind" filterable allow-create style="width: 100%">
            <el-option v-for="k in KINDS" :key="k" :value="k" :label="k" />
          </el-select>
        </el-form-item>
        <el-form-item label="接入地址 Base URL">
          <el-input
            v-model="pForm.base_url"
            :placeholder="KIND_HINT[pForm.kind] || 'https://api.openai.com/v1'"
          />
        </el-form-item>
        <el-form-item :label="editProvider ? 'API Key（留空则不变）' : 'API Key'">
          <el-input v-model="pForm.api_key" type="password" show-password :placeholder="editProvider && editProvider.api_key_set ? '已设置，留空保持不变' : 'sk-...'" />
        </el-form-item>
        <el-form-item label="超时（秒）">
          <el-input-number v-model="pForm.timeout" :min="1" :max="600" controls-position="right" />
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="pForm.status" style="width: 100%">
            <el-option value="active" label="启用" />
            <el-option value="disabled" label="停用" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pOpen = false">取消</el-button>
        <el-button type="primary" @click="submitProvider">保存</el-button>
      </template>
    </el-dialog>

    <!-- 模型配置新建 / 编辑 -->
    <el-dialog v-model="cOpen" :title="editConfig ? '编辑模型配置' : '新建模型配置'" width="560px">
      <el-form :model="cForm" label-position="top">
        <el-form-item label="Provider" required>
          <el-select
            v-model="cForm.provider_id"
            style="width: 100%"
            @change="onProviderChange"
          >
            <el-option v-for="p in providers" :key="p.id" :value="p.id" :label="p.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="用途" required>
          <el-select v-model="cForm.purpose" style="width: 100%">
            <el-option v-for="(label, k) in PURPOSE_LABEL" :key="k" :value="k" :label="label" />
          </el-select>
        </el-form-item>
        <el-form-item label="模型标识" required>
          <!-- 可选可填：优先从 Provider 拉取模型列表，拉不到（或本地/离线驱动）时手动输入 -->
          <div class="model-pick">
            <el-select
              v-model="cForm.model_name"
              filterable
              allow-create
              default-first-option
              clearable
              :loading="modelsLoading"
              :placeholder="modelsLoading ? '正在获取模型列表…' : '选择或直接输入模型标识'"
              style="flex: 1"
            >
              <el-option v-for="m in modelOptions" :key="m" :value="m" :label="m" />
            </el-select>
            <el-button
              :icon="Refresh"
              :loading="modelsLoading"
              :disabled="!cForm.provider_id"
              title="从 Provider 获取模型列表"
              @click="loadModelOptions(cForm.provider_id)"
            />
          </div>
          <div v-if="modelPickMsg" class="model-msg">{{ modelPickMsg }}</div>
          <div v-else class="model-msg dim">
            可直接输入型号；点右侧按钮从 Provider 拉取可用列表
          </div>
        </el-form-item>
        <el-form-item label="显示名">
          <el-input v-model="cForm.display_name" placeholder="留空则显示模型标识" />
        </el-form-item>
        <el-form-item v-if="cForm.purpose === 'embedding'" label="向量维度">
          <el-input-number v-model="cForm.embedding_dim" :min="0" controls-position="right" />
          <div class="model-msg dim">
            本地模型请填真实维度（BGE small 中文 512 / BGE base 中文 768 / MiniLM 384）。
            换用不同维度的模型后，已有知识库必须「重新处理」文档，否则新旧向量无法比对。
          </div>
        </el-form-item>
        <el-form-item label="优先级" extra="数字越小越优先">
          <el-input-number v-model="cForm.priority" :min="0" controls-position="right" />
        </el-form-item>
        <el-form-item label="设为默认"><el-switch v-model="cForm.is_default" /></el-form-item>
        <el-form-item label="状态">
          <el-select v-model="cForm.status" style="width: 100%">
            <el-option value="active" label="启用" />
            <el-option value="disabled" label="停用" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="cOpen = false">取消</el-button>
        <el-button type="primary" @click="submitConfig">保存</el-button>
      </template>
    </el-dialog>

    <!-- 可用模型列表 -->
    <el-dialog v-model="modelsOpen" :title="`可用模型：${modelsProvider?.name || ''}`" width="520px">
      <el-alert v-if="modelsMsg" :type="modelsOk ? 'success' : 'warning'" :closable="false" :title="modelsMsg" style="margin-bottom: 12px" />
      <el-empty v-if="!models.length" description="未获取到模型" :image-size="70" />
      <el-table v-else :data="models" max-height="360">
        <el-table-column prop="id" label="模型 ID" show-overflow-tooltip />
        <el-table-column prop="owned_by" label="归属" width="140" />
      </el-table>
      <template #footer><el-button @click="modelsOpen = false">关闭</el-button></template>
    </el-dialog>
  </PageContainer>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Edit, Delete, Refresh } from '@element-plus/icons-vue'
import PageContainer from '@/components/PageContainer.vue'
import { providerApiFull, type LocalEmbedModel, type LocalEmbedStatus, type LocalDownloadProgress, type LocalDependencyProgress, type ModelConfig } from '@/api'
import { errMsg } from '@/api/http'

// 与后端 providers/registry.py 的驱动分支保持一致；仍允许 allow-create 填自定义
const KINDS = [
  'openai', 'azure', 'anthropi', 'dashscope', 'deepseek', 'qwen', 'ollama',
  'local_st', 'local_bge', 'local_hash', 'local_agent', 'custom',
]
// kind → 接入地址的填写提示（本地驱动用模型路径/标识，不是 URL）
const KIND_HINT: Record<string, string> = {
  local_st: '本地向量模型标识，如 BAAI/bge-small-zh-v1.5（无需 URL，模型在上方面板下载）',
  local_bge: '已由 local_st 取代：本地 bge 模型目录路径',
  local_hash: '离线自测（无需网络），填 local 即可',
  local_agent: '本地 Agent 桥接地址',
}
const PURPOSE_LABEL: Record<string, string> = {
  chat: '对话', embedding: '向量', rerank: '重排', vision: '视觉', stt: '语音识别', tts: '语音合成',
}

const tab = ref('provider')
const loading = ref(true)
const providers = ref<any[]>([])
const configs = ref<ModelConfig[]>([])
const testingId = ref<number | null>(null)

function healthTag(s?: string) {
  if (s === 'ok') return 'success'
  if (s === 'degraded') return 'warning'
  if (s === 'down') return 'danger'
  return 'info'
}
function healthLabel(s?: string) {
  return ({ ok: '正常', degraded: '降级', down: '不可用', unknown: '未知' } as any)[s || ''] || s || '未知'
}
function providerName(id: number) {
  const p = providers.value.find((x) => x.id === id)
  return p ? p.name : `#${id}`
}

async function load() {
  loading.value = true
  try {
    providers.value = await providerApiFull.list().catch(() => [])
    configs.value = await providerApiFull.configs().catch(() => [])
  } catch (e) { ElMessage.error(errMsg(e)) } finally { loading.value = false }
}

// ---- Provider ----
const pOpen = ref(false)
const editProvider = ref<any>(null)
const pForm = reactive<any>({ name: '', kind: 'openai', base_url: '', api_key: '', timeout: 60, status: 'active' })

function openProvider(row?: any) {
  editProvider.value = row || null
  Object.assign(pForm, {
    name: row?.name || '', kind: row?.kind || 'openai', base_url: row?.base_url || '',
    api_key: '', timeout: row?.timeout ?? 60, status: row?.status || 'active',
  })
  pOpen.value = true
}
async function submitProvider() {
  if (!pForm.name.trim()) { ElMessage.warning('请输入名称'); return }
  const payload: any = {
    name: pForm.name, kind: pForm.kind, base_url: pForm.base_url,
    timeout: Number(pForm.timeout) || 60, status: pForm.status,
  }
  if (pForm.api_key) payload.api_key = pForm.api_key
  try {
    if (editProvider.value) await providerApiFull.update(editProvider.value.id, payload)
    else await providerApiFull.create(payload)
    ElMessage.success('已保存')
    pOpen.value = false
    load()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function removeProvider(row: any) {
  try { await providerApiFull.remove(row.id); ElMessage.success('已删除'); load() } catch (e) { ElMessage.error(errMsg(e)) }
}
async function testProvider(row: any) {
  testingId.value = row.id
  try {
    const r: any = await providerApiFull.test(row.id, 'chat')
    if (r?.ok !== false) ElMessage.success(`测试通过：${r?.message || '连通'}`)
    else ElMessage.error(`测试失败：${r?.message || '未知错误'}`)
  } catch (e) { ElMessage.error(errMsg(e)) } finally { testingId.value = null }
}
async function healthCheck(row: any) {
  try {
    const r = await providerApiFull.health(row.id)
    if (r.ok) ElMessage.success(`健康：${r.message}（${r.latency_ms}ms）`)
    else ElMessage.error(`异常：${r.message}`)
    load()
  } catch (e) { ElMessage.error(errMsg(e)) }
}

const modelsOpen = ref(false)
const modelsProvider = ref<any>(null)
const models = ref<{ id: string; owned_by?: string }[]>([])
const modelsMsg = ref('')
const modelsOk = ref(true)
// ---- 模型标识：从 Provider 拉取可选列表（可手填）----
const modelOptions = ref<string[]>([])
const modelsLoading = ref(false)
const modelPickMsg = ref('')

/** 拉取某 Provider 的可用模型，填充「模型标识」下拉。失败不阻断手填。 */
async function loadModelOptions(providerId: number | null) {
  modelOptions.value = []
  modelPickMsg.value = ''
  if (!providerId) return
  modelsLoading.value = true
  try {
    const r = await providerApiFull.listModels(providerId)
    const ids = (r.models || []).map((m) => m.id).filter(Boolean)
    modelOptions.value = ids
    if (r.ok === false) {
      // 例如本地/离线驱动：给出原因，引导手填
      modelPickMsg.value = r.message || '该 Provider 不支持自动获取模型，请手动填写'
    } else if (!ids.length) {
      modelPickMsg.value = '未获取到模型，请手动填写模型标识'
    } else {
      modelPickMsg.value = `已获取 ${ids.length} 个模型，可从下拉选择`
    }
  } catch (e) {
    modelPickMsg.value = `获取模型列表失败（${errMsg(e)}），可手动填写`
  } finally {
    modelsLoading.value = false
  }
}

function onProviderChange(id: number | null) {
  loadModelOptions(id)
}

async function fetchModels(row: any) {
  modelsProvider.value = row
  models.value = []
  modelsMsg.value = ''
  modelsOk.value = true
  modelsOpen.value = true
  try {
    const r = await providerApiFull.listModels(row.id)
    models.value = r.models || []
    modelsOk.value = r.ok !== false
    modelsMsg.value = r.message || (r.models?.length ? `共 ${r.models.length} 个模型` : '未获取到模型')
  } catch (e) { ElMessage.error(errMsg(e)); modelsMsg.value = errMsg(e); modelsOk.value = false }
}

// ---- 模型配置 ----
const cOpen = ref(false)
const editConfig = ref<ModelConfig | null>(null)
const cForm = reactive<any>({
  provider_id: null, purpose: 'chat', model_name: '', display_name: '',
  embedding_dim: 0, priority: 0, is_default: false, status: 'active',
})

function openConfig(row?: ModelConfig) {
  editConfig.value = row || null
  Object.assign(cForm, {
    provider_id: row?.provider_id ?? (providers.value[0]?.id ?? null),
    purpose: row?.purpose || 'chat', model_name: row?.model_name || '',
    display_name: row?.display_name || '', embedding_dim: row?.embedding_dim ?? 0,
    priority: row?.priority ?? 0, is_default: row?.is_default ?? false, status: row?.status || 'active',
  })
  cOpen.value = true
  // 打开即尝试拉取该 Provider 的模型列表，供「模型标识」下拉使用
  loadModelOptions(cForm.provider_id)
}
async function submitConfig() {
  if (!cForm.provider_id) { ElMessage.warning('请选择 Provider'); return }
  if (!cForm.model_name.trim()) { ElMessage.warning('请输入模型标识'); return }
  const payload: any = {
    provider_id: cForm.provider_id, purpose: cForm.purpose, model_name: cForm.model_name,
    display_name: cForm.display_name || cForm.model_name, embedding_dim: Number(cForm.embedding_dim) || 0,
    priority: Number(cForm.priority) || 0, is_default: cForm.is_default, status: cForm.status,
  }
  try {
    if (editConfig.value) await providerApiFull.updateConfig(editConfig.value.id, payload)
    else await providerApiFull.createConfig(payload)
    ElMessage.success('已保存')
    cOpen.value = false
    load()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function removeConfig(row: ModelConfig) {
  try { await providerApiFull.removeConfig(row.id); ElMessage.success('已删除'); load() } catch (e) { ElMessage.error(errMsg(e)) }
}
async function setDefault(row: ModelConfig) {
  try {
    await ElMessageBox.confirm(`将「${row.display_name || row.model_name}」设为 ${PURPOSE_LABEL[row.purpose] || row.purpose} 默认模型？`, '设为默认', { type: 'warning' })
  } catch { return }
  try { await providerApiFull.updateConfig(row.id, { is_default: true }); ElMessage.success('已设为默认'); load() } catch (e) { ElMessage.error(errMsg(e)) }
}

// ---------------- 本地离线向量模型 ----------------
const localStatus = ref<LocalEmbedStatus | null>(null)
const localLoading = ref(false)
/** 状态拉取失败原因（卡片已渲染、只是拿不到数据时展示，并给重试） */
const localError = ref('')
const importing = ref(false)
/** 依赖安装进度（只在安装/失败时有内容） */
const depState = reactive<LocalDependencyProgress>({ phase: 'idle', step: '', log: '', error: null })
const depInstalling = computed(() => depState.phase === 'running')
let depTimer: ReturnType<typeof setInterval> | null = null
/** model_id → 最近一次进度快照（只有正在下载的才有值） */
const downloadState = reactive<Record<string, LocalDownloadProgress>>({})
let pollTimer: ReturnType<typeof setInterval> | null = null
let pollingModelId = ''

const busyAny = computed(() => Object.values(downloadState).some((p) => isBusyPhase(p?.phase)))

function isBusyPhase(p?: string) {
  return p === 'resolving' || p === 'downloading' || p === 'verifying'
}
function isBusy(modelId: string) {
  return isBusyPhase(downloadState[modelId]?.phase)
}
function progressPct(modelId: string) {
  const st = downloadState[modelId]
  if (!st) return 0
  if (st.phase === 'done') return 100
  if (!st.total) return st.phase === 'resolving' ? 0 : 1
  return Math.min(99, Math.floor((st.received / st.total) * 100))
}
function mb(n?: number | null) {
  return ((n || 0) / 1024 / 1024).toFixed(1)
}
function bytesLabel(modelId: string) {
  const st = downloadState[modelId]
  if (!st) return ''
  return `${mb(st.received)} / ${st.total ? mb(st.total) + ' MB' : '大小未知'}`
}

async function loadLocalStatus() {
  localLoading.value = true
  localError.value = ''
  try {
    localStatus.value = await providerApiFull.localStatus()
    // 后端可能重启过、进度已归零：把还没跑完的进度清掉，避免一直显示「下载中」
    for (const m of localStatus.value.models || []) {
      const p = m.progress
      if (p && isBusyPhase(p.phase)) downloadState[m.id] = p
      else if (downloadState[m.id] && !isBusy(m.id) && downloadState[m.id].phase !== 'failed') delete downloadState[m.id]
    }
    // 依赖安装进度：页面刷新后也能接上（后端仍在跑）
    const dp = localStatus.value.dependency?.progress
    if (dp && (dp.phase === 'running' || dp.phase === 'failed')) {
      Object.assign(depState, dp)
      if (dp.phase === 'running') startDepPolling()
    }
  } catch (e) {
    // 拿不到状态不能把页面留成空白：卡片会切到「无法获取」并给出重试入口
    localStatus.value = null
    localError.value = errMsg(e)
  } finally {
    localLoading.value = false
  }
}

// ---------------- 依赖一键安装 / 卸载 ----------------
function stopDepPolling() {
  if (depTimer) { clearInterval(depTimer); depTimer = null }
}

function startDepPolling() {
  if (depTimer) return
  depTimer = setInterval(async () => {
    try {
      const p = await providerApiFull.localDependencyProgress()
      Object.assign(depState, p)
      if (p.phase === 'running') return
      stopDepPolling()
      if (p.phase === 'done') ElMessage.success(p.step || '依赖安装完成')
      else if (p.phase === 'failed') ElMessage.error('依赖安装失败，详情见下方日志')
      await loadLocalStatus()
    } catch {
      // 网络抖动：下一轮再试
    }
  }, 1500)
}

async function installDependency() {
  Object.assign(depState, { phase: 'running', step: '准备安装…', log: '', error: null })
  try {
    const r = await providerApiFull.installLocalDependency()
    if (r.started) { startDepPolling(); return }
    depState.phase = 'idle'
    if (r.already_installed || /已安装/.test(r.reason || '')) {
      ElMessage.info(r.reason || '依赖已安装')
      await loadLocalStatus()
      return
    }
    depState.phase = 'failed'
    depState.error = r.reason || '无法开始安装'
    ElMessage.warning(r.reason || '无法开始安装')
  } catch (e) {
    depState.phase = 'failed'
    depState.error = errMsg(e)
    ElMessage.error(errMsg(e))
  }
}

async function uninstallDependency() {
  try {
    const r = await providerApiFull.uninstallLocalDependency()
    if (r.ok) ElMessage.success(r.message || '依赖已卸载')
    else ElMessage.warning(r.reason || '卸载未完成')
    Object.assign(depState, { phase: 'idle', step: '', log: '', error: null })
    await loadLocalStatus()
  } catch (e) { ElMessage.error(errMsg(e)) }
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
  pollingModelId = ''
}

function startPolling(modelId: string) {
  stopPolling()
  pollingModelId = modelId
  pollTimer = setInterval(async () => {
    try {
      const p = await providerApiFull.localProgress(modelId)
      downloadState[modelId] = p
      if (p.phase === 'done' || p.phase === 'failed') {
        stopPolling()
        if (p.phase === 'done') ElMessage.success('模型下载完成')
        else ElMessage.error(`下载失败：${p.error || '未知错误'}`)
        await loadLocalStatus()
      }
    } catch {
      // 轮询失败不打断用户操作，等下一轮
    }
  }, 1000)
}

async function downloadModel(modelId: string) {
  downloadState[modelId] = { phase: 'resolving', received: 0, total: 0, error: null }
  try {
    const r = await providerApiFull.downloadLocal(modelId)
    if (r.started) {
      startPolling(modelId)
      return
    }
    delete downloadState[modelId]
    if (r.reason) {
      // 「已下载」不是错误，其余才是
      const done = /已下载/.test(r.reason)
      done ? ElMessage.info(r.reason) : ElMessage.warning(r.reason)
      if (r.install_hint) ElMessage.info({ message: r.install_hint, duration: 8000 })
      await loadLocalStatus()
    }
  } catch (e) {
    delete downloadState[modelId]
    ElMessage.error(errMsg(e))
  }
}

async function removeModel(modelId: string) {
  try {
    await providerApiFull.removeLocal(modelId)
    ElMessage.success('已删除')
    await loadLocalStatus()
  } catch (e) { ElMessage.error(errMsg(e)) }
}

async function onImportZip(file: { raw?: File }) {
  const f = file?.raw
  if (!f) return
  importing.value = true
  try {
    const r = await providerApiFull.importLocal(f)
    ElMessage.success(`已导入「${r.model_id}」${r.dim ? `（${r.dim} 维）` : ''}`)
    await loadLocalStatus()
  } catch (e) { ElMessage.error(errMsg(e)) } finally { importing.value = false }
}

/**
 * 一次把「Provider + embedding 模型配置」建好，用户点一下就能用。
 * 已存在同标识的 local_st Provider 时只补模型配置，避免重复建。
 */
async function createLocalProvider(m: LocalEmbedModel) {
  try {
    let p = providers.value.find((x) => x.kind === 'local_st' && x.base_url === m.id)
    if (!p) {
      p = await providerApiFull.create({
        name: `本地 ${m.label || m.id}`,
        kind: 'local_st',
        base_url: m.id,
        timeout: 120,
      })
    }
    const exists = configs.value.some((c) => c.provider_id === p.id && c.model_name === m.id && c.purpose === 'embedding')
    if (!exists) {
      const hasDefault = configs.value.some((c) => c.purpose === 'embedding' && c.is_default)
      await providerApiFull.createConfig({
        provider_id: p.id,
        purpose: 'embedding',
        model_name: m.id,
        display_name: m.label || m.id,
        embedding_dim: m.dim || 0,
        is_default: !hasDefault,
        priority: 50,
      })
    }
    ElMessage.success(exists ? '该模型已在向量模型列表中' : `已添加为向量模型：${m.label || m.id}`)
    await load()
    await loadLocalStatus()
  } catch (e) { ElMessage.error(errMsg(e)) }
}

onMounted(() => {
  load()
  loadLocalStatus()
})
onBeforeUnmount(() => { stopPolling(); stopDepPolling() })
</script>

<style scoped>
.panel { padding: 18px 20px; }
.mono { font-family: monospace; font-size: 12px; color: var(--text-secondary); }

/* 模型标识：下拉 + 拉取按钮 */
.model-pick { display: flex; gap: 8px; width: 100%; align-items: center; }
.model-msg { font-size: 12px; color: var(--text-muted); margin-top: 4px; line-height: 1.5; }
.model-msg.dim { opacity: 0.85; }

/* 本地离线向量模型卡片 */
.local-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 16px;
  background: var(--el-fill-color-blank);
}
.lc-head { margin-bottom: 12px; }
.lc-title-row { display: flex; align-items: center; gap: 10px; }
.lc-title { font-weight: 600; font-size: 14px; }
.lc-desc { margin: 6px 0 0; font-size: 12px; color: var(--text-secondary); line-height: 1.6; }
.lc-desc code, .lc-foot-tip code { font-family: monospace; font-size: 11px; }
.lc-hint { font-size: 12px; line-height: 1.7; white-space: pre-wrap; word-break: break-all; }
.lc-hint.dim, .lc-foot-tip.dim { color: var(--text-muted); }
.lc-hint.warn { margin-top: 8px; color: var(--el-color-warning); }
.lc-dep-size { font-size: 12px; }

/* 依赖安装：按钮行 / 进度 / pip 日志 */
.dep-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 6px; }
.dep-progress { margin-top: 10px; }
.dep-step { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
.dep-log { margin-top: 8px; }
.dep-log summary { font-size: 12px; color: var(--text-secondary); cursor: pointer; }
.dep-log pre {
  margin: 6px 0 0;
  padding: 8px 10px;
  max-height: 220px;
  overflow: auto;
  font-size: 11px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  background: var(--el-fill-color-lighter);
  border-radius: 4px;
  color: var(--text-secondary);
}

.lc-list { display: flex; flex-direction: column; gap: 10px; }
.lc-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
}
.lc-main { min-width: 0; flex: 1; }
.lc-name { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 500; }
.lc-meta { margin-top: 4px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 12px; color: var(--text-secondary); }
.lc-meta .sep { opacity: 0.5; }
.lc-note { margin-top: 2px; font-size: 12px; color: var(--text-muted); }
.lc-progress { margin-top: 8px; display: flex; align-items: center; gap: 10px; }
.lc-progress :deep(.el-progress) { flex: 1; }
.lc-bytes { font-size: 11px; color: var(--text-muted); white-space: nowrap; font-family: monospace; }
.lc-error { margin-top: 6px; font-size: 12px; color: var(--el-color-danger); word-break: break-all; }
.lc-ops { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }

.lc-foot { margin-top: 12px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.lc-foot-tip { font-size: 12px; line-height: 1.6; }
</style>
