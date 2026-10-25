<template>
  <PageContainer :title="`编辑智能体：${agent?.name || ''}`" subtitle="设定智能体的人设、知识库范围、可调用的技能与工具，以及多套行为模式">
    <template #extra>
      <el-button :icon="ArrowLeft" @click="$router.push('/agents')">返回</el-button>
      <el-button type="primary" :icon="VideoPlay" @click="testOpen = true">测试对话</el-button>
    </template>

    <el-tabs v-model="tab">
      <!-- 基础配置 -->
      <el-tab-pane label="基础配置" name="basic">
        <div class="app-card panel">
          <el-form :model="form" label-position="top" style="max-width: 720px">
            <el-form-item label="名称" required><el-input v-model="form.name" /></el-form-item>
            <el-form-item label="描述"><el-input v-model="form.description" type="textarea" :rows="2" /></el-form-item>
            <el-form-item label="可见性">
              <el-radio-group v-model="form.visibility">
                <el-radio-button value="private">仅自己</el-radio-button>
                <el-radio-button value="tenant">全租户可见</el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="人设（系统提示词）">
              <div class="persona-head">
                <span class="persona-hint">
                  设定这个智能体的身份与说话方式。写清四项效果最好：
                  <b>角色</b>（你是谁）、<b>语气</b>（怎么说话）、<b>边界</b>（不做什么）、<b>输出格式</b>（怎么组织）
                </span>
                <el-select
                  v-model="baseTemplateId"
                  clearable
                  filterable
                  size="small"
                  placeholder="套用模板"
                  style="width: 190px; flex-shrink: 0"
                  @change="applyBaseTemplate"
                >
                  <el-option label="空白自定义" :value="0" />
                  <el-option-group v-if="builtinTemplates.length" label="内置模板">
                    <el-option v-for="t in builtinTemplates" :key="t.id" :value="t.id" :label="t.name" />
                  </el-option-group>
                  <el-option-group v-if="customTemplates.length" label="我的模板">
                    <el-option v-for="t in customTemplates" :key="t.id" :value="t.id" :label="t.name" />
                  </el-option-group>
                </el-select>
              </div>
              <el-input
                v-model="form.system_prompt"
                type="textarea"
                :rows="7"
                placeholder="例如：你是本公司的客服助手。语气：礼貌、简洁。边界：不臆造政策，不确定时引导转人工。输出：先给结论，再分点给步骤。"
              />
              <div class="persona-hint dim">
                留空也可以，智能体会用默认的通用助手风格。此处是「人设基座」，行为模式里的提示词会叠加在它之后。
              </div>
            </el-form-item>
            <el-form-item label="开场白（新对话时展示）"><el-input v-model="form.greeting" type="textarea" :rows="2" /></el-form-item>
            <el-form-item label="对话模型">
              <el-select v-model="form.model_config_id" clearable placeholder="默认" style="max-width: 320px">
                <el-option v-for="c in chatConfigs" :key="c.id" :value="c.id" :label="c.display_name" />
              </el-select>
            </el-form-item>
            <el-form-item label="温度"><el-input-number v-model="form.temperature" :min="0" :max="2" :step="0.1" /></el-form-item>
            <el-form-item label="知识库范围（留空=自动检索全部有权库）">
              <el-select v-model="form.kb_ids" multiple style="width: 100%">
                <el-option v-for="k in kbs" :key="k.id" :value="k.id" :label="k.name" />
              </el-select>
            </el-form-item>
            <el-form-item label="启用技能（能力包/工具/集合）">
              <el-select v-model="form.skill_ids" multiple style="width: 100%">
                <el-option v-for="s in parentSkills" :key="s.id" :value="s.id" :label="`${s.name}（${kindLabel(s.kind)}）`" />
              </el-select>
            </el-form-item>
            <div v-for="s in skillsWithParams" :key="s.id" class="skill-params">
              <div class="sp-title">技能参数：{{ s.name }}</div>
              <JsonSchemaForm v-model="skillParams[s.slug]" :schema="s.params_schema" />
            </div>
            <el-form-item label="最大工具调用轮次"><el-input-number v-model="form.max_turns" :min="1" /></el-form-item>
            <el-button type="primary" @click="save">保存</el-button>
          </el-form>
        </div>
      </el-tab-pane>

      <!-- 行为模式 -->
      <el-tab-pane label="行为模式（多套人设）" name="modes">
        <div class="app-card panel">
          <div class="panel-head">
            <span class="section-title">行为模式 —— 同一智能体的多套人设</span>
            <el-button :icon="Plus" @click="openAddMode">添加模式</el-button>
          </div>
          <p class="persona-hint" style="margin: -6px 0 12px">
            每套模式是一份独立的人设（提示词，可另加知识库/技能范围）。
            聊天时在顶部「模式」下拉里切换，即可让同一个智能体换一种身份和说话方式应答。
          </p>
          <el-table :data="modes" size="small">
            <el-table-column prop="name" label="名称" />
            <el-table-column prop="system_prompt" label="提示词" show-overflow-tooltip />
            <el-table-column label="知识库覆盖" width="120">
              <template #default="{ row }">{{ row.kb_ids?.length ? `${row.kb_ids.length} 个` : '继承' }}</template>
            </el-table-column>
            <el-table-column label="技能覆盖" width="120">
              <template #default="{ row }">{{ row.skill_ids?.length ? `${row.skill_ids.length} 个` : '继承' }}</template>
            </el-table-column>
            <el-table-column label="默认" width="70">
              <template #default="{ row }"><el-tag v-if="row.is_default" size="small" type="success">是</el-tag><span v-else>-</span></template>
            </el-table-column>
            <el-table-column label="操作" width="220">
              <template #default="{ row }">
                <el-button size="small" :icon="Edit" @click="openEditMode(row)" />
                <el-button size="small" @click="setDefaultMode(row)">设为默认</el-button>
                <el-popconfirm title="删除该模式？" @confirm="removeMode(row)">
                  <template #reference><el-button size="small" type="danger" :icon="Delete" /></template>
                </el-popconfirm>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- 可用工具 -->
      <el-tab-pane label="可用工具" name="tools">
        <div class="app-card panel">
          <div style="max-width: 720px">
            <h4 class="p-title">内置工具</h4>
            <div v-for="t in BUILTIN_TOOLS" :key="t.name" class="tool-row">
              <el-switch :model-value="!!builtinState[t.name]" @update:model-value="(v: any) => (builtinState[t.name] = v)" />
              <span class="tool-label">{{ t.label }}</span>
              <el-tag size="small" :type="t.write ? 'warning' : 'info'">{{ t.perm }}{{ t.write ? ' · 需人工确认' : '' }}</el-tag>
            </div>
            <h4 class="p-title">管理类工具</h4>
            <div class="tool-row">
              <el-switch v-model="adminEnabled" />
              <span class="tool-label">启用管理类工具（创建技能/智能体、写文档等）</span>
            </div>
            <div style="margin: 12px 0 16px">
              <div class="tool-sub">限定可用工具（留空=全部）</div>
              <el-select v-model="adminToolsSel" multiple filterable clearable placeholder="留空=全部管理工具" style="width: 100%">
                <el-option v-for="n in adminToolsPool" :key="n" :value="n" :label="n" />
              </el-select>
            </div>
            <el-button type="primary" @click="save">保存工具配置</el-button>
          </div>
        </div>
      </el-tab-pane>

      <!-- 版本 -->
      <el-tab-pane label="版本" name="versions">
        <div class="app-card panel">
          <div class="panel-head">
            <span class="section-title">版本快照（可回滚）</span>
            <el-button :icon="Plus" @click="snapshot">生成快照</el-button>
          </div>
          <el-table :data="versions" size="small" empty-text="暂无版本快照">
            <el-table-column label="版本" width="90"><template #default="{ row }"><el-tag>v{{ row.version }}</el-tag></template></el-table-column>
            <el-table-column prop="note" label="备注"><template #default="{ row }">{{ row.note || '-' }}</template></el-table-column>
            <el-table-column label="时间"><template #default="{ row }">{{ (row.created_at || '').slice(0, 19).replace('T', ' ') }}</template></el-table-column>
            <el-table-column label="操作" width="100">
              <template #default="{ row }">
                <el-popconfirm :title="`回滚到 v${row.version}？将覆盖当前配置`" @confirm="rollback(row)">
                  <template #reference><el-button size="small">回滚</el-button></template>
                </el-popconfirm>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 模式编辑 -->
    <el-dialog v-model="modeOpen" :title="editMode ? '编辑人设（行为模式）' : '添加人设（行为模式）'" width="640px">
      <el-form :model="modeForm" label-position="top">
        <!-- 从模板快速套用人设：仅新增时提供（编辑已有模式时不显示，避免误覆盖已写好的内容） -->
        <el-form-item v-if="!editMode" label="从模板快速套用人设">
          <el-select
            v-model="pickedTemplateId"
            clearable
            filterable
            placeholder="选择一个人设模板（可选），选中后自动填入下方人设内容"
            style="width: 100%"
            @change="applyTemplate"
          >
            <el-option label="空白自定义" :value="0" />
            <el-option-group v-if="builtinTemplates.length" label="内置模板">
              <el-option
                v-for="t in builtinTemplates"
                :key="t.id"
                :value="t.id"
                :label="t.name"
              >
                <span>{{ t.name }}</span>
                <span class="opt-desc">{{ t.description }}</span>
              </el-option>
            </el-option-group>
            <el-option-group v-if="customTemplates.length" label="我的模板">
              <el-option
                v-for="t in customTemplates"
                :key="t.id"
                :value="t.id"
                :label="t.name"
              >
                <span>{{ t.name }}</span>
                <span class="opt-desc">{{ t.description }}</span>
              </el-option>
            </el-option-group>
          </el-select>
        </el-form-item>
        <el-form-item label="人设名称" required><el-input v-model="modeForm.name" placeholder="如：客服人设 / 审核人设 / 写作人设" /></el-form-item>
        <el-form-item label="人设内容（叠加在基础人设之后）"><el-input v-model="modeForm.system_prompt" type="textarea" :rows="7" placeholder="写清角色 / 语气 / 边界 / 输出格式；也可先用上方模板填充后再修改" /></el-form-item>
        <el-form-item label="知识库覆盖（留空=继承）">
          <el-select v-model="modeForm.kb_ids" multiple clearable style="width: 100%">
            <el-option v-for="k in kbs" :key="k.id" :value="k.id" :label="k.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="技能覆盖（留空=继承）">
          <el-select v-model="modeForm.skill_ids" multiple clearable style="width: 100%">
            <el-option v-for="s in skills" :key="s.id" :value="s.id" :label="s.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="设为默认"><el-switch v-model="modeForm.is_default" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="modeOpen = false">取消</el-button>
        <el-button type="primary" @click="submitMode">保存</el-button>
      </template>
    </el-dialog>

    <!-- 测试对话 -->
    <el-drawer v-model="testOpen" title="测试对话" size="520px">
      <div ref="testListRef" class="test-list">
        <p v-if="!testMsgs.length" class="text-secondary" style="font-size: 13px">输入消息测试该智能体（保存后生效）。</p>
        <div v-for="(m, i) in testMsgs" :key="i" class="test-msg">
          <el-tag size="small" :type="m.role === 'user' ? 'primary' : 'success'">{{ m.role === 'user' ? '你' : '智能体' }}</el-tag>
          <div class="test-body">
            <MarkdownBody v-if="m.role === 'assistant'" :content="m.content || (testStreaming ? '…' : '')" />
            <div v-else class="test-plain">{{ m.content }}</div>
          </div>
        </div>
      </div>
      <div class="test-composer">
        <el-input v-model="testInput" placeholder="输入测试消息" @keydown.enter.prevent="sendTest" />
        <el-button type="primary" :icon="Promotion" :loading="testStreaming" @click="sendTest">发送</el-button>
      </div>
    </el-drawer>
  </PageContainer>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, VideoPlay, Plus, Edit, Delete, Promotion } from '@element-plus/icons-vue'
