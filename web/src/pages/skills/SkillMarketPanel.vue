<template>
  <div class="market-wrap">
    <!-- 搜索行 -->
    <div class="search-row">
      <el-input
        v-model="query"
        placeholder="搜索技能，如：油价、天气、PPT、翻译…（留空查看精选）"
        clearable
        :prefix-icon="Search"
        @keyup.enter="doSearch"
        @clear="doSearch"
      />
      <el-button type="primary" :icon="Search" :loading="loading" @click="doSearch">搜索</el-button>
    </div>

    <el-alert
      v-if="note"
      :title="note"
      type="info"
      :closable="false"
      show-icon
      class="market-note"
    />
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
      show-icon
      class="market-note"
    />

    <el-skeleton v-if="loading" :rows="6" animated class="market-skeleton" />

    <template v-else>
      <div v-if="candidates.length" class="card-grid">
        <el-card v-for="c in candidates" :key="cardKey(c)" shadow="hover" class="skill-card">
          <div class="card-head">
            <span class="card-name" :title="c.name">{{ c.name }}</span>
            <el-tag size="small" :type="c.source === 'curated' ? 'primary' : 'info'">
              {{ c.source === 'curated' ? '精选' : 'GitHub' }}
            </el-tag>
            <el-tag v-if="c.installed" size="small" type="success">已安装</el-tag>
          </div>

          <div class="card-repo">
            <code>{{ c.repo }}</code>
            <span v-if="c.stars" class="stars">★ {{ c.stars }}</span>
          </div>

          <p class="card-desc">{{ c.description || '（无描述）' }}</p>

          <div v-if="c.sub_skills.length" class="sub-chips">
            <el-tag v-for="s in c.sub_skills.slice(0, 3)" :key="s" size="small" type="info" effect="plain">
              {{ subLabel(s) }}
            </el-tag>
            <span v-if="c.sub_skills.length > 3" class="more">+{{ c.sub_skills.length - 3 }}</span>
          </div>

          <div class="card-ops">
            <el-button size="small" :icon="View" @click="openDetail(c)">预览</el-button>
            <template v-if="c.installed">
              <el-button
                size="small"
                type="warning"
                plain
                :icon="RefreshRight"
                :loading="busyKey === cardKey(c)"
                @click="doUpdate(c)"
              >更新</el-button>
            </template>
            <el-button
              v-else
              size="small"
              type="primary"
              :icon="Download"
              :disabled="!canEdit"
              :loading="busyKey === cardKey(c)"
              @click="doInstall(c)"
            >安装</el-button>
          </div>
        </el-card>
      </div>

      <el-empty v-else description="没有匹配的技能，换个关键词试试" :image-size="70" />
    </template>

    <!-- 预览抽屉：先打开骨架屏，再异步填内容（网络慢时不至于“点了没反应”） -->
    <el-drawer v-model="detailOpen" :title="detailTitle" size="640px">
      <el-skeleton v-if="detailLoading" :rows="8" animated />
      <el-result
        v-else-if="detailError"
        icon="warning"
        :title="detailError"
        :sub-title="detailCandidate ? detailCandidate.repo : ''"
      >
        <template #extra>
          <el-button @click="retryDetail">重试</el-button>
          <el-button v-if="detailCandidate" :icon="Link" @click="openExternal(detailCandidate)">在 GitHub 打开</el-button>
        </template>
      </el-result>
      <template v-else-if="detail">
        <div class="kv"><span class="k">仓库</span>
          <span class="v"><a :href="detail.html_url" target="_blank" rel="noreferrer noopener">{{ detail.repo }}</a></span>
        </div>
        <div v-if="detail.subpath" class="kv"><span class="k">子技能目录</span><span class="v">{{ detail.subpath }}</span></div>
        <div v-if="detail.version" class="kv"><span class="k">版本</span><span class="v">{{ detail.version }}</span></div>
        <div class="kv"><span class="k">文件数</span><span class="v">{{ detail.files.length }}</span></div>

        <el-alert
          v-for="l in detail.limitations"
          :key="l"
          :title="l"
          type="warning"
          :closable="false"
          show-icon
          class="market-note"
        />

        <h4 class="p-title">说明</h4>
        <MarkdownBody v-if="detail.body_md" :content="detail.body_md" />
        <p v-else class="text-muted">（SKILL.md 无正文）</p>

        <template v-if="detail.entry_scripts.length">
          <h4 class="p-title">入口脚本</h4>
          <div v-for="s in detail.entry_scripts" :key="s.name" class="script-row">
            <code>{{ s.name }}</code>
            <el-tag v-if="s.runtime" size="small">{{ s.runtime }}</el-tag>
            <span class="text-muted">{{ s.path }}</span>
          </div>
        </template>

        <template v-if="detail.files.length">
          <h4 class="p-title">文件清单</h4>
          <div class="file-list">
            <div v-for="f in detail.files.slice(0, 200)" :key="f.path" class="file-row">
              <span class="file-path">{{ f.path }}</span>
              <span class="text-muted">{{ f.size }} B</span>
            </div>
            <p v-if="detail.files.length > 200" class="text-muted">…… 共 {{ detail.files.length }} 个文件</p>
          </div>
        </template>
      </template>

      <template #footer>
        <el-button @click="detailOpen = false">关闭</el-button>
        <el-button
          v-if="canEdit && detailCandidate && !detailLoading"
          type="primary"
          :icon="Download"
          :loading="busyKey === 'detail'"
          @click="installFromDetail"
        >{{ detailError ? '不看详情，直接安装' : '安装此技能' }}</el-button>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, Download, View, RefreshRight, Link } from '@element-plus/icons-vue'
