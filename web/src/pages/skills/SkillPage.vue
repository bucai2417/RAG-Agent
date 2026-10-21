<template>
  <PageContainer title="技能" subtitle="技能 = SKILL.md（提示词/说明）+ 可选脚本。从 .md / .zip / GitHub 导入，或手动创建能力包与 HTTP 工具，供智能体调用">
    <template #extra>
      <template v-if="tab === 'mine' && canEdit">
        <el-button :icon="Upload" @click="importOpen = true">导入技能包</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新建技能</el-button>
      </template>
    </template>

    <el-tabs v-model="tab" class="skill-tabs">
      <el-tab-pane label="我的技能" name="mine">
        <div class="app-card panel">
          <el-table :data="topSkills" v-loading="loading" row-key="id" :expand-row-keys="expandedKeys" @expand-change="onExpand">
            <el-table-column type="expand">
              <template #default="{ row }">
                <div class="sub-wrap">
                  <el-table v-if="childrenOf(row.id).length" :data="childrenOf(row.id)" size="small">
                    <el-table-column prop="name" label="子技能" width="200" />
                    <el-table-column prop="description" label="描述" show-overflow-tooltip />
                    <el-table-column label="状态" width="90">
                      <template #default="{ row: c }"><el-tag size="small" :type="c.status === 'active' ? 'success' : 'info'">{{ c.status === 'active' ? '启用' : '停用' }}</el-tag></template>
                    </el-table-column>
                    <el-table-column label="操作" width="160">
                      <template #default="{ row: c }">
                        <el-button size="small" @click="viewPackage(c.id)">查看</el-button>
                        <el-popconfirm v-if="canEdit" title="删除该子技能？" @confirm="removeSkill(c.id)">
                          <template #reference><el-button size="small" danger :icon="Delete" /></template>
                        </el-popconfirm>
                      </template>
                    </el-table-column>
                  </el-table>
                  <span v-else class="text-muted">（无子技能）</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="名称" min-width="200">
              <template #default="{ row }">
                <span class="sk-name">
                  <el-icon class="sk-ico"><Grid /></el-icon>
                  <b>{{ row.name }}</b>
                  <el-tag v-if="row.kind === 'collection'" size="small" type="info">集合</el-tag>
                </span>
              </template>
            </el-table-column>
            <el-table-column label="类型" width="110">
              <template #default="{ row }"><el-tag size="small" :type="kindMeta(row.kind).tag">{{ kindMeta(row.kind).label }}</el-tag></template>
            </el-table-column>
            <el-table-column label="来源" width="100">
              <template #default="{ row }"><el-tag size="small" :type="row.source === 'package' ? 'warning' : 'info'">{{ row.source === 'package' ? '技能包' : '手建' }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="description" label="描述" show-overflow-tooltip />
            <el-table-column label="操作" width="220">
              <template #default="{ row }">
                <el-button v-if="row.source === 'package'" size="small" @click="viewPackage(row.id)">查看包</el-button>
                <template v-if="row.kind !== 'collection' && canEdit">
                  <el-button size="small" :icon="Edit" @click="openEdit(row)" />
                  <el-popconfirm title="删除该技能？" @confirm="removeSkill(row.id)">
                    <template #reference><el-button size="small" danger :icon="Delete" /></template>
                  </el-popconfirm>
                </template>
                <el-popconfirm v-if="row.kind === 'collection' && canEdit" title="删除该集合（含全部子技能）？" @confirm="removeSkill(row.id)">
                  <template #reference><el-button size="small" danger :icon="Delete" /></template>
                </el-popconfirm>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!loading && !topSkills.length" description="暂无技能，点击右上角新建或导入技能包" :image-size="70" />
        </div>
      </el-tab-pane>

      <el-tab-pane label="技能市场" name="market" lazy>
        <SkillMarketPanel @installed="onMarketInstalled" />
      </el-tab-pane>
    </el-tabs>

    <!-- 新建 / 编辑技能 -->
    <el-dialog v-model="open" :title="editSkill ? '编辑技能' : '新建技能'" width="640px">
      <el-radio-group v-if="!editSkill" v-model="createMode" style="margin-bottom: 16px">
        <el-radio-button value="import">从 SKILL.md 导入 · 推荐</el-radio-button>
        <el-radio-button value="manual">手动创建（高级）</el-radio-button>
      </el-radio-group>

      <template v-if="createMode === 'import' && !editSkill">
        <p class="text-secondary" style="font-size: 12px; margin: 0 0 8px">
          粘贴 SKILL.md 内容（含 YAML frontmatter：name/description + 正文）：
        </p>
        <el-input v-model="mdText" type="textarea" :rows="12" placeholder="---&#10;name: my-skill&#10;description: ...&#10;---&#10;&#10;# 正文" @input="onMdChange" />
        <div v-if="mdPreview" class="md-preview">
          <div><b>解析结果：</b>{{ mdPreview.name }} —— {{ mdPreview.description }}</div>
          <div class="text-muted" style="font-size: 12px; margin-top: 4px">frontmatter：{{ mdPreview.has_frontmatter ? '有' : '无' }}</div>
        </div>
      </template>

      <el-form v-else :model="form" label-position="top">
        <el-form-item label="名称" required><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="form.description" type="textarea" :rows="2" /></el-form-item>
        <el-form-item label="类型">
          <el-select v-model="form.kind" style="width: 100%">
            <el-option value="prompt_pack" label="能力包（提示词 + 工具 + 知识库）" />
            <el-option value="tool" label="工具（HTTP 可调用）" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.kind === 'prompt_pack'" label="提示词模板"><el-input v-model="form.prompt_template" type="textarea" :rows="4" /></el-form-item>
        <template v-if="form.kind === 'tool'">
          <el-form-item label="HTTP URL"><el-input v-model="form.http_url" placeholder="https://..." /></el-form-item>
          <el-form-item label="方法">
            <el-select v-model="form.http_method" style="width: 140px">
              <el-option v-for="m in ['GET', 'POST', 'PUT', 'DELETE']" :key="m" :value="m" :label="m" />
            </el-select>
          </el-form-item>
        </template>
        <el-form-item :label="form.kind === 'tool' ? '参数 Schema（JSON）' : '参数 Schema（JSON，可选）'">
          <el-input v-model="form.params_json" type="textarea" :rows="5" placeholder='{"type":"object","properties":{}}' />
        </el-form-item>
        <el-form-item label="知识库范围（可选）">
          <el-select v-model="form.kb_ids" multiple style="width: 100%">
            <el-option v-for="k in kbs" :key="k.id" :value="k.id" :label="k.name" />
          </el-select>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="open = false">取消</el-button>
        <el-button v-if="createMode === 'import' && !editSkill" type="primary" :loading="importing" :icon="Upload" @click="doImportMarkdown">导入技能</el-button>
        <el-button v-else type="primary" @click="onCreate">保存</el-button>
      </template>
    </el-dialog>

    <!-- 导入技能包 -->
    <el-dialog v-model="importOpen" title="导入技能包" width="560px">
      <p class="text-secondary" style="font-size: 12px; margin: 0 0 12px">
        支持 Agent Skills 格式：包内需含 SKILL.md（YAML frontmatter: name/description + 正文），可选 scripts/ 目录。可上传 .zip / .md，或从 URL / GitHub 导入。
      </p>
      <el-button :icon="Upload" style="width: 100%" @click="zipRef?.click()">上传 .zip 技能包 / .md 文件</el-button>
      <input ref="zipRef" type="file" hidden accept=".zip,.md,.markdown" @change="onZipPick" />
      <div class="url-row">
        <el-input v-model="urlInput" placeholder="https://github.com/user/repo 或 zip 直链" />
        <el-button type="primary" @click="doImportUrl">导入</el-button>
      </div>
    </el-dialog>

    <!-- 技能包抽屉 -->
    <el-drawer v-model="pkgOpen" :title="`技能包：${pkg?.name || ''}`" size="560px">
      <template v-if="pkg">
        <div class="kv"><span class="k">来源类型</span><span class="v">{{ pkg.source_type }}</span></div>
        <div class="kv"><span class="k">来源</span><span class="v">{{ pkg.source_uri || '-' }}</span></div>
        <div class="kv"><span class="k">版本</span><span class="v">{{ pkg.version || '-' }}</span></div>
        <div class="kv"><span class="k">文件数</span><span class="v">{{ pkg.files?.length || 0 }}</span></div>
        <div class="kv">
          <span class="k">允许脚本执行</span>
          <el-switch :model-value="pkg.scripts_enabled" :disabled="!pkg.entry_scripts?.length" @change="(v: any) => toggleScripts(v)" />
        </div>
        <div v-if="canEdit" class="pkg-ops">
          <el-button :icon="Upload" @click="upgradeRef?.click()">更新技能包（上传 zip）</el-button>
          <input ref="upgradeRef" type="file" hidden accept=".zip" @change="onUpgradePick" />
          <el-button :icon="Link" @click="upgradeOpen = true">从 URL 更新</el-button>
          <el-button v-if="hasDeps" :icon="Download" :loading="installingDeps" @click="installDeps">
            安装依赖<el-badge v-if="needsInstall" is-dot style="margin-left: 4px" />
          </el-button>
          <el-button :icon="Document" @click="showManifestTemplate = true">清单格式</el-button>
        </div>

        <h4 class="p-title">脚本</h4>
        <el-empty v-if="!pkg.entry_scripts?.length" description="无脚本" :image-size="50" />
        <div v-for="s in pkg.entry_scripts || []" :key="s.name" class="script-row">
          <div class="script-info">
            <code>{{ s.name }}</code>
            <el-tag v-if="s.runtime" size="small" :type="runtimeTag(s.runtime)" class="rt-tag">{{ s.runtime }}</el-tag>
            <div class="text-muted" style="font-size: 12px">{{ s.description || s.path }}</div>
          </div>
          <el-button v-if="canRun" size="small" :disabled="!pkg.scripts_enabled" @click="openRunScript(s)">试跑</el-button>
        </div>

        <h4 class="p-title">文件清单（点击查看/编辑）</h4>
        <div v-for="f in pkg.files || []" :key="f.path" class="file-row" @click="openFile(f.path)">
          <el-icon class="file-ico"><Document /></el-icon>
          <span class="file-path">{{ f.path }}</span>
          <span class="text-muted">（{{ f.size }} B）</span>
        </div>
      </template>
    </el-drawer>

    <!-- 从 URL 更新 -->
    <el-dialog v-model="upgradeOpen" title="从 URL 更新技能包" width="480px">
      <p class="text-secondary" style="font-size: 12px">填入技能包 zip 直链或 GitHub 仓库地址，平台会拉取并覆盖当前技能包。</p>
      <el-input v-model="upgradeUrl" placeholder="https://github.com/user/skill-repo 或 https://.../skill.zip" />
      <template #footer>
        <el-button @click="upgradeOpen = false">取消</el-button>
        <el-button type="primary" :loading="upgradeBusy" @click="doUpgradeUrl">更新</el-button>
      </template>
    </el-dialog>

    <!-- 清单格式说明 -->
    <el-dialog v-model="showManifestTemplate" title="技能清单 skill.yaml（可选）" width="640px">
      <p class="text-secondary" style="font-size: 12px">
        在技能包根目录放 skill.yaml 即可声明入口、参数约定、产物与依赖。不写则按扩展名/目录自动推断。
      </p>
      <pre class="run-result" style="max-height: 400px">{{ pkg?.manifest_template || '' }}</pre>
      <template #footer>
        <el-button @click="showManifestTemplate = false">关闭</el-button>
        <el-button type="primary" :icon="CopyDocument" @click="copyManifestTemplate">复制</el-button>
      </template>
    </el-dialog>

    <!-- 试跑脚本 -->
    <el-dialog v-model="runOpen" :title="`试跑脚本：${runScript}`" width="560px">
      <p class="text-secondary" style="font-size: 12px">
        Python 脚本从 stdin 读 JSON 参数；Node/Shell 脚本额外支持下方「命令行参数」。
      </p>
      <el-input v-model="runScriptArgs" type="textarea" :rows="4" placeholder='{"key":"value"}' class="mono-in" />
      <el-input v-model="runExtraArgs" placeholder="命令行参数（空格分隔），如：deck.json -o out" class="mono-in" style="margin-top: 8px" />
      <el-button type="primary" :icon="VideoPlay" :loading="running" style="margin-top: 12px" @click="doRunScript">执行</el-button>
      <pre v-if="runResult" class="run-result">{{ runResult }}</pre>
    </el-dialog>

    <!-- 文件查看/编辑 -->
    <el-drawer v-model="fileOpen" :title="`文件：${fileView?.path || ''}`" size="720px">
      <template v-if="fileView">
        <div v-if="!fileView.binary" class="file-ops">
          <el-button :icon="CopyDocument" @click="copyFile">复制</el-button>
          <el-button type="primary" :disabled="!fileView.dirty" @click="saveFile">保存</el-button>
        </div>
        <div v-if="fileView.binary" class="text-secondary">二进制文件，无法预览（{{ fileView.path }}）</div>
        <el-input v-else v-model="fileView.content" type="textarea" class="mono-in file-content" />
      </template>
    </el-drawer>
  </PageContainer>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Upload, Edit, Delete, Grid, Document, Link, VideoPlay, CopyDocument, Download } from '@element-plus/icons-vue'
