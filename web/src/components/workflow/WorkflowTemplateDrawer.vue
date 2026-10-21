<template>
  <el-drawer
    :model-value="modelValue"
    title="工作流模板"
    size="720px"
    append-to-body
    @update:model-value="(v: any) => emit('update:modelValue', v)"
  >
    <p class="hint">
      套用模板会创建一个<b>新的</b>工作流型智能体，不影响当前工作流。
      模板是完整可运行的图，套用后可直接运行或微调。
    </p>

    <div class="tpl-bar">
      <el-radio-group v-model="scope" size="small">
        <el-radio-button value="builtin">内置模板</el-radio-button>
        <el-radio-button value="user">我的模板</el-radio-button>
        <el-radio-button value="public">公共库</el-radio-button>
      </el-radio-group>
      <el-input v-model="filter" size="small" placeholder="搜索模板" clearable :prefix-icon="Search" style="width: 200px" />
      <el-button size="small" :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      <el-button v-if="isAdmin" size="small" :icon="Stamp" @click="reviewOpen = true">审核</el-button>
    </div>

    <el-skeleton v-if="loading" :rows="5" animated />

    <template v-else>
      <div v-if="filtered.length" class="tpl-grid">
        <el-card v-for="t in filtered" :key="keyOf(t)" shadow="hover" class="tpl-card">
          <div class="tpl-head">
            <span class="tpl-name">{{ t.name }}</span>
            <el-tag size="small" :type="t.source === 'builtin' ? 'primary' : t.source === 'public' ? 'success' : 'info'">
              {{ t.source === 'builtin' ? '内置' : t.source === 'public' ? '公共' : '我的' }}
            </el-tag>
            <el-tag v-if="t.risk_level && t.risk_level !== 'none'" size="small" :type="riskTag(t.risk_level)">
              {{ riskLabel(t.risk_level) }}
            </el-tag>
          </div>
          <div class="tpl-meta">
            <el-tag size="small" type="info" effect="plain">{{ t.category }}</el-tag>
            <span class="tpl-count">{{ t.node_count }} 个节点</span>
            <el-tag v-if="t.case_count" size="small" type="success" effect="plain">
              {{ t.case_count }} 个用例
            </el-tag>
            <span v-if="t.source === 'public' && t.apply_count" class="tpl-count">
              套用 {{ t.apply_count }} 次
            </span>
            <span v-if="t.source === 'public' && t.rating" class="tpl-rating">
              ★ {{ t.rating }}<span class="tpl-count">（{{ t.rating_count }}）</span>
            </span>
          </div>
          <div v-if="t.review_status === 'pending'" class="tpl-review pending">待审核</div>
          <div v-else-if="t.review_status === 'rejected'" class="tpl-review rejected">审核未通过</div>
          <p class="tpl-desc">{{ t.description }}</p>
          <div class="tpl-tags">
            <el-tag v-for="tag in t.tags" :key="tag" size="small" effect="plain">{{ tag }}</el-tag>
          </div>
          <div class="tpl-actions">
            <el-button size="small" type="primary" :disabled="!canEdit" @click="apply(t)">套用</el-button>

            <!-- 公共模板可评分 -->
            <el-button
              v-if="t.source === 'public'"
              size="small"
              :icon="Star"
              @click="rate(t)"
            >评分</el-button>

            <!-- 自己的模板：发布 / 撤回 -->
            <el-button
              v-if="t.source === 'user'"
              size="small"
              :type="t.visibility === 'public' ? 'warning' : t.review_status === 'pending' ? 'info' : 'default'"
              plain
              :loading="busyId === t.id"
              @click="togglePublish(t)"
            >{{ publishLabel(t) }}</el-button>

            <el-popconfirm v-if="t.source === 'user'" title="删除该模板？" @confirm="remove(t)">
              <template #reference><el-button size="small" link type="danger">删除</el-button></template>
            </el-popconfirm>
          </div>
        </el-card>
      </div>
      <el-empty v-else :description="emptyText" :image-size="70" />
    </template>

    <WorkflowTemplateReviewDrawer v-model="reviewOpen" />
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, Refresh, Star, Stamp } from '@element-plus/icons-vue'
import { workflowApi, type WorkflowTemplateItem } from '@/api'
import { errMsg } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import WorkflowTemplateReviewDrawer from './WorkflowTemplateReviewDrawer.vue'

