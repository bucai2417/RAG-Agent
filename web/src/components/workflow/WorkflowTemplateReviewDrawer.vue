<template>
  <el-drawer
    :model-value="modelValue"
    title="模板审核"
    size="760px"
    append-to-body
    @update:model-value="(v: any) => emit('update:modelValue', v)"
  >
    <p class="hint">
      公共模板需审核通过后才会对其他租户可见。风险扫描只给参考等级，
      请结合具体节点内容判断 —— 含 <code>code</code> 节点的模板会执行任意 Python。
    </p>

    <div class="rv-bar">
      <el-button size="small" :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      <span class="rv-count">待审核 {{ items.length }} 个</span>
    </div>

    <el-skeleton v-if="loading" :rows="5" animated />

    <template v-else>
      <div v-for="t in items" :key="t.id" class="rv-card">
        <div class="rv-head">
          <b>{{ t.name }}</b>
          <el-tag size="small" :type="riskTag(t.risk_level)">{{ riskLabel(t.risk_level) }}</el-tag>
          <el-tag size="small" type="info" effect="plain">{{ t.category }}</el-tag>
          <span class="rv-meta">{{ t.node_count }} 节点 · {{ t.case_count }} 用例</span>
        </div>
        <p class="rv-desc">{{ t.description || '（无描述）' }}</p>

        <div v-if="t.risk_findings && t.risk_findings.length" class="rv-findings">
          <div class="rv-findings-title">风险节点</div>
          <div v-for="(f, i) in t.risk_findings" :key="i" class="rv-finding">
            <el-tag size="small" :type="riskTag(f.level)">{{ f.level }}</el-tag>
            <code>{{ f.node_id }}</code>
            <span class="rv-reason">{{ f.reason }}</span>
            <pre v-if="f.hint" class="rv-hint">{{ f.hint }}</pre>
          </div>
        </div>
        <div v-else class="rv-safe">未发现风险节点</div>

        <div class="rv-actions">
          <el-button type="success" size="small" :loading="busyId === t.id" @click="decide(t, true)">通过</el-button>
          <el-button type="danger" size="small" plain :loading="busyId === t.id" @click="decide(t, false)">拒绝</el-button>
        </div>
      </div>
      <el-empty v-if="!items.length" description="没有待审核的模板" :image-size="70" />
    </template>
  </el-drawer>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { workflowApi } from '@/api'
import { errMsg } from '@/api/http'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()

const items = ref<any[]>([])
const loading = ref(false)
const busyId = ref<number | null>(null)

function riskLabel(level?: string) {
  const m: Record<string, string> = { low: '低风险', medium: '中风险', high: '高风险' }
  return m[level || ''] || '无风险'
}
function riskTag(level?: string) {
  return level === 'high' ? 'danger' : level === 'medium' ? 'warning' : 'success'
}

async function load() {
  loading.value = true
  try {
    items.value = await workflowApi.listPendingReviews()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loading.value = false
  }
}

async function decide(t: any, approve: boolean) {
  let note = ''
  try {
    if (!approve) {
      const r = await ElMessageBox.prompt('拒绝原因（会记入审核记录）', '拒绝发布', {
        inputPlaceholder: '如：包含未经验证的外部接口调用',
        confirmButtonText: '确定拒绝',
        cancelButtonText: '取消',
      })
      note = r.value || ''
    }
  } catch {
    return
  }
  busyId.value = t.id
  try {
    const r = await workflowApi.reviewTemplate(t.id, approve, note)
    ElMessage.success(r.message)
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    busyId.value = null
  }
}

watch(
  () => props.modelValue,
  (v) => { if (v) load() },
)
</script>

<style scoped>
.hint { font-size: 12.5px; color: var(--text-secondary); line-height: 1.7; margin: 0 0 12px; }
.hint code { background: var(--bg-subtle); padding: 1px 4px; border-radius: 3px; }
.rv-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.rv-count { font-size: 12.5px; color: var(--text-secondary); }
.rv-card { border: 1px solid var(--border-subtle); border-radius: 8px; padding: 12px 14px; margin-bottom: 12px; }
.rv-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.rv-meta { font-size: 12px; color: var(--text-muted); }
.rv-desc { font-size: 13px; color: var(--text-body); margin: 8px 0; line-height: 1.6; }
.rv-findings { border-top: 1px dashed var(--border-subtle); padding-top: 8px; }
.rv-findings-title { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
.rv-finding { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; font-size: 12.5px; }
.rv-finding code { font-family: monospace; color: var(--text-title); }
.rv-reason { color: var(--text-secondary); }
.rv-hint {
  width: 100%; margin: 4px 0 0; padding: 6px 8px; background: var(--bg-subtle);
  border-radius: 6px; font-size: 11.5px; font-family: monospace; white-space: pre-wrap;
  max-height: 120px; overflow: auto; color: var(--text-body);
}
.rv-safe { font-size: 12.5px; color: #52c41a; }
.rv-actions { display: flex; gap: 8px; margin-top: 12px; }
</style>