import PageContainer from '@/components/PageContainer.vue'
import SkillMarketPanel from './SkillMarketPanel.vue'
import { kbApi, skillApi, type KB, type Skill, type SkillPackageInfo } from '@/api'
import { errMsg } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const canEdit = computed(() => auth.hasPermission('skill:edit') || auth.hasPermission('skill:manage'))
const canRun = computed(() => auth.hasPermission('skill:run') || canEdit.value)

const tab = ref<'mine' | 'market'>('mine')

const skills = ref<Skill[]>([])
const kbs = ref<KB[]>([])
const loading = ref(true)
const expandedKeys = ref<number[]>([])

const open = ref(false)
const editSkill = ref<Skill | null>(null)
const createMode = ref('import')
const mdText = ref('')
const mdPreview = ref<{ name: string; description: string; has_frontmatter: boolean } | null>(null)
const importing = ref(false)
const form = reactive<any>({ name: '', description: '', kind: 'prompt_pack', prompt_template: '', http_url: '', http_method: 'GET', params_json: '', kb_ids: [] })
let mdTimer: any = null

const importOpen = ref(false)
const urlInput = ref('')
const zipRef = ref<HTMLInputElement>()

const pkgOpen = ref(false)
const pkg = ref<SkillPackageInfo | null>(null)
const pkgSkillId = ref<number | null>(null)
const upgradeRef = ref<HTMLInputElement>()
const upgradeOpen = ref(false)
const upgradeUrl = ref('')
const upgradeBusy = ref(false)

