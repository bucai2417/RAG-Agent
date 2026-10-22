<template>
  <PageContainer title="浏览器" subtitle="让 AI 的网页工具复用你已登录的浏览器会话">
    <div class="app-card panel">
      <el-alert type="info" :closable="false" show-icon class="tip">
        <template #title>为什么需要这一步</template>
        <p>
          AI 的网页工具通过 CDP 调试端口接管浏览器。直接连接你日常使用的浏览器无法开启该端口，
          所以这里用<b>独立配置目录</b>启动一个实例 —— 首次请在这个窗口里登录目标网站，
          之后登录态会保存下来重复使用。
        </p>
      </el-alert>

      <el-skeleton v-if="loading" :rows="4" animated />

      <template v-else>
        <!-- 一个浏览器都没装：直接给出去哪装的引导，不必等用户点「启动」才看到报错 -->
        <BrowserMissingGuide v-if="st.no_browser" :guide="st.install_guide" class="tip" />

        <div class="status-row">
          <el-tag :type="st.port_ready ? 'success' : 'info'" effect="dark" size="large">
            {{ st.port_ready ? '调试端口已就绪' : '未启动' }}
          </el-tag>
          <span class="meta">
            端口 <b>{{ st.port }}</b>
            <template v-if="st.pid"> · PID <b>{{ st.pid }}</b></template>
            <template v-if="st.managed"> · <span class="managed">由本服务管理</span></template>
          </span>
          <div class="spacer" />
          <el-button :icon="Refresh" @click="refresh">刷新</el-button>
          <el-button v-if="st.port_ready" type="danger" plain :icon="SwitchButton" @click="stop">停止</el-button>
          <el-tooltip v-else :disabled="!st.no_browser" content="请先安装浏览器（见上方引导）" placement="top">
            <span>
              <el-button
                type="primary" :icon="VideoPlay" :loading="busy"
                :disabled="!!st.no_browser" @click="launch"
              >启动浏览器</el-button>
            </span>
          </el-tooltip>
        </div>

        <el-alert v-if="st.error" type="error" :closable="false" show-icon :title="st.error" class="tip" />

        <el-form label-width="110px" class="cfg">
          <el-form-item label="浏览器">
            <el-select
              v-model="exe" style="width: 100%" placeholder="自动选择（内置优先）" clearable
              :disabled="!!st.no_browser"
            >
              <el-option-group v-if="bundled.length" label="内置运行时（可选组件）">
                <el-option v-for="b in bundled" :key="b.path" :label="`${b.name} — ${b.path}`" :value="b.path" />
              </el-option-group>
              <el-option-group v-if="system.length" label="系统浏览器">
                <el-option v-for="b in system" :key="b.path" :label="`${b.name} — ${b.path}`" :value="b.path" />
              </el-option-group>
            </el-select>
            <div v-if="st.no_browser" class="hint warn">
              未检测到 Chrome / Edge / Chromium，请按上方引导安装后再启动。
            </div>
            <div v-else class="hint">
              不选则自动使用列表第一项（内置运行时优先，其次系统浏览器）。
            </div>
          </el-form-item>
          <el-form-item label="调试端口">
            <el-input-number v-model="port" :min="1024" :max="65535" controls-position="right" />
          </el-form-item>
          <el-form-item label="无头模式">
            <el-switch v-model="headless" />
            <span class="hint">
              无头不弹窗口，但<b>无法扫码/输密码登录</b>，仅适合已登录过的 profile。
            </span>
          </el-form-item>
          <el-form-item label="配置目录">
            <span class="mono path">{{ st.profile_dir }}</span>
          </el-form-item>
        </el-form>

        <el-divider />
        <div class="usage">
          <div class="usage-title">启动后可以这样用</div>
          <ul>
            <li>在聊天里直接说「打开百度首页并读出结果」，AI 会自动调用浏览器工具。</li>
            <li>需要登录的站点，先在这个浏览器窗口里登录一次，之后 AI 可复用登录态。</li>
            <li>页面抓取、填表、点击、截图都可用；遇到验证码需你在窗口里手动通过。</li>
          </ul>
        </div>
      </template>
    </div>
  </PageContainer>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, SwitchButton, VideoPlay } from '@element-plus/icons-vue'
import PageContainer from '@/components/PageContainer.vue'
import BrowserMissingGuide from '@/components/BrowserMissingGuide.vue'
import { browserApi, type BrowserStatus } from '@/api'
import { errMsg } from '@/api/http'

const loading = ref(true)
const busy = ref(false)
const exe = ref('')
const port = ref(9222)
const headless = ref(false)

const st = ref<BrowserStatus>({
  running: false, managed: false, pid: null, port: 9222, port_ready: false,
  profile_dir: '', started_at: null, error: null, browsers: [],
})

// 内置运行时（可选组件安装）与系统浏览器分组展示
const bundled = computed(() => st.value.browsers.filter((b) => b.source === 'bundled'))
const system = computed(() => st.value.browsers.filter((b) => b.source !== 'bundled'))

async function refresh() {
  try {
    st.value = await browserApi.status()
    // 不预选：留空 = 后端按「内置优先、系统兜底」自动挑
    if (exe.value && !st.value.browsers.some((b) => b.path === exe.value)) exe.value = ''
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loading.value = false
  }
}

async function launch() {
  busy.value = true
  try {
    const r = await browserApi.launch({
      executable: exe.value || undefined,
      port: port.value,
      headless: headless.value,
    })
    st.value = { ...st.value, ...r }
    ElMessage.success(r.message || '已启动')
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    busy.value = false
  }
}

async function stop() {
  try {
    const r = await browserApi.stop()
    st.value = { ...st.value, ...r }
    ElMessage.success(r.message || '已停止')
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

onMounted(refresh)
</script>

<style scoped>
.panel { padding: 20px; }
.tip { margin-bottom: 16px; }
.tip p { margin: 4px 0 0; font-size: 13px; line-height: 1.7; }
.status-row { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.meta { font-size: 13px; color: var(--el-text-color-secondary); }
.managed { color: var(--el-color-success); }
.spacer { flex: 1; }
.cfg { max-width: 720px; margin-top: 8px; }
.hint { font-size: 12px; color: var(--el-text-color-secondary); margin-left: 8px; }
.hint.warn { color: var(--el-color-warning); margin: 4px 0 0; display: block; }
.mono { font-family: var(--el-font-family-mono, monospace); }
.path { font-size: 12px; word-break: break-all; }
.usage-title { font-weight: 600; margin-bottom: 8px; }
.usage ul { margin: 0; padding-left: 20px; font-size: 13px; line-height: 1.9; color: var(--el-text-color-regular); }
</style>
