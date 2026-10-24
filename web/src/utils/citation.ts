/**
 * 引用相关的纯函数（前后端同口径）。
 *
 * 为什么前端要自己算一遍「答案引用了哪几条」：
 * 实时流里后端会下发 used_ids（答案里出现过的 [n]），但历史消息只存了 citations，
 * 没存这个派生字段。刷新页面后若不算，同一句话的引用归属会从「已引用」
 * 掉回「仅检索到」，用户会以为数据丢了。
 *
 * 口径必须与后端 citation_check.py 一致：
 * - 只认 [数字]，不认 [^1] / 【1】/ (1)；
 * - 只保留 1..引用条数 范围内的编号（越界的是模型编的，不能拿来高亮）。
 */
import type { Citation } from '@/api'

const CITE_RE = /\[(\d+)\]/

export function usedCiteIds(content: string | null | undefined, citations?: Citation[] | null): number[] {
  const n = citations?.length || 0
  if (!n) return []
  const ids = new Set<number>()
  for (const m of String(content || '').matchAll(new RegExp(CITE_RE, 'g'))) {
    const i = Number(m[1])
    if (Number.isFinite(i) && i >= 1 && i <= n) ids.add(i)
  }
  return [...ids].sort((a, b) => a - b)
}