const runOpen = ref(false)
const runScript = ref('')
const runScriptArgs = ref('')
const runExtraArgs = ref('')
const runResult = ref('')
const running = ref(false)
const installingDeps = ref(false)
const showManifestTemplate = ref(false)

function copyManifestTemplate() {
  const text = pkg.value?.manifest_template || ''
  if (!text) return
  navigator.clipboard?.writeText(text).then(
    () => ElMessage.success('已复制'),
    () => ElMessage.warning('复制失败，请手动选择'),
  )
}

const hasNodeManifest = computed(() =>
  pkg.value?.capabilities?.has_node_manifest ?? (pkg.value?.files || []).some((f) => f.path === 'package.json'))
const hasDeps = computed(() =>
  pkg.value?.capabilities?.has_node_manifest || pkg.value?.capabilities?.has_python_reqs)
const needsInstall = computed(() => pkg.value?.capabilities?.needs_node_install === true)
function runtimeTag(rt: string) {
  return rt === 'python' ? 'primary' : rt === 'node' ? 'success' : rt === 'sh' ? 'warning' : 'info'
}

const fileOpen = ref(false)
const fileView = ref<{ path: string; content: string; binary: boolean; dirty?: boolean } | null>(null)

const topSkills = computed(() => skills.value.filter((s) => !s.parent_id))
function childrenOf(id: number) { return skills.value.filter((s) => s.parent_id === id) }
function kindMeta(k: string) {
  return k === 'prompt_pack' ? { tag: 'primary', label: '能力包' }
    : k === 'tool' ? { tag: 'warning', label: '工具' }
      : k === 'collection' ? { tag: 'info', label: '技能集合' }
        : { tag: 'info', label: k }
}
function onExpand(row: Skill, expanded: Skill[]) {
  void row
  expandedKeys.value = expanded.map((r) => r.id)
}