import MarkdownBody from '@/components/MarkdownBody.vue'
import { skillMarketApi, type MarketCandidate, type MarketDetail } from '@/api'
import { errMsg } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const emit = defineEmits<{ (e: 'installed', skillId?: number): void }>()

const auth = useAuthStore()
const canEdit = computed(() => auth.hasPermission('skill:edit') || auth.hasPermission('skill:manage'))

const query = ref('')
const candidates = ref<MarketCandidate[]>([])
const loading = ref(false)
const note = ref<string | null>(null)
const error = ref<string | null>(null)
const busyKey = ref<string | null>(null)

const detailOpen = ref(false)
const detail = ref<MarketDetail | null>(null)
const detailCandidate = ref<MarketCandidate | null>(null)
const detailLoading = ref(false)
const detailError = ref<string | null>(null)

const detailTitle = computed(() => {
  if (detail.value) return detail.value.name
  if (detailCandidate.value) return detailCandidate.value.name || detailCandidate.value.repo
  return '技能预览'
})

function retryDetail() {
  const c = detailCandidate.value
  if (c) void openDetail(c)
}

function openExternal(c: MarketCandidate) {
  window.open(c.html_url, '_blank', 'noopener,noreferrer')
}

function cardKey(c: MarketCandidate) {
  return c.sub_skills.length === 1 ? `${c.repo}#${c.sub_skills[0]}` : c.repo
}
function subLabel(s: string) {
  return s ? s.split('/').pop() || s : '（根目录）'
}

async function doSearch() {
  loading.value = true
  error.value = null
  note.value = null
  try {
    const r = await skillMarketApi.search(query.value.trim())
    candidates.value = r.candidates || []
    note.value = r.note
    error.value = r.error
  } catch (e) {
    error.value = errMsg(e)
    candidates.value = []
  } finally {
    loading.value = false
  }
}

async function openDetail(c: MarketCandidate) {
  detailCandidate.value = c
  // 先把抽屉打开显示骨架屏 —— 网络慢时用户能立刻看到反馈，而不是“点了没反应”
  detail.value = null
  detailError.value = null
  detailLoading.value = true
  detailOpen.value = true

  // 单子技能 → 用该 subpath 预览；多子技能 → 预览仓库根（多数多技能仓库根目录无 SKILL.md，会提示）
  const sub = c.sub_skills.length === 1 ? c.sub_skills[0] : ''
  try {
    detail.value = await skillMarketApi.detail(c.repo, sub)
  } catch (e) {
    detailError.value = errMsg(e)
  } finally {
    detailLoading.value = false
  }
}

/** 把安装结果按统一口径提示，并刷新卡片状态 */
function handleResult(r: any, repo: string) {
  if (r.duplicate) {
    ElMessage.warning(`技能包已存在（技能 #${r.existing_skill_id}「${r.existing_skill_name}」），未重复导入`)
  } else if (r.collection) {
    ElMessage.success(`已导入技能集合「${r.name}」（${r.count} 个子技能）`)
  } else if (r.upgraded) {
    ElMessage.success(`已更新「${repo}」到最新版本`)
  } else {
    ElMessage.success(`安装成功：${r.name}（${r.files} 个文件）`)
  }
  emit('installed', r.skill_id)
  void doSearch()
}