import PageContainer from '@/components/PageContainer.vue'
import MarkdownBody from '@/components/MarkdownBody.vue'
import JsonSchemaForm from '@/components/JsonSchemaForm.vue'
import { agentApi, API_BASE, kbApi, personaApi, providerApi, skillApi, streamChat, toolApi, type Agent, type AgentMode, type KB, type ModelConfig, type PersonaTemplate, type Skill } from '@/api'
import { errMsg } from '@/api/http'

const route = useRoute()
const agentId = Number(route.params.id)

const BUILTIN_TOOLS = [
  { name: 'knowledge_retrieval', label: '知识库检索', perm: 'chat:use' },
  { name: 'http_request', label: 'HTTP 请求', perm: 'tool:invoke', write: true },
  { name: 'read_file', label: '读取文件', perm: 'file:read' },
  { name: 'list_conversation_files', label: '列出会话文件', perm: 'file:read' },
  { name: 'convert_file', label: '文件转文本', perm: 'file:read' },
  { name: 'generate_file', label: '生成文件', perm: 'file:write', write: true },
  { name: 'generate_office_doc', label: '生成办公文档（纪要/周报/待办/公文）', perm: 'file:write', write: true },
  { name: 'edit_file', label: '改写文件', perm: 'file:write', write: true },
  { name: 'convert_file_to', label: '转换格式', perm: 'file:write', write: true },
]