async function load() {
  loading.value = true
  try {
    skills.value = await skillApi.list()
    kbs.value = await kbApi.list().catch(() => [])
  } catch (e) { ElMessage.error(errMsg(e)) } finally { loading.value = false }
}
function openCreate() {
  editSkill.value = null
  createMode.value = 'import'
  mdText.value = ''
  mdPreview.value = null
  Object.assign(form, { name: '', description: '', kind: 'prompt_pack', prompt_template: '', http_url: '', http_method: 'GET', params_json: '', kb_ids: [] })
  open.value = true
}
function openEdit(s: Skill) {
  editSkill.value = s
  createMode.value = 'manual'
  Object.assign(form, {
    name: s.name, description: s.description || '', kind: s.kind, prompt_template: s.prompt_template || '',
    http_url: (s as any).tool_def?.http?.url || '', http_method: (s as any).tool_def?.http?.method || 'GET',
    params_json: s.kind === 'tool' ? JSON.stringify((s as any).tool_def?.parameters || {}, null, 2) : (s.params_schema ? JSON.stringify(s.params_schema, null, 2) : ''),
    kb_ids: s.kb_ids || [],
  })
  open.value = true
}
function onMdChange(v: string) {
  if (mdTimer) clearTimeout(mdTimer)
  if (!v.trim()) { mdPreview.value = null; return }
  mdTimer = setTimeout(async () => {
    try { mdPreview.value = await skillApi.parseMarkdown(v) } catch { /* ignore */ }
  }, 400)
}
async function doImportMarkdown() {
  if (!mdText.value.trim()) { ElMessage.warning('请先粘贴 SKILL.md 内容'); return }
  importing.value = true
  try {
    const r = await skillApi.importMarkdown(mdText.value)
    if (r.duplicate) ElMessage.warning(`技能包已存在（技能 #${r.existing_skill_id}「${r.existing_skill_name}」），未重复导入`)
    else ElMessage.success(`导入成功：${r.name}（${r.files} 个文件）`)
    open.value = false
    load()
  } catch (e) { ElMessage.error(errMsg(e)) } finally { importing.value = false }
}
async function onCreate() {
  if (!form.name.trim()) { ElMessage.warning('请输入名称'); return }
  const payload: any = { name: form.name, description: form.description, kind: form.kind, kb_ids: form.kb_ids }
  if (form.kind === 'tool') {
    let parameters: any = { type: 'object', properties: {} }
    if (form.params_json) { try { parameters = JSON.parse(form.params_json) } catch { ElMessage.error('参数 Schema 不是合法 JSON'); return } }
    payload.tool_def = { name: form.name, description: form.description || form.name, parameters, impl: 'http', http: { url: form.http_url, method: form.http_method || 'GET', headers: {} } }
  } else {
    if (form.params_json) { try { payload.params_schema = JSON.parse(form.params_json) } catch { ElMessage.error('参数 Schema 不是合法 JSON'); return } }
    else payload.params_schema = null
    payload.prompt_template = form.prompt_template
  }
  try {
    if (editSkill.value) await skillApi.update(editSkill.value.id, payload)
    else await skillApi.create(payload)
    ElMessage.success(editSkill.value ? '已保存' : '创建成功')
    open.value = false
    load()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function removeSkill(id: number) {
  try { await skillApi.remove(id); ElMessage.success('已删除'); load() } catch (e) { ElMessage.error(errMsg(e)) }
}

// ---- 导入技能包 ----
async function onZipPick(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (!f) return
  try {
    const r = await skillApi.importUpload(f)
    await handleImportResult({ ...r, __collection: () => skillApi.importCollectionUpload(f) }, (subpath) => skillApi.importUpload(f, subpath))
  } catch (e) { ElMessage.error(errMsg(e)) }
  if (zipRef.value) zipRef.value.value = ''
}
async function doImportUrl() {
  const url = urlInput.value.trim()
  if (!url) return
  try {
    const r = await skillApi.importUrl(url)
    await handleImportResult({ ...r, __collection: () => skillApi.importCollection(url) }, (subpath) => skillApi.importUrl(url, subpath))
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function handleImportResult(r: any, retry: (subpath?: string) => Promise<any>) {
  if (r.duplicate) { ElMessage.warning(`技能包已存在（技能 #${r.existing_skill_id}「${r.existing_skill_name}」），未重复导入`); return }
  if (r.multi_skill) {
    const subs: string[] = r.sub_skills || []
    try {
      const { value } = await ElMessageBox.prompt(`该仓库含 ${subs.length} 个子技能。可整体导入为「技能集合」，或在下方填入一个子技能名单独安装（留空=整体导入）：`, '多技能仓库', { inputPlaceholder: '（可选）子技能名', confirmButtonText: '确定' })
      const r2 = value && value.trim() ? await retry(value.trim()) : await r.__collection?.()
      if (r2) {
        if (r2.duplicate) ElMessage.warning('技能包已存在，未重复导入')
        else if (r2.collection) ElMessage.success(`已导入技能集合「${r2.name}」（${r2.count} 个子技能）`)
        else ElMessage.success(`导入成功：${r2.name}`)
      }
    } catch { /* cancelled */ }
    importOpen.value = false
    load()
    return
  }
  if (r.collection) ElMessage.success(`已导入技能集合「${r.name}」（${r.count} 个子技能）`)
  else ElMessage.success(`导入成功：${r.name}（${r.files} 个文件）`)
  importOpen.value = false
  urlInput.value = ''
  load()
}

// ---- 技能包 ----
async function viewPackage(skillId: number) {
  try {
    pkg.value = await skillApi.getPackage(skillId)
    pkgSkillId.value = skillId
    pkgOpen.value = true
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function toggleScripts(enabled: boolean) {
  if (!pkgSkillId.value) return
  try {
    await skillApi.toggleScripts(pkgSkillId.value, enabled)
    if (pkg.value) pkg.value.scripts_enabled = enabled
    ElMessage.success(enabled ? '已允许脚本执行' : '已禁用脚本执行')
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function onUpgradePick(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (!f || !pkgSkillId.value) return
  try {
    await skillApi.upgradeUpload(pkgSkillId.value, f)
    ElMessage.success('已更新技能包')
    pkg.value = await skillApi.getPackage(pkgSkillId.value)
    load()
  } catch (e) { ElMessage.error(errMsg(e)) }
  if (upgradeRef.value) upgradeRef.value.value = ''
}
async function doUpgradeUrl() {
  if (!pkgSkillId.value || !upgradeUrl.value.trim()) return
  upgradeBusy.value = true
  try {
    await skillApi.upgradeUrl(pkgSkillId.value, upgradeUrl.value.trim())
    ElMessage.success('已从 URL 更新技能包')
    upgradeOpen.value = false
    pkg.value = await skillApi.getPackage(pkgSkillId.value)
    load()
  } catch (e) { ElMessage.error(errMsg(e)) } finally { upgradeBusy.value = false }
}
function openRunScript(s: { name: string }) {
  runScript.value = s.name
  runScriptArgs.value = ''
  runExtraArgs.value = ''
  runResult.value = ''
  runOpen.value = true
}
async function doRunScript() {
  if (!pkgSkillId.value || !runScript.value) return
  let args: any = {}
  try { args = runScriptArgs.value.trim() ? JSON.parse(runScriptArgs.value) : {} } catch { ElMessage.error('参数不是合法 JSON'); return }
  const extra = runExtraArgs.value.trim() ? runExtraArgs.value.trim().split(/\s+/) : []
  running.value = true
  try {
    const r = await skillApi.runScript(pkgSkillId.value, runScript.value, args, extra)
    runResult.value = r.content || ''
    if (r.ok) ElMessage.success('执行成功')
    else ElMessage.warning('脚本返回错误')
  } catch (e) { runResult.value = '失败：' + errMsg(e) } finally { running.value = false }
}
async function installDeps() {
  if (!pkgSkillId.value) return
  installingDeps.value = true
  try {
    const r = await skillApi.installDeps(pkgSkillId.value)
    if (r.ok) ElMessage.success(r.message || '依赖安装完成')
    else ElMessage.error(r.message || '安装失败')
    runResult.value = r.log || ''
    runOpen.value = true
    runScript.value = 'npm ci'
  } catch (e) { ElMessage.error(errMsg(e)) } finally { installingDeps.value = false }
}
async function openFile(path: string) {
  if (!pkgSkillId.value) return
  try {
    const r = await skillApi.readPackageFile(pkgSkillId.value, path)
    fileView.value = { path, content: r.content || '', binary: r.binary, dirty: false }
    fileOpen.value = true
  } catch (e) { ElMessage.error(errMsg(e)) }
}
function copyFile() {
  if (fileView.value?.content) { navigator.clipboard?.writeText(fileView.value.content); ElMessage.success('已复制') }
}
async function saveFile() {
  if (!fileView.value || !pkgSkillId.value) return
  try {
    await skillApi.writePackageFile(pkgSkillId.value, fileView.value.path, fileView.value.content)
    fileView.value.dirty = false
    ElMessage.success('已保存')
  } catch (e) { ElMessage.error(errMsg(e)) }
}

onMounted(load)

/** 市场安装成功：刷新「我的技能」列表，并提示用户切过去看 */
function onMarketInstalled() {
  void load()
}
</script>

<style scoped>
.panel { padding: 18px 20px; }
.sub-wrap { padding: 4px 12px; }
.sk-name { display: inline-flex; align-items: center; gap: 8px; }
.sk-ico { color: var(--color-primary); }
.md-preview { margin-top: 10px; padding: 10px 12px; background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 8px; font-size: 13px; }
.url-row { display: flex; gap: 8px; margin-top: 12px; }
.kv { display: flex; align-items: center; gap: 8px; padding: 7px 0; border-bottom: 1px solid var(--border-subtle); font-size: 13px; }
.kv .k { width: 110px; color: var(--text-secondary); }
.kv .v { color: var(--text-body); }
.pkg-ops { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.p-title { font-size: 14px; font-weight: 600; color: var(--text-title); margin: 16px 0 8px; }
.script-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--border-subtle); }
.script-info { flex: 1; min-width: 0; }
.script-info code { font-family: monospace; font-size: 12.5px; }
.rt-tag { margin-left: 6px; vertical-align: middle; }
.file-row { display: flex; align-items: center; gap: 6px; padding: 6px 4px; border-radius: 6px; cursor: pointer; font-family: monospace; font-size: 13px; }
.file-row:hover { background: var(--bg-subtle); }
.file-ico { color: var(--color-primary); }
.file-path { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mono-in :deep(.el-textarea__inner) { font-family: monospace; font-size: 12px; }
.file-content :deep(.el-textarea__inner) { height: calc(100vh - 160px); }
.file-ops { display: flex; gap: 8px; margin-bottom: 10px; }
.run-result { margin-top: 12px; max-height: 280px; overflow: auto; background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 6px; padding: 10px; font-size: 12px; white-space: pre-wrap; }
</style>