/** 多技能仓库：让用户选「单独装某个子技能」或「整体装成技能集合」 */
async function resolveMultiSkill(c: MarketCandidate): Promise<void> {
  const subs = c.sub_skills
  try {
    const { value } = await ElMessageBox.prompt(
      `该仓库含 ${subs.length} 个技能。可填入一个子技能名单独安装，或留空整体导入为「技能集合」。\n可选：${subs.map(subLabel).join('、')}`,
      '多技能仓库',
      { inputPlaceholder: '（可选）子技能名，如 data-query', confirmButtonText: '确定' },
    )
    const name = (value || '').trim()
    if (!name) {
      handleResult(await skillMarketApi.installCollection(c.repo), c.repo)
      return
    }
    const sub = subs.find((s) => subLabel(s) === name || s === name || s.endsWith('/' + name)) || name
    handleResult(await skillMarketApi.install(c.repo, sub), c.repo)
  } catch {
    /* 取消 */
  }
}

async function doInstall(c: MarketCandidate) {
  const key = cardKey(c)
  busyKey.value = key
  try {
    if (c.sub_skills.length > 1) {
      await resolveMultiSkill(c)
    } else {
      const r = await skillMarketApi.install(c.repo, c.sub_skills[0] || '')
      if (r.multi_skill) await resolveMultiSkill(c)
      else handleResult(r, c.repo)
    }
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    busyKey.value = null
  }
}

async function doUpdate(c: MarketCandidate) {
  const skillId = c.installed?.skill_id
  if (!skillId) {
    ElMessage.warning('未找到对应技能，请到「我的技能」手动更新')
    return
  }
  const key = cardKey(c)
  busyKey.value = key
  try {
    const sub = c.installed?.subpath || c.sub_skills[0] || ''
    handleResult(await skillMarketApi.install(c.repo, sub, skillId), c.repo)
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    busyKey.value = null
  }
}

async function installFromDetail() {
  const c = detailCandidate.value
  if (!c) return
  busyKey.value = 'detail'
  try {
    if (c.sub_skills.length > 1) {
      detailOpen.value = false
      await resolveMultiSkill(c)
    } else {
      const r = await skillMarketApi.install(c.repo, c.sub_skills[0] || '')
      if (r.multi_skill) {
        detailOpen.value = false
        await resolveMultiSkill(c)
      } else {
        detailOpen.value = false
        handleResult(r, c.repo)
      }
    }
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    busyKey.value = null
  }
}

onMounted(doSearch)
</script>

<style scoped>
.market-wrap { padding-top: 4px; }
.search-row { display: flex; gap: 8px; margin-bottom: 12px; }
.market-note { margin-bottom: 12px; }
.market-skeleton { padding: 12px 0; }
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.skill-card :deep(.el-card__body) { padding: 14px 16px; display: flex; flex-direction: column; height: 100%; }
.card-head { display: flex; align-items: center; gap: 8px; }
.card-name { font-weight: 600; color: var(--text-title); font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
.card-repo { display: flex; align-items: center; gap: 8px; margin-top: 6px; font-size: 12px; color: var(--text-secondary); }
.card-repo code { font-size: 12px; }
.stars { color: var(--color-primary); }
.card-desc {
  margin: 8px 0; font-size: 13px; line-height: 1.55; color: var(--text-body);
  display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  min-height: 40px;
}
.sub-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; min-height: 22px; }
.more { font-size: 12px; color: var(--text-secondary); align-self: center; }
.card-ops { display: flex; gap: 8px; margin-top: auto; }
.kv { display: flex; align-items: center; gap: 8px; padding: 7px 0; border-bottom: 1px solid var(--border-subtle); font-size: 13px; }
.kv .k { width: 100px; color: var(--text-secondary); flex: none; }
.kv .v { color: var(--text-body); word-break: break-all; }
.p-title { font-size: 14px; font-weight: 600; color: var(--text-title); margin: 16px 0 8px; }
.script-row { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--border-subtle); font-size: 13px; }
.script-row code { font-family: monospace; }
.file-list { max-height: 260px; overflow: auto; }
.file-row { display: flex; justify-content: space-between; gap: 8px; padding: 4px 0; font-size: 12.5px; font-family: monospace; }
.file-path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