const tab = ref('basic')
const agent = ref<Agent | null>(null)
const kbs = ref<KB[]>([])
const configs = ref<ModelConfig[]>([])
const skills = ref<Skill[]>([])
const modes = ref<AgentMode[]>([])
const versions = ref<{ id: number; version: number; note?: string; created_at: string }[]>([])
const builtinState = reactive<Record<string, boolean>>({})
const adminEnabled = ref(false)
const adminToolsSel = ref<string[]>([])
const adminToolsPool = ref<string[]>([])

const form = reactive<any>({ name: '', description: '', system_prompt: '', greeting: '', visibility: 'private', model_config_id: undefined, kb_ids: [], skill_ids: [], temperature: 0.3, max_turns: 4 })
const skillParams = reactive<Record<string, any>>({})

const modeOpen = ref(false)
const editMode = ref<AgentMode | null>(null)
const modeForm = reactive<any>({ name: '', system_prompt: '', kb_ids: [], skill_ids: [], is_default: false })

// 人设模板（选择后填充提示词；仅新增模式时提供）
const templates = ref<PersonaTemplate[]>([])
const pickedTemplateId = ref<number>(0)
const builtinTemplates = computed(() => templates.value.filter((t) => t.is_builtin && t.enabled))
const customTemplates = computed(() => templates.value.filter((t) => !t.is_builtin && t.enabled))

