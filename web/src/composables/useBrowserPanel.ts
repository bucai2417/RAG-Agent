/**
 * 聊天页右侧「浏览器实时画面」面板的展开状态 + 跟随目标。
 *
 * 为什么抽成 composable：
 * 「智能问答」和「智能体对话」两个页面的消息流是各自实现的（SSE 处理有两套），
 * 但"哪些工具调用该把面板顶出来"必须完全一致 —— 否则同一句话在两个入口
 * 行为不同，用户会以为是 bug。
 *
 * 展开规则（刻意不做任何跨会话记忆）：
 * - 默认关闭：面板一挂载就连 WS 并持续推帧，默认开着等于让每个进聊天页的人
 *   都在后台烧 CPU 和带宽；
 * - AI 一旦调用浏览器工具就自动展开：否则用户只看到一行"浏览网页"，
 *   不知道它到底在哪个页面、点了什么；
 * - 用户手动关掉后，下一次浏览器工具调用仍会重新展开。
 *
 * 早先的实现把开关写进 localStorage，并额外记一个 dismissed 抑制自动展开，
 * 两个毛病都出在"跨轮次记住用户的手动动作"上：
 * - 历史上被自动顶开过一次，之后永远读到 '1'，于是"默认关闭"只在第一次成立；
 * - 手动关过一次后 dismissed 一直为真，AI 再操作浏览器画面也不再出现，
 *   用户会以为实时画面坏了。
 */
import { ref } from 'vue'

/**
 * 这些工具意味着"AI 正在看/动浏览器"，此时画面有信息量：
 * - browser_tabs / browser_snapshot / browser_read / browser_action 是浏览器工具本身
 *
 * 刻意不匹配工具名里普通出现 "browser" 的情况（如某个 MCP 工具叫
 * get_browser_info），避免无谓地把面板顶出来占掉半屏宽度。
 */
const BROWSER_TOOL_RE = /^browser_(tabs|snapshot|read|action)$/

export function isBrowserTool(name?: string | null): boolean {
  if (!name) return false
  return BROWSER_TOOL_RE.test(name)
}

export function useBrowserPanel() {
  /** 默认关闭：每次进入页面都是收起状态，由浏览器工具调用顶开 */
  const open = ref(false)

  /**
   * AI 当前在操作的页面地址。
   *
   * 为什么不靠面板自己轮询标签列表：
   * AI 可能在**别的标签页**操作（用户开着 3 个页面，AI 在第 2 个里点按钮）。
   * 面板若只显示"第一个标签"，用户看到的画面就不是 AI 实际操作的那个页面，
   * 面板从"可观测"变成了"误导"。这里把地址传下去，由面板切到对应标签。
   *
   * followSeq 递增用于"同一个 URL 连续操作"时也能触发面板重新对齐：
   * watch 只看 URL 值时，标签被关掉重开（URL 相同）不会重新 attach。
   */
  const followUrl = ref('')
  const followSeq = ref(0)

  function show() {
    open.value = true
  }

  function close() {
    open.value = false
  }

  function toggle() {
    open.value = !open.value
  }

  /**
   * 工具调用时调用：是浏览器工具就展开。
   * 返回是否真的展开了，供调用方决定要不要额外提示。
   */
  function autoShowForTool(name?: string | null): boolean {
    if (!isBrowserTool(name)) return false
    open.value = true
    return true
  }

  /**
   * 告诉面板"AI 正在操作这个地址"。
   *
   * 即使面板没展开也要记录：用户随后手动打开面板时，
   * 应该直接看到 AI 正在操作的那个页面，而不是浏览器里的第一个标签。
   */
  function follow(url?: string | null): void {
    const u = (url || '').trim()
    if (!u) return
    followUrl.value = u
    followSeq.value += 1
  }

  return {
    open, followUrl, followSeq,
    show, close, toggle, autoShowForTool, follow,
  }
}
