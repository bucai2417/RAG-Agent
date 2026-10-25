<template>
  <div class="reg-wrap">
    <div class="reg-card">
      <div class="rc-head">
        <div class="rc-logo">R</div>
        <div>
          <h2 class="rc-title">注册账号</h2>
          <p class="rc-sub">提交后需管理员审核通过才能登录</p>
        </div>
      </div>

      <el-alert
        v-if="closed"
        title="系统当前未开放自助注册"
        description="请联系管理员为你开通账号。"
        type="info"
        :closable="false"
        show-icon
        class="rc-alert"
      />

      <template v-else>
        <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
          <!-- 邀请注册：带上邀请码时组织已确定，不再让用户选 -->
          <el-alert
            v-if="inviteMode"
            :title="inviteTenantName ? `受邀加入「${inviteTenantName}」` : '已识别邀请码'"
            description="组织由邀请链接决定，无需再选择"
            type="success"
            :closable="false"
            show-icon
            class="rc-alert"
          />
          <!-- 多组织部署：必须显式选组织，否则后端会拒绝（避免注册进错误的组织） -->
          <el-form-item
            v-else-if="tenantCfg.require_selector"
            label="加入组织"
            prop="tenant_slug"
          >
            <el-select v-model="form.tenant_slug" placeholder="请选择组织" style="width: 100%">
              <el-option v-for="t in tenantCfg.tenants" :key="t.slug" :label="t.name" :value="t.slug" />
            </el-select>
          </el-form-item>
          <el-form-item label="用户名" prop="username">
            <el-input v-model="form.username" placeholder="2-64 位，字母/数字/_.@-" :prefix-icon="User" />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input
              v-model="form.password"
              type="password"
              show-password
              placeholder="至少 8 位，建议含大小写与数字"
              :prefix-icon="Lock"
            />
          </el-form-item>
          <el-form-item label="确认密码" prop="confirm">
            <el-input v-model="form.confirm" type="password" show-password placeholder="再次输入密码" :prefix-icon="Lock" />
          </el-form-item>
          <el-form-item label="显示名称">
            <el-input v-model="form.display_name" placeholder="留空则与用户名相同" />
          </el-form-item>
          <el-form-item label="邮箱" prop="email">
            <el-input v-model="form.email" placeholder="用于接收审核结果（可选）" :prefix-icon="Message" />
          </el-form-item>
          <el-form-item v-if="departments.length" label="申请部门">
            <el-select v-model="form.department_id" placeholder="请选择（可选）" clearable style="width: 100%">
              <el-option
                v-for="d in departments"
                :key="d.id"
                :label="'　'.repeat(d.depth) + d.name"
                :value="d.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="申请理由">
            <el-input v-model="form.reason" type="textarea" :rows="2" maxlength="256" show-word-limit placeholder="便于管理员审核（可选）" />
          </el-form-item>

          <el-button type="primary" class="rc-submit" :loading="loading" @click="submit">提交注册</el-button>
        </el-form>

        <div class="rc-foot">
          已有账号？<el-link type="primary" :underline="false" @click="toLogin">返回登录</el-link>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock, Message } from '@element-plus/icons-vue'
import { authApi } from '@/api'
import { errMsg } from '@/api/http'

const router = useRouter()
const route = useRoute()
const formRef = ref<FormInstance>()
const loading = ref(false)
const closed = ref(false)
const departments = ref<{ id: number; name: string; parent_id: number | null; depth: number }[]>([])

// 组织路由：邀请码优先于 slug。单组织部署时两者都为空，行为与改造前一致。
const tenantCfg = reactive({
  multi_tenant: false,
  require_selector: false,
  tenants: [] as { slug: string; name: string }[],
  default_slug: null as string | null,
})
const inviteCode = ref('')
const inviteMode = ref(false)