/** 选中模板 → 预填名称与提示词（之后可自由编辑；不建立绑定关系） */
function applyTemplate(id: number | null) {
  if (!id) return
  const t = templates.value.find((x) => x.id === id)
  if (!t) return
  if (!modeForm.name?.trim()) modeForm.name = t.name
  modeForm.system_prompt = t.system_prompt
  ElMessage.success(`已套用模板「${t.name}」，可按需修改`)
}

async function loadTemplates() {
  try {
    templates.value = await personaApi.list({ enabled: true })
  } catch {
    templates.value = []
  }
}

// 基座人设也支持套模板
const baseTemplateId = ref<number>(0)

/** 选中模板 → 填入基座人设（有内容时先确认，避免误覆盖） */
function applyBaseTemplate(id: number | null | undefined) {
  if (!id) return
  const t = templates.value.find((x) => x.id === id)
  if (!t) return
  const fill = () => {
    form.system_prompt = t.system_prompt
    ElMessage.success(`已套用模板「${t.name}」`)
  }
  if (form.system_prompt?.trim()) {
    ElMessageBox.confirm('基座人设已有内容，套用模板将覆盖它。继续？', '覆盖确认', {
      confirmButtonText: '覆盖',
      cancelButtonText: '取消',
      type: 'warning',
    })
      .then(fill)
      .catch(() => {})
    return
  }
  fill()
}