const props = defineProps<{ modelValue: boolean; canEdit: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()

const router = useRouter()
const auth = useAuthStore()
const reviewOpen = ref(false)
// 审核入口仅对管理员显示（后端仍会校验 workflow:manage 权限）
const isAdmin = computed(
  () => auth.hasPermission('workflow:manage') || auth.hasPermission('*'),
)

const scope = ref<'builtin' | 'user' | 'public'>('builtin')
const filter = ref('')
const loading = ref(false)
const builtin = ref<WorkflowTemplateItem[]>([])
const userTemplates = ref<WorkflowTemplateItem[]>([])
const publicTemplates = ref<WorkflowTemplateItem[]>([])
const busyId = ref<number | null>(null)

const filtered = computed(() => {
  const list =
    scope.value === 'builtin' ? builtin.value
      : scope.value === 'public' ? publicTemplates.value
        : userTemplates.value
  const kw = filter.value.trim().toLowerCase()
  if (!kw) return list
  return list.filter(
    (t) =>
      t.name.toLowerCase().includes(kw)
      || t.description.toLowerCase().includes(kw)
      || t.category.toLowerCase().includes(kw)
      || (t.tags || []).some((x) => x.toLowerCase().includes(kw)),
  )
})

function keyOf(t: WorkflowTemplateItem) {
  return t.key || `u-${t.id}`
}

const emptyText = computed(() => {
  if (filter.value.trim()) return '没有匹配的模板'
  if (scope.value === 'user') return '还没有自己的模板，可在工作流编辑页「存为模板」'
  if (scope.value === 'public') return '公共库还没有模板，可把「我的模板」发布到这里'
  return '暂无内置模板'
})

async function load() {
  loading.value = true
  try {
    const r = await workflowApi.listTemplates()
    builtin.value = r.builtin || []
    userTemplates.value = r.user || []
    publicTemplates.value = r.public || []
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loading.value = false
  }
}

async function togglePublish(t: WorkflowTemplateItem) {
  if (!t.id) return
  busyId.value = t.id
  try {
    // 已公开 → 撤回；待审核 → 撤回；否则申请发布
    const shouldPublish = t.visibility !== 'public' && t.review_status !== 'pending'
    const r = await workflowApi.publishTemplate(t.id, shouldPublish)
    if (r.blocked) {
      ElMessageBox.alert(r.message || '该模板包含高风险内容，无法直接公开', '发布被拦截', {
        type: 'warning', confirmButtonText: '知道了',
      })
    } else {
      ElMessage.success(r.message || (shouldPublish ? '已提交审核' : '已撤回'))
    }
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    busyId.value = null
  }
}

function publishLabel(t: WorkflowTemplateItem) {
  if (t.review_status === 'pending') return '撤回申请'
  if (t.visibility === 'public') return '取消公开'
  return '申请公开'
}

function riskLabel(level?: string) {
  const m: Record<string, string> = { low: '低风险', medium: '中风险', high: '高风险' }
  return m[level || ''] || level || ''
}
function riskTag(level?: string) {
  return level === 'high' ? 'danger' : level === 'medium' ? 'warning' : 'info'
}

/** 评分：1-5 星 */
async function rate(t: WorkflowTemplateItem) {
  if (!t.id) return
  let rating = 5
  try {
    const r = await ElMessageBox.prompt('给这个模板打分（1-5 分）', '评分', {
      inputValue: String(Math.round(t.rating || 5)),
      inputPlaceholder: '1-5',
      confirmButtonText: '提交',
      cancelButtonText: '取消',
      inputValidator: (v: string) => {
        const n = Number(v)
        return Number.isInteger(n) && n >= 1 && n <= 5 ? true : '请输入 1-5 的整数'
      },
    })
    rating = Number(r.value)
  } catch {
    return
  }
  try {
    const r = await workflowApi.rateTemplate(t.id, rating)
    ElMessage.success(`已评分，当前平均 ${r.rating} 分（${r.rating_count} 人）`)
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function apply(t: WorkflowTemplateItem) {
  let name = t.name
  try {
    const r = await ElMessageBox.prompt('新工作流名称', '套用模板', {
      inputValue: t.name,
      confirmButtonText: '创建',
      cancelButtonText: '取消',
    })
    name = (r.value || t.name).trim() || t.name
  } catch {
    return // 取消
  }
  try {
    const r = await workflowApi.applyTemplate({
      key: t.key,
      template_id: t.id,
      name,
    })
    ElMessage.success(`已创建「${r.name}」（${r.node_count} 个节点）`)
    emit('update:modelValue', false)
    router.push(`/agents/${r.agent_id}/workflow`)
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function remove(t: WorkflowTemplateItem) {
  if (!t.id) return
  try {
    await workflowApi.deleteTemplate(t.id)
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

watch(
  () => props.modelValue,
  (v) => { if (v) load() },
)
</script>

<style scoped>
.hint { font-size: 12.5px; color: var(--text-secondary); line-height: 1.7; margin: 0 0 12px; }
.tpl-bar { display: flex; gap: 10px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
.tpl-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.tpl-card :deep(.el-card__body) { padding: 14px 16px; display: flex; flex-direction: column; height: 100%; }
.tpl-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.tpl-name { font-weight: 600; color: var(--text-title); font-size: 14px; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tpl-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.tpl-count { font-size: 12px; color: var(--text-muted); }
.tpl-desc {
  font-size: 13px; line-height: 1.55; color: var(--text-body); margin: 0 0 10px;
  display: -webkit-box; -webkit-line-clamp: 3; line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  min-height: 40px;
}
.tpl-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; min-height: 22px; }
.tpl-actions { display: flex; gap: 8px; margin-top: auto; }
</style>