const inviteTenantName = computed(() => {
  // 邀请码对应的组织无法从公开接口得知（避免枚举），这里只在能唯一确定时显示名称
  const only = tenantCfg.tenants.length === 1 ? tenantCfg.tenants[0] : null
  return only && inviteMode.value ? only.name : ''
})

const form = reactive({
  username: '',
  password: '',
  confirm: '',
  display_name: '',
  email: '',
  department_id: null as number | null,
  reason: '',
  tenant_slug: null as string | null,
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 2, max: 64, message: '长度 2-64 位', trigger: 'blur' },
    { pattern: /^[A-Za-z0-9_.@-]+$/, message: '仅可含字母、数字与 _ . @ -', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 8, max: 128, message: '密码至少 8 位', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_r, v, cb) => (v === form.password ? cb() : cb(new Error('两次输入的密码不一致'))),
      trigger: 'blur',
    },
  ],
  email: [{ type: 'email', message: '邮箱格式不正确', trigger: 'blur' }],
  tenant_slug: [
    {
      validator: (_r, v, cb) =>
        !tenantCfg.require_selector || inviteMode.value || v ? cb() : cb(new Error('请选择要加入的组织')),
      trigger: 'change',
    },
  ],
}

onMounted(async () => {
  // 先看注册是否开放；未开放直接显示「已关闭」提示，不必拉部门
  try {
    const st = await authApi.registerStatus()
    if (!st.enabled) {
      closed.value = true
      return
    }
  } catch {
    /* 接口不可用：按未开放处理，避免提交后才报错 */
  }
  inviteCode.value = String(route.query.invite || '').trim()
  inviteMode.value = !!inviteCode.value
  try {
    const cfg = await authApi.registerTenants()
    Object.assign(tenantCfg, cfg)
    // 单组织部署：直接用唯一组织，省掉一次选择
    if (!inviteMode.value && cfg.tenants.length === 1) {
      form.tenant_slug = cfg.tenants[0].slug
    }
  } catch {
    /* 拿不到组织配置时退回「不指定」——后端会在多组织下明确拒绝 */
  }
  try {
    departments.value =
      (await authApi.registerDepartments({
        tenantSlug: form.tenant_slug || undefined,
        inviteCode: inviteCode.value || undefined,
      })) || []
  } catch {
    departments.value = []
  }
})

async function submit() {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return
  loading.value = true
  try {
    const r = await authApi.register({
      username: form.username,
      password: form.password,
      display_name: form.display_name || undefined,
      email: form.email || undefined,
      department_id: form.department_id,
      reason: form.reason || undefined,
      invite_code: inviteCode.value || undefined,
      tenant_slug: inviteMode.value ? undefined : form.tenant_slug || undefined,
    })
    ElMessage.success(r.message || '注册成功')
    router.replace('/login')
  } catch (e) {
    // 后端在未开放注册时返回明确提示，这里单独识别以便展示「已关闭」态
    const msg = errMsg(e)
    if (msg.includes('未开放')) closed.value = true
    ElMessage.error(msg)
  } finally {
    loading.value = false
  }
}

function toLogin() {
  router.replace('/login')
}
</script>

<style scoped>
.reg-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-canvas);
  padding: 24px;
}
.reg-card {
  width: 420px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-modal);
  padding: 30px 32px;
}
.rc-head {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 18px;
}
.rc-logo {
  width: 46px;
  height: 46px;
  border-radius: 12px;
  background: linear-gradient(135deg, #2563eb, #3b82f6);
  color: #fff;
  font-size: 22px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.rc-title {
  margin: 0;
  font-size: 19px;
  font-weight: 700;
  color: var(--text-title);
}
.rc-sub {
  margin: 3px 0 0;
  font-size: 12.5px;
  color: var(--text-secondary);
}
.rc-alert {
  margin-bottom: 8px;
}
.rc-submit {
  width: 100%;
  margin-top: 4px;
}
.rc-foot {
  margin-top: 16px;
  text-align: center;
  font-size: 13px;
  color: var(--text-secondary);
}
</style>