const testOpen = ref(false)
const testMsgs = ref<{ role: string; content: string }[]>([])
const testInput = ref('')
const testStreaming = ref(false)
const testListRef = ref<HTMLElement>()

const chatConfigs = computed(() => configs.value.filter((c) => c.purpose === 'chat'))
const parentSkills = computed(() => skills.value.filter((s) => !s.parent_id))
const skillsWithParams = computed(() =>
  (form.skill_ids as number[])
    .map((id) => skills.value.find((s) => s.id === id))
    .filter((s): s is Skill => !!s && s.kind === 'prompt_pack' && !!s.params_schema),
)
function kindLabel(k: string) {
  return k === 'prompt_pack' ? '能力包' : k === 'collection' ? '技能集合' : '工具'
}

async function load() {
  try {
    const a = await agentApi.get(agentId)
    agent.value = a
    const tc = a.tool_config || {}
    for (const t of BUILTIN_TOOLS) builtinState[t.name] = tc.builtin?.[t.name]?.enabled ?? false
    adminEnabled.value = tc.admin?.enabled ?? false
    adminToolsSel.value = tc.admin?.tools ?? []
    Object.assign(form, {
      name: a.name, description: a.description || '', system_prompt: a.system_prompt,
      greeting: (a.config as any)?.greeting || '', visibility: a.visibility || 'private',
      model_config_id: a.model_config_id, kb_ids: a.kb_ids || [], skill_ids: a.skill_ids || [],
      temperature: (a.config as any)?.temperature ?? 0.3, max_turns: tc.max_turns ?? 4,
    })
    const params = (a.config as any)?.params || {}
    for (const k of Object.keys(params)) skillParams[k] = params[k]
    kbs.value = await kbApi.list()
    configs.value = await providerApi.configs()
    skills.value = await skillApi.list()
    modes.value = await agentApi.modes(agentId)
    versions.value = await agentApi.versions(agentId).catch(() => [])
    toolApi.adminList().then((list) => (adminToolsPool.value = list.map((x) => x.name))).catch(() => {})
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function save() {
  if (!form.name.trim()) { ElMessage.warning('请输入名称'); return }
  try {
    const prev = agent.value?.tool_config || {}
    const builtin: Record<string, any> = { ...(prev.builtin || {}) }
    for (const t of BUILTIN_TOOLS) builtin[t.name] = { ...(builtin[t.name] || {}), enabled: !!builtinState[t.name] }
    const toolConfig = { ...prev, builtin, admin: { ...(prev.admin || {}), enabled: !!adminEnabled.value, tools: adminToolsSel.value || [] }, max_turns: form.max_turns }
    await agentApi.update(agentId, {
      name: form.name, description: form.description, system_prompt: form.system_prompt, visibility: form.visibility,
      model_config_id: form.model_config_id, kb_ids: form.kb_ids, skill_ids: form.skill_ids,
      config: { temperature: form.temperature, greeting: form.greeting, params: { ...skillParams } },
      tool_config: toolConfig,
      // 乐观锁：带上本次加载时的版本，别人在这期间改过就返回 409 而不是覆盖掉
      row_version: agent.value?.row_version,
    } as any)
    ElMessage.success('已保存')
    load()
  } catch (e) { ElMessage.error(errMsg(e)) }
}

// 模式
function openAddMode() {
  editMode.value = null
  pickedTemplateId.value = 0
  Object.assign(modeForm, { name: '', system_prompt: '', kb_ids: [], skill_ids: [], is_default: false })
  modeOpen.value = true
}
function openEditMode(m: AgentMode) {
  editMode.value = m
  Object.assign(modeForm, { name: m.name, system_prompt: m.system_prompt, kb_ids: m.kb_ids || [], skill_ids: m.skill_ids || [], is_default: m.is_default })
  modeOpen.value = true
}
async function submitMode() {
  if (!modeForm.name.trim()) { ElMessage.warning('请输入人设名称'); return }
  try {
    if (editMode.value) await agentApi.updateMode(agentId, editMode.value.id, { ...modeForm })
    else await agentApi.createMode(agentId, { ...modeForm })
    ElMessage.success('已保存模式')
    modeOpen.value = false
    load()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function setDefaultMode(m: AgentMode) {
  try { await agentApi.update(agentId, { default_mode_id: m.id } as any); ElMessage.success('已设为默认模式'); load() } catch (e) { ElMessage.error(errMsg(e)) }
}
async function removeMode(m: AgentMode) {
  try { await agentApi.removeMode(agentId, m.id); load() } catch (e) { ElMessage.error(errMsg(e)) }
}

// 版本
async function snapshot() {
  try { const note = window.prompt('版本备注（可选）', '') ?? undefined; await agentApi.snapshot(agentId, note); ElMessage.success('已生成快照'); load() } catch (e) { ElMessage.error(errMsg(e)) }
}
async function rollback(v: any) {
  try { await agentApi.rollback(agentId, v.id); ElMessage.success('已回滚'); load() } catch (e) { ElMessage.error(errMsg(e)) }
}

// 测试
async function sendTest() {
  const text = testInput.value.trim()
  if (!text) return
  testInput.value = ''
  testMsgs.value = [...testMsgs.value, { role: 'user', content: text }, { role: 'assistant', content: '' }]
  testStreaming.value = true
  try {
    await streamChat(
      { kb_ids: agent.value?.kb_ids || [], message: text },
      {
        onDelta: (t) => {
          const c = [...testMsgs.value]
          const last = c.length - 1
          c[last] = { ...c[last], content: c[last].content + t }
          testMsgs.value = c
        },
        onError: (msg) => ElMessage.error(msg),
      },
      undefined,
      `${API_BASE}/agents/${agentId}/run`,
    )
  } catch (e) { ElMessage.error(errMsg(e)) }
  testStreaming.value = false
  nextTick(() => testListRef.value?.scrollTo({ top: 99999 }))
}

onMounted(async () => {
  await load()
  loadTemplates()
})
</script>

<style scoped>
.panel { padding: 18px 20px; }
.panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.p-title { font-size: 14px; font-weight: 600; color: var(--text-title); margin: 4px 0 10px; }
.tool-row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.tool-label { font-size: 13px; color: var(--text-body); }
.tool-sub { font-size: 13px; color: var(--text-secondary); margin-bottom: 6px; }
.skill-params { border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 14px; }
.sp-title { font-size: 13px; font-weight: 600; color: var(--text-title); margin-bottom: 10px; }
.test-list { height: calc(100vh - 200px); overflow-y: auto; margin-bottom: 12px; }
.test-msg { margin-bottom: 12px; }
.test-body { margin-top: 4px; }
.test-plain { white-space: pre-wrap; font-size: 14px; color: var(--text-body); }
.test-composer { display: flex; gap: 8px; }
/* 模板下拉里的说明文字 */
.opt-desc { float: right; color: var(--text-muted); font-size: 12px; margin-left: 12px; }
/* 人设区块 */
.persona-head { display: flex; align-items: flex-start; gap: 12px; width: 100%; margin-bottom: 8px; }
.persona-hint { flex: 1; font-size: 12.5px; color: var(--text-secondary); line-height: 1.6; }
.persona-hint.dim { color: var(--text-muted); margin-top: 6px; }
.persona-hint b { color: var(--text-title); }
</style>
