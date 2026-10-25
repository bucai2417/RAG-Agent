<template>
  <PageContainer
    title="组织管理"
    subtitle="维护多个组织（租户）与各自的资源配额。0 表示不限；停用的组织不能再登录与注册"
  >
    <template #extra>
      <el-button v-if="isPlatformAdmin" type="primary" :icon="Plus" @click="openCreate">新建组织</el-button>
    </template>

    <!-- 当前组织：所有人都能看到自己的组织与配额用量 -->
    <div class="app-card panel mine">
      <div class="mine-head">
        <div>
          <b class="mine-name">{{ me?.name || '—' }}</b>
          <el-tag size="small" class="slug-tag">{{ me?.slug }}</el-tag>
          <el-tag v-if="me?.status && me.status !== 'active'" size="small" type="danger">
            {{ statusLabel(me.status) }}
          </el-tag>
        </div>
        <div class="mine-ops">
          <el-button v-if="canManageQuota" size="small" :keyboard="false" @click="openQuota">调整配额</el-button>
        </div>
      </div>
      <div class="quotas">
        <div class="quota">
          <span class="q-label">成员</span>
          <b class="q-val">{{ me?.users ?? 0 }}</b>
          <span class="q-max">/ {{ quotaText(me?.max_users) }}</span>
        </div>
        <div class="quota">
          <span class="q-label">文档</span>
          <b class="q-val">{{ me?.documents ?? 0 }}</b>
          <span class="q-max">/ {{ quotaText(me?.max_documents) }}</span>
        </div>
        <div class="quota">
          <span class="q-label">存储</span>
          <b class="q-val">{{ bytes(me?.storage_bytes) }}</b>
          <span class="q-max">/ {{ me?.max_storage_bytes ? bytes(me.max_storage_bytes) : '不限' }}</span>
        </div>
      </div>

      <!-- 邀请码：本组织管理员（tenant:invite）可见 -->
      <div v-if="canInvite" class="invite">
        <div class="invite-head">
          <b>邀请入组</b>
          <span class="invite-hint">
            凭邀请链接注册会直接落到本组织；轮换后旧链接立即失效
          </span>
        </div>
        <div v-if="invite.enabled" class="invite-row">
          <el-input :model-value="inviteUrl" readonly size="small" />
          <el-button size="small" @click="copyInvite">复制链接</el-button>
          <el-button size="small" @click="rotateInvite">重新生成</el-button>
          <el-popconfirm title="关闭邀请制？之后只能由管理员代建账号" @confirm="clearInvite">
            <template #reference><el-button size="small" danger>关闭</el-button></template>
          </el-popconfirm>
        </div>
        <div v-else class="invite-row">
          <span class="invite-off">当前为「管理员代建账号」模式，未开放自助入组</span>
          <el-button size="small" type="primary" @click="rotateInvite">生成邀请链接</el-button>
        </div>
      </div>
    </div>

    <!-- 全部组织：仅平台管理员可见 -->
    <div v-if="isPlatformAdmin" class="app-card panel">
      <div class="list-head">
        <b>全部组织（{{ rows.length }}）</b>
        <span class="list-hint">跨组织操作只对「平台管理员」开放（超级管理员的 platform 授予）</span>
      </div>
      <el-table :data="rows" row-key="id">
        <el-table-column label="组织" min-width="200">
          <template #default="{ row }">
            <div class="org-cell">
              <b>{{ row.name }}</b>
              <span class="org-slug">{{ row.slug }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTag(row.status)">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="成员" width="110" align="center">
          <template #default="{ row }">{{ row.users }} / {{ quotaText(row.max_users) }}</template>
        </el-table-column>
        <el-table-column label="文档" width="110" align="center">
          <template #default="{ row }">{{ row.documents }} / {{ quotaText(row.max_documents) }}</template>
        </el-table-column>
        <el-table-column label="存储" width="150" align="center">
          <template #default="{ row }">
            {{ bytes(row.storage_bytes) }} / {{ row.max_storage_bytes ? bytes(row.max_storage_bytes) : '不限' }}
          </template>
        </el-table-column>
        <el-table-column label="套餐" width="100">
          <template #default="{ row }">{{ row.plan || 'default' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-popconfirm
              v-if="row.status !== 'archived' && row.id !== me?.id"
              title="归档后该组织成员将无法登录（数据保留）"
              @confirm="archive(row)"
            >
              <template #reference><el-button size="small" danger>归档</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 新建组织 -->
    <el-dialog v-model="open" title="新建组织" width="560px">
      <el-form :model="form" label-position="top">
        <el-form-item label="组织名称" required>
          <el-input v-model="form.name" placeholder="如：某某科技有限公司" />
        </el-form-item>
        <el-form-item label="组织标识（slug）" required>
          <el-input v-model="form.slug" placeholder="小写字母/数字/连字符，如 acme" />
          <div class="extra">用于注册与邀请路由，创建后不可更改</div>
        </el-form-item>
        <el-form-item label="套餐">
          <el-input v-model="form.plan" placeholder="default" />
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="成员上限">
              <el-input-number v-model="form.max_users" :min="0" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="文档上限">
              <el-input-number v-model="form.max_documents" :min="0" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="存储上限（MB）">
              <el-input-number v-model="form.max_storage_mb" :min="0" style="width: 100%" />
            </el-form-item>
          </el-col>
        </el-row>
        <div class="extra" style="margin: -6px 0 12px">配额填 0 表示不限制；存储按 1 MB = 1048576 字节换算</div>

        <el-divider content-position="left">首位管理员（可选）</el-divider>
        <div class="extra" style="margin-bottom: 10px">
          不填则组织建好后没人能登录，需要另外由管理员代建账号
        </div>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="用户名">
              <el-input v-model="form.admin_username" placeholder="留空则不创建" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="初始密码">
              <el-input v-model="form.admin_password" type="password" show-password />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="备注">
          <el-input v-model="form.note" type="textarea" :rows="2" maxlength="512" show-word-limit />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="open = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 编辑组织 -->
    <el-dialog v-model="open2" title="编辑组织" width="520px">
      <el-form :model="editForm" label-position="top">
        <el-form-item label="组织名称"><el-input v-model="editForm.name" /></el-form-item>
        <el-form-item label="状态">
          <el-select v-model="editForm.status" style="width: 100%">
            <el-option label="正常" value="active" />
            <el-option label="停用（禁止登录与注册）" value="suspended" />
            <el-option label="归档（视为只读归档）" value="archived" />
          </el-select>
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="成员上限">
              <el-input-number v-model="editForm.max_users" :min="0" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="文档上限">
              <el-input-number v-model="editForm.max_documents" :min="0" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="存储上限（MB）">
              <el-input-number v-model="editForm.max_storage_mb" :min="0" style="width: 100%" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="备注">
          <el-input v-model="editForm.note" type="textarea" :rows="2" maxlength="512" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="open2 = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 调整本组织配额 -->
    <el-dialog v-model="open3" title="调整本组织配额" width="440px">
      <el-form :model="quotaForm" label-position="top">
        <el-form-item label="成员上限"><el-input-number v-model="quotaForm.max_users" :min="0" style="width: 100%" /></el-form-item>
        <el-form-item label="文档上限"><el-input-number v-model="quotaForm.max_documents" :min="0" style="width: 100%" /></el-form-item>
        <el-form-item label="存储上限（MB）">
          <el-input-number v-model="quotaForm.max_storage_mb" :min="0" style="width: 100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="open3 = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitQuota">保存</el-button>
      </template>
    </el-dialog>
  </PageContainer>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import PageContainer from '@/components/PageContainer.vue'
import { tenantApi, type TenantMe, type TenantRow } from '@/api'
import { errMsg } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const MB = 1024 * 1024
const auth = useAuthStore()

const me = ref<TenantMe | null>(null)
const rows = ref<TenantRow[]>([])
const invite = reactive({ enabled: false, code: '' as string | null })

const isPlatformAdmin = computed(() => !!me.value?.is_platform_admin)
// 配额是资源边界（平台侧），邀请入组是本组织日常运营——两者权限分开
const canManageQuota = computed(() => auth.hasPermission('tenant:manage'))
const canInvite = computed(() => auth.hasPermission('tenant:invite') || canManageQuota.value)

const open = ref(false)
const open2 = ref(false)
const open3 = ref(false)
const saving = ref(false)
const editing = ref<TenantRow | null>(null)

const form = reactive({
  name: '', slug: '', plan: 'default',
  max_users: 0, max_documents: 0, max_storage_mb: 0,
  note: '', admin_username: '', admin_password: '',
})
const editForm = reactive({
  name: '', status: 'active', max_users: 0, max_documents: 0, max_storage_mb: 0, note: '',
})
const quotaForm = reactive({ max_users: 0, max_documents: 0, max_storage_mb: 0 })

const inviteUrl = computed(() =>
  invite.code ? `${window.location.origin}/register?invite=${encodeURIComponent(invite.code)}` : '',
)

function quotaText(n?: number) { return n && n > 0 ? String(n) : '不限' }
function bytes(n?: number) {
  const v = n || 0
  if (v <= 0) return '0 B'
  if (v < 1024) return `${v} B`
  if (v < MB) return `${(v / 1024).toFixed(1)} KB`
  if (v < 1024 * MB) return `${(v / MB).toFixed(1)} MB`
  return `${(v / (1024 * MB)).toFixed(2)} GB`
}
function statusLabel(s?: string) {
  return ({ active: '正常', suspended: '停用', archived: '归档' } as Record<string, string>)[s || ''] || s || '—'
}
function statusTag(s?: string) {
  return ({ active: 'success', suspended: 'warning', archived: 'info' } as Record<string, string>)[s || ''] || 'info'
}
function mbToBytes(mb: number) { return Math.max(0, Math.round((mb || 0) * MB)) }
function bytesToMb(n?: number) { return Math.round((n || 0) / MB) }

async function load() {
  try {
    me.value = await tenantApi.me()
    if (canInvite.value) {
      const ic = await tenantApi.getInviteCode()
      invite.enabled = ic.enabled
      invite.code = ic.invite_code
    }
    if (me.value.is_platform_admin) rows.value = await tenantApi.list()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

function openCreate() {
  Object.assign(form, {
    name: '', slug: '', plan: 'default',
    max_users: 0, max_documents: 0, max_storage_mb: 0,
    note: '', admin_username: '', admin_password: '',
  })
  open.value = true
}

async function submitCreate() {
  if (!form.name.trim()) { ElMessage.warning('请填写组织名称'); return }
  if (!form.slug.trim()) { ElMessage.warning('请填写组织标识'); return }
  if (form.admin_username && !form.admin_password) {
    ElMessage.warning('填写了管理员用户名就必须填初始密码')
    return
  }
  saving.value = true
  try {
    await tenantApi.create({
      name: form.name.trim(),
      slug: form.slug.trim(),
      plan: form.plan || 'default',
      max_users: form.max_users,
      max_documents: form.max_documents,
      max_storage_bytes: mbToBytes(form.max_storage_mb),
      note: form.note || null,
      admin_username: form.admin_username || null,
      admin_password: form.admin_password || null,
    })
    ElMessage.success('组织已创建')
    open.value = false
    load()
  } catch (e) { ElMessage.error(errMsg(e)) } finally { saving.value = false }
}

function openEdit(row: TenantRow) {
  editing.value = row
  Object.assign(editForm, {
    name: row.name,
    status: row.status || 'active',
    max_users: row.max_users || 0,
    max_documents: row.max_documents || 0,
    max_storage_mb: bytesToMb(row.max_storage_bytes),
    note: row.note || '',
  })
  open2.value = true
}

async function submitEdit() {
  if (!editing.value) return
  saving.value = true
  try {
    await tenantApi.update(editing.value.id, {
      name: editForm.name,
      status: editForm.status,
      max_users: editForm.max_users,
      max_documents: editForm.max_documents,
      max_storage_bytes: mbToBytes(editForm.max_storage_mb),
      note: editForm.note || null,
    })
    ElMessage.success('已保存')
    open2.value = false
    load()
  } catch (e) { ElMessage.error(errMsg(e)) } finally { saving.value = false }
}

async function archive(row: TenantRow) {
  try {
    await tenantApi.archive(row.id)
    ElMessage.success('已归档')
    load()
  } catch (e) { ElMessage.error(errMsg(e)) }
}

function openQuota() {
  Object.assign(quotaForm, {
    max_users: me.value?.max_users || 0,
    max_documents: me.value?.max_documents || 0,
    max_storage_mb: bytesToMb(me.value?.max_storage_bytes),
  })
  open3.value = true
}

async function submitQuota() {
  saving.value = true
  try {
    await tenantApi.updateMyQuota({
      max_users: quotaForm.max_users,
      max_documents: quotaForm.max_documents,
      max_storage_bytes: mbToBytes(quotaForm.max_storage_mb),
    })
    ElMessage.success('配额已更新')
    open3.value = false
    load()
  } catch (e) { ElMessage.error(errMsg(e)) } finally { saving.value = false }
}

async function rotateInvite() {
  try {
    const r = await tenantApi.rotateInviteCode()
    invite.enabled = r.enabled
    invite.code = r.invite_code
    ElMessage.success('邀请码已生成，旧链接已失效')
  } catch (e) { ElMessage.error(errMsg(e)) }
}

async function clearInvite() {
  try {
    await tenantApi.clearInviteCode()
    invite.enabled = false
    invite.code = null
    ElMessage.success('已关闭邀请制')
  } catch (e) { ElMessage.error(errMsg(e)) }
}

async function copyInvite() {
  try {
    await navigator.clipboard.writeText(inviteUrl.value)
    ElMessage.success('链接已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选取链接')
  }
}

onMounted(load)
</script>

<style scoped>
.panel { padding: 16px; margin-bottom: 16px; }
.mine-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.mine-name { font-size: 15px; }
.slug-tag { margin-left: 8px; color: var(--text-secondary); }
.quotas { display: flex; gap: 32px; margin-top: 14px; flex-wrap: wrap; }
.quota { display: flex; align-items: baseline; gap: 6px; }
.q-label { color: var(--text-secondary); font-size: 13px; }
.q-val { font-size: 15px; }
.q-max { color: var(--text-secondary); font-size: 12px; }
.invite { margin-top: 18px; padding-top: 14px; border-top: 1px dashed var(--border); }
.invite-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; }
.invite-hint, .invite-off { color: var(--text-secondary); font-size: 12.5px; }
.invite-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.list-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 12px; gap: 10px; }
.list-hint { color: var(--text-secondary); font-size: 12.5px; }
.org-cell { display: flex; flex-direction: column; }
.org-slug { color: var(--text-secondary); font-size: 12px; }
.extra { color: var(--text-secondary); font-size: 12px; line-height: 1.5; }
</style>
