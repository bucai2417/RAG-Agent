<template>
  <div class="usage-panel">
    <div class="up-bar">
      <el-radio-group v-model="days" size="small" @change="load">
        <el-radio-button :value="7">近 7 天</el-radio-button>
        <el-radio-button :value="30">近 30 天</el-radio-button>
        <el-radio-button :value="90">近 90 天</el-radio-button>
      </el-radio-group>
      <el-button size="small" :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-skeleton v-if="loading && !data" :rows="4" animated />

    <template v-else-if="data">
      <div class="up-cards">
        <div class="up-card">
          <div class="up-label">运行次数</div>
          <div class="up-value">{{ fmt(data.total.runs) }}</div>
        </div>
        <div class="up-card">
          <div class="up-label">模型调用</div>
          <div class="up-value">{{ fmt(data.total.calls) }}</div>
        </div>
        <div class="up-card">
          <div class="up-label">输入 Token</div>
          <div class="up-value">{{ fmt(data.total.prompt_tokens) }}</div>
        </div>
        <div class="up-card">
          <div class="up-label">输出 Token</div>
          <div class="up-value">{{ fmt(data.total.completion_tokens) }}</div>
        </div>
      </div>

      <div v-if="data.series.length" class="up-chart">
        <VChart :option="chartOption" height="280px" />
      </div>
      <el-empty
        v-else
        description="该时间段内没有模型调用记录（纯数据变换的工作流不产生 Token）"
        :image-size="60"
      />

      <h4 class="up-title">按天明细</h4>
      <el-table :data="reversedSeries" size="small" empty-text="暂无数据" max-height="320">
        <el-table-column prop="date" label="日期" width="120" />
        <el-table-column label="运行" width="90" align="right">
          <template #default="{ row }">{{ fmt(row.runs) }}</template>
        </el-table-column>
        <el-table-column label="调用" width="90" align="right">
          <template #default="{ row }">{{ fmt(row.calls) }}</template>
        </el-table-column>
        <el-table-column label="输入 Token" width="130" align="right">
          <template #default="{ row }">{{ fmt(row.prompt_tokens) }}</template>
        </el-table-column>
        <el-table-column label="输出 Token" width="130" align="right">
          <template #default="{ row }">{{ fmt(row.completion_tokens) }}</template>
        </el-table-column>
        <el-table-column label="合计" align="right">
          <template #default="{ row }">{{ fmt(row.prompt_tokens + row.completion_tokens) }}</template>
        </el-table-column>
      </el-table>
    </template>

    <el-empty v-else description="加载失败" :image-size="60" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import VChart from '@/components/VChart.vue'
import { workflowApi } from '@/api'
import { errMsg } from '@/api/http'

interface DayUsage {
  date: string
  prompt_tokens: number
  completion_tokens: number
  calls: number
  runs: number
}
interface UsageData {
  days: number
  total: { prompt_tokens: number; completion_tokens: number; calls: number; runs: number }
  series: DayUsage[]
}

const props = defineProps<{ agentId: number }>()

const days = ref(30)
const loading = ref(false)
const data = ref<UsageData | null>(null)

const reversedSeries = computed(() => [...(data.value?.series || [])].reverse())

function fmt(n: number | undefined) {
  const v = Number(n || 0)
  if (v >= 1000000) return `${(v / 1000000).toFixed(1)}M`
  if (v >= 10000) return `${(v / 1000).toFixed(1)}k`
  return v.toLocaleString('zh-CN')
}

const chartOption = computed(() => {
  const s = data.value?.series || []
  return {
    grid: { left: 56, right: 20, top: 28, bottom: 30 },
    tooltip: { trigger: 'axis' },
    legend: { top: 0, textStyle: { fontSize: 11, color: '#64748b' } },
    xAxis: {
      type: 'category',
      data: s.map((d) => d.date.slice(5)),
      axisLine: { lineStyle: { color: '#e2e8f0' } },
      axisTick: { show: false },
      axisLabel: { color: '#94a3b8', fontSize: 11 },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: '#f1f5f9' } },
      axisLabel: { color: '#94a3b8', fontSize: 11 },
    },
    series: [
      {
        name: '输入 Token',
        type: 'line',
        smooth: 0.35,
        symbol: 'circle',
        symbolSize: 5,
        data: s.map((d) => d.prompt_tokens),
        itemStyle: { color: '#2563eb' },
        lineStyle: { width: 2, color: '#2563eb' },
        areaStyle: {
          color: {
            type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(37,99,235,0.16)' },
              { offset: 1, color: 'rgba(37,99,235,0.01)' },
            ],
          },
        },
      },
      {
        name: '输出 Token',
        type: 'line',
        smooth: 0.35,
        symbol: 'circle',
        symbolSize: 5,
        data: s.map((d) => d.completion_tokens),
        itemStyle: { color: '#0891b2' },
        lineStyle: { width: 2, color: '#0891b2' },
      },
    ],
  }
})

async function load() {
  loading.value = true
  try {
    data.value = await workflowApi.usage(props.agentId, days.value)
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loading.value = false
  }
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.usage-panel { padding: 2px 0; }
.up-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.up-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 16px; }
.up-card {
  border: 1px solid var(--border-subtle); border-radius: 8px;
  padding: 10px 14px; background: var(--bg-subtle);
}
.up-label { font-size: 12px; color: var(--text-secondary); margin-bottom: 4px; }
.up-value { font-size: 20px; font-weight: 600; color: var(--text-title); font-variant-numeric: tabular-nums; }
.up-chart { border: 1px solid var(--border-subtle); border-radius: 8px; padding: 8px; margin-bottom: 16px; }
.up-title { font-size: 13px; font-weight: 600; color: var(--text-title); margin: 0 0 8px; }
</style>
