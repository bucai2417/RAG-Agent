"""浏览器的「元素级自动化」纯逻辑层：快照编号、ref 解析、步骤校验、等待条件。

为什么单独抽一层：
1. `browser_tools` 里真正难调的不是"能不能点"，而是**点了对不对**：
   AI 只看得到文字，看不到 DOM；如果快照编号与点击时的元素对不上，
   表现是"点了没反应"或"点错按钮"——这类 bug 在真机上极难复现。
2. 等待、下拉、批量步骤的**参数语义**必须在执行前一次性校验完：
   跑到第 7 步才发现 `select` 少给 value，前面的副作用已经产生了。

设计要点（勿凭直觉改动）：

1. **编号只在一次快照内有效**。快照给元素打 `[0] [1] ...`，
   AI 用它回指；页面一变编号就漂移。所以执行前必须用
   `match_candidates` 按「角色 + 名称 + 位置」重新认人，
   **不能**直接拿旧下标去扫 DOM。

2. **快照只收「可交互 + 有可见性」的元素**。
   把所有 div 都编号会淹没上下文（一个页面几千个），
   AI 反而找不到重点；只给按钮/链接/输入框/可点 role。

3. **等待是显式的，不靠 sleep 猜**。
   `wait_for` 以「可见」为准（`display:none` 与零尺寸都算没到），
   给超时后返回"当前实际状态"，让 AI 能决定重试还是换路。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- 常量

# 可交互元素的选择器：覆盖原生控件与常见 ARIA role。
# 刻意不含 `div`/`span` 通配——那会把整页结构都收进来。
CLICKABLE_SELECTOR = (
    "a[href],button,input,textarea,select,summary,"
    "[contenteditable=''], [contenteditable='true'],"
    "[role=button],[role=link],[role=tab],[role=menuitem],"
    "[role=checkbox],[role=radio],[role=switch],[role=option],"
    "[role=combobox],[role=textbox]"
)

# 元素角色归一：DOM 标签 → AI 好理解的语义角色。
# 同一份快照在两个页面之间对比时，靠的是角色名而不是 tagName。
_ROLE_BY_TAG = {
    "a": "link", "button": "button", "input": "input", "textarea": "textbox",
    "select": "select", "summary": "summary", "option": "option",
}

# input 的 type → 角色。checkbox/radio/file 的行为与文本框完全不同，
# 统一叫 input 会让 AI 用 fill 去填复选框。
_ROLE_BY_INPUT_TYPE = {
    "checkbox": "checkbox", "radio": "radio",
    "submit": "button", "button": "button", "reset": "button", "image": "button",
    "file": "file", "color": "color", "range": "slider", "number": "spinbutton",
    "date": "date", "time": "time", "search": "searchbox",
}

# 名称截断：按钮文字偶尔是整段说明，留 80 字够识别，再长只会挤掉别的元素
NAME_MAX = 80

# 单次快照最多编号多少个元素。超出后按「视口内优先」截断并明确告知，
# 而不是静默丢弃（静默丢弃会让 AI 以为页面上没有目标元素）。
MAX_ELEMENTS = 150

# 页面正文默认长度
TEXT_MAX = 6000

# `scroll` 的多轮上限。无限滚动页面上「一直有变化」是常态（时间戳、轮播动画），
# 没有硬上限的话一次工具调用就能把会话挂死在里面。
MAX_SCROLL_ROUNDS = 30

# ---------------------------------------------------------------- 跨框架定位
#
# 支付/登录/验证码/富文本编辑器几乎都是 iframe 内嵌的。只扫顶层文档时，
# AI 会得到「页面上没有这个按钮」这个**错误**结论，然后反复重试或去猜选择器。
#
# 这段前奏被快照与所有元素级操作共用（唯一一份实现，勿复制出第二份）：
# 遍历同源 iframe，把每个文档的**原点偏移**记下来 —— 子文档里的
# getBoundingClientRect 是相对该子文档视口的，而 CDP 的鼠标坐标是相对
# 顶层视口的，不加偏移就会点到别处（通常落在 iframe 边框外，表现为"点了没反应"）。
#
# 跨域 iframe 拿不到 contentDocument，这是浏览器强制的边界。**不能装作读到了**：
# 记下数量和 src，由快照如实告诉 AI，否则它会在一个读不到的区域里无限找下去。
FRAME_PRELUDE = r"""
  const __reaFrames = (function () {
    const docs = [];
    const seen = new Set();
    const cross = [];
    let crossCount = 0;
    // chain：从最外层到这一层所经过的 iframe 元素。滚动时要由外向内逐层滚 ——
    // 只滚里层文档的滚动容器，iframe 本身可能还在父页面视口之外，点不到。
    function walk(d, ox, oy, depth, path, chain) {
      if (!d || seen.has(d) || depth > 4) return;
      seen.add(d);
      docs.push({d: d, ox: ox, oy: oy, depth: depth, path: path, chain: chain});
      let fs = [];
      try { fs = Array.from(d.querySelectorAll('iframe,frame')); } catch (e) { return; }
      for (let i = 0; i < fs.length; i++) {
        const f = fs[i];
        let cd = null;
        try { cd = f.contentDocument || null; } catch (e) { cd = null; }
        if (!cd) {
          crossCount += 1;
          if (cross.length < 6) {
            let src = '';
            try { src = f.getAttribute('src') || ''; } catch (e) { src = ''; }
            cross.push(src.slice(0, 160));
          }
          continue;
        }
        let r;
        try { r = f.getBoundingClientRect(); } catch (e) { continue; }
        // clientLeft/clientTop 是 iframe 边框宽度：内容原点在边框内侧
        walk(cd, ox + r.left + (f.clientLeft || 0),
                 oy + r.top + (f.clientTop || 0),
             depth + 1, path ? path + '>' + i : String(i), chain.concat([f]));
      }
    }
    walk(document, 0, 0, 0, '', []);
    return {docs: docs, cross: cross, crossCount: crossCount};
  })();
  function __reaFind(sel) {
    const ds = __reaFrames.docs;
    for (let i = 0; i < ds.length; i++) {
      let el = null;
      try { el = ds[i].d.querySelector(sel); } catch (e) { el = null; }
      if (el) return {el: el, ox: ds[i].ox, oy: ds[i].oy, depth: ds[i].depth,
                      chain: ds[i].chain};
    }
    return null;
  }
  function __reaRect(hit) {
    const r = hit.el.getBoundingClientRect();
    return {x: r.left + hit.ox, y: r.top + hit.oy, w: r.width, h: r.height};
  }
"""


def with_frames(body: str) -> str:
    """把一段操作脚本包进「已准备好跨框架定位」的 IIFE。

    调用方在 body 里可以直接用 `__reaFrames` / `__reaFind(sel)` / `__reaRect(hit)`。
    """
    return "(() => {\n" + FRAME_PRELUDE + "\n" + body + "\n})()"


# 跨框架版本的 body 别名：cdp_client 里每个元素级动作都要包一层，
# 名字短一点，避免那些表达式被前奏淹没。
frame_script = with_frames


# ---------------------------------------------------------------- 快照脚本

# 这段脚本只在**内部固定用途**下执行（取结构，不接受外部传入代码）。
# 之所以用 data 属性登记 rect 而不是返回 DOM 引用：返回值必须可 JSON 序列化，
# 而 DOM 节点跨 CDP 往返会变成不可读的远程对象。
SNAPSHOT_JS = r"""
(() => {
  const SEL = %SELECTOR%;
  const MAX = %MAX%;
  const TOKEN = %TOKEN%;

  const vw = window.innerWidth || 1280;
  const vh = window.innerHeight || 800;

  %FRAME%

  // 清掉上一次快照留下的标记：旧标记残留会让按 token 查询命中过期元素。
  // **每个文档都要清**：只在顶层清，iframe 里的旧标记会一直命中过期元素。
  for (const f of __reaFrames.docs) {
    try {
      f.d.querySelectorAll('[data-rea-snap]').forEach(el => {
        el.removeAttribute('data-rea-snap');
        el.removeAttribute('data-rea-idx');
      });
    } catch (e) { /* 文档已被卸载 */ }
  }

  function isVisible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    // 必须用元素**自己**的 window 取样式：iframe 里的元素用顶层 getComputedStyle 会抛错
    const win = el.ownerDocument.defaultView || window;
    const st = win.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') return false;
    if (parseFloat(st.opacity || '1') === 0) return false;
    // 完全滚出视口但页面还能滚到的元素仍然可点（会先 scrollIntoView），
    // 所以这里不按视口裁剪，只按「是否真的占位」判断。
    return true;
  }

  function roleOf(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      const map = %ROLE_BY_INPUT%;
      if (map[t]) return map[t];
      return 'textbox';
    }
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.toLowerCase();
    const map = %ROLE_BY_TAG%;
    return map[tag] || tag;
  }

  function labelOf(el) {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    // 原生 <label for> / 包裹式 label：表单最关键的名称来源
    try {
      if (el.labels && el.labels.length) {
        const t = (el.labels[0].innerText || '').trim();
        if (t) return t;
      }
    } catch (e) { /* 某些元素没有 labels 属性 */ }
    const ph = el.getAttribute('placeholder');
    if (ph && ph.trim()) return ph.trim();
    const title = el.getAttribute('title');
    if (title && title.trim()) return title.trim();
    const alt = el.getAttribute('alt');
    if (alt && alt.trim()) return alt.trim();
    const txt = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (txt) return txt;
    const val = el.getAttribute('value');
    if (val && val.trim()) return val.trim();
    return '';
  }

  function checkedOf(el) {
    if (el.tagName.toLowerCase() === 'input') {
      if (el.type === 'checkbox' || el.type === 'radio') return !!el.checked;
      return null;
    }
    const aria = el.getAttribute('aria-checked');
    if (aria === 'true') return true;
    if (aria === 'false') return false;
    return null;
  }

  const all = [];
  const origins = new Map();   // 元素 → 所在文档原点偏移
  const frames = new Map();    // 元素 → 所属框架路径（'' 表示顶层）
  for (const f of __reaFrames.docs) {
    let hits = [];
    try { hits = Array.from(f.d.querySelectorAll(SEL)); } catch (e) { hits = []; }
    for (const el of hits) {
      all.push(el);
      origins.set(el, f);
      frames.set(el, f.path);
    }
  }

  const seen = new Set();
  const picked = [];
  for (const el of all) {
    // 嵌套的同名控件（label 里套 input）只收一次，按最外层可交互元素算
    if (seen.has(el)) continue;
    seen.add(el);
    if (!isVisible(el)) continue;
    picked.push(el);
  }

  // 视口内的排前面：AI 最先要找的几乎总是"屏幕上看得到的东西"。
  // 用换算到顶层视口的绝对坐标排序 —— 子文档里的 top 是相对它自己的，
  // 直接比较会让 iframe 里的元素全都排到最前面或最后面。
  function absTop(el) {
    const o = origins.get(el) || {ox: 0, oy: 0};
    return el.getBoundingClientRect().top + o.oy;
  }
  picked.sort((a, b) => {
    const ta = absTop(a), tb = absTop(b);
    const inA = ta < vh && ta > -1 ? 0 : 1;
    const inB = tb < vh && tb > -1 ? 0 : 1;
    if (inA !== inB) return inA - inB;
    return ta - tb;
  });

  const elements = [];
  const total = picked.length;
  for (let i = 0; i < Math.min(total, MAX); i++) {
    const el = picked[i];
    const r = el.getBoundingClientRect();
    const o = origins.get(el) || {ox: 0, oy: 0};
    const name = labelOf(el);
    // 打标记：后续动作靠 [data-rea-snap=token][data-rea-idx=i] 精确回指这个元素，
    // 不依赖选择器拼接或坐标猜测 —— 页面重排也不会指错人。
    el.setAttribute('data-rea-snap', TOKEN);
    el.setAttribute('data-rea-idx', String(i));
    const item = {
      idx: i,
      role: roleOf(el),
      name: name.slice(0, %NAME_MAX%),
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      checked: checkedOf(el),
      value: (typeof el.value === 'string' ? el.value.slice(0, 100) : null),
      placeholder: el.getAttribute('placeholder') || null,
      rect: {
        // **顶层视口坐标**：CDP 鼠标事件用的是这个坐标系
        x: Math.round(r.left + o.ox), y: Math.round(r.top + o.oy),
        w: Math.round(r.width), h: Math.round(r.height),
      },
    };
    const fp = frames.get(el) || '';
    if (fp) item.frame = fp;
    elements.push(item);
  }

  // 结构摘要：标题层级对"这是哪一页、有几个区块"最有用。
  // 跨文档取标题，否则 iframe 里的正文分节对 AI 完全不可见。
  const headings = [];
  for (const f of __reaFrames.docs) {
    let hs = [];
    try { hs = Array.from(f.d.querySelectorAll('h1,h2,h3')); } catch (e) { hs = []; }
    for (const h of hs) {
      if (headings.length >= 40) break;
      const t = (h.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120);
      if (t) headings.push({level: Number(h.tagName.slice(1)), text: t});
    }
  }

  // 正文：去掉脚本样式，与 get_text 同源但更短（快照要留给元素列表）。
  // iframe 的正文单独取并标出所属框架 —— 支付页的关键信息（金额、按钮文案）
  // 往往**只在 iframe 里**，丢掉它等于让 AI 看不见要点的东西。
  function docText(f) {
    const src = f.d.body || f.d.documentElement;
    if (!src) return '';
    let noise = [];
    try {
      noise = Array.from(src.querySelectorAll('script,style,noscript,svg,canvas,iframe,frame,template'));
    } catch (e) { noise = []; }
    const saved = noise.map(el => [el, el.style.display]);
    noise.forEach(el => { el.style.display = 'none'; });
    let t = '';
    try {
      t = (src.innerText || '').trim();
      if (!t) t = (src.textContent || '').trim();
    } finally {
      saved.forEach(([el, prev]) => { el.style.display = prev; });
    }
    return t;
  }
  let text = docText(__reaFrames.docs[0] || {});
  const frameTexts = [];
  for (let i = 1; i < __reaFrames.docs.length; i++) {
    const t = docText(__reaFrames.docs[i]);
    if (t) frameTexts.push({frame: __reaFrames.docs[i].path, text: t.slice(0, 4000)});
  }

  return {
    title: document.title || '',
    url: location.href,
    token: TOKEN,
    viewport: { w: vw, h: vh },
    scroll: {
      x: Math.round(window.scrollX || 0),
      y: Math.round(window.scrollY || 0),
      max_x: Math.round(Math.max(0, (document.documentElement.scrollWidth || 0) - vw)),
      max_y: Math.round(Math.max(0, (document.documentElement.scrollHeight || 0) - vh)),
    },
    total_elements: total,
    truncated: total > MAX,
    elements,
    headings,
    iframes: {count: __reaFrames.docs.length - 1, cross_origin: __reaFrames.crossCount,
              cross_srcs: __reaFrames.cross},
    frame_texts: frameTexts,
    text: text.replace(/\n{3,}/g, '\n\n').trim(),
  };
})()
"""


def snapshot_script(max_elements: int = MAX_ELEMENTS, token: str = "") -> str:
    """把选择器/角色表/快照令牌嵌进 JS。

    用字符串替换而不是 f-string：JS 里有大量 `{}`，
    走 f-string 要么全转义（易错）要么引入 `%` 与 `%s` 的歧义。
    """
    cap = max(10, min(int(max_elements or MAX_ELEMENTS), MAX_ELEMENTS))
    tok = token or "x"
    return (
        SNAPSHOT_JS
        .replace("%FRAME%", FRAME_PRELUDE)
        .replace("%SELECTOR%", json.dumps(CLICKABLE_SELECTOR))
        .replace("%ROLE_BY_INPUT%", json.dumps(_ROLE_BY_INPUT_TYPE))
        .replace("%ROLE_BY_TAG%", json.dumps(_ROLE_BY_TAG))
        .replace("%NAME_MAX%", str(NAME_MAX))
        .replace("%MAX%", str(cap))
        .replace("%TOKEN%", json.dumps(tok))
    )


# ---------------------------------------------------------------- 元素标记


def marked_selector(token: str, idx: int) -> str:
    """快照标记的选择器：`[data-rea-snap=token][data-rea-idx=idx]`。

    这是**最可靠**的回指方式：元素身份由浏览器自己维护的属性确定，
    不像「按名称拼选择器」那样怕重名、不怕文案被改写。
    只有页面在两次调用间整体重渲染（标记丢失）时才需要退回匹配逻辑。
    """
    return f'[data-rea-snap="{token}"][data-rea-idx="{int(idx)}"]'


# ---------------------------------------------------------------- 元素描述


@dataclass(frozen=True)
class Element:
    """快照里的一个可交互元素（AI 的 ref 就指向它）。"""

    idx: int
    role: str
    name: str
    tag: str = ""
    type: str = ""
    disabled: bool = False
    checked: bool | None = None
    value: str | None = None
    placeholder: str | None = None
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    # 元素所在的 iframe 路径（'' = 顶层）。只用于向 AI 解释"这个元素在哪个框里"，
    # 定位本身靠标记属性 —— 标记在哪个文档里，querySelector 就要在哪个文档里找。
    frame: str = ""

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    @classmethod
    def from_raw(cls, raw: dict) -> "Element":
        r = raw.get("rect") or {}

        def _f(v: Any) -> float:
            try:
                f = float(v)
            except (TypeError, ValueError):
                return 0.0
            # NaN/inf 会让坐标计算失去意义，兜成 0 而不是把异常抛给上层
            return 0.0 if f != f or f in (float("inf"), float("-inf")) else f

        checked = raw.get("checked")
        return cls(
            idx=int(raw.get("idx") or 0),
            role=str(raw.get("role") or ""),
            name=str(raw.get("name") or ""),
            tag=str(raw.get("tag") or ""),
            type=str(raw.get("type") or ""),
            disabled=bool(raw.get("disabled")),
            checked=checked if isinstance(checked, bool) else None,
            value=raw.get("value") if isinstance(raw.get("value"), str) else None,
            placeholder=raw.get("placeholder") if isinstance(raw.get("placeholder"), str) else None,
            x=_f(r.get("x")), y=_f(r.get("y")),
            w=_f(r.get("w")), h=_f(r.get("h")),
            frame=str(raw.get("frame") or ""),
        )


@dataclass
class PageSnapshot:
    """一次快照的完整结果。elements 的下标就是 AI 使用的 ref。"""

    title: str = ""
    url: str = ""
    token: str = ""
    elements: list[Element] = field(default_factory=list)
    headings: list[dict] = field(default_factory=list)
    text: str = ""
    viewport_w: float = 1280.0
    viewport_h: float = 800.0
    scroll_y: float = 0.0
    scroll_max_y: float = 0.0
    total_elements: int = 0
    truncated: bool = False
    # 同源 iframe 的个数（不含顶层）与跨域 iframe 的个数/src。
    # 跨域个数必须如实带给 AI：读不到 ≠ 没有，混为一谈会让它一直找下去。
    iframe_count: int = 0
    cross_iframe_count: int = 0
    cross_iframe_srcs: list[str] = field(default_factory=list)
    # 同源 iframe 里的正文：支付/登录页的关键信息常常只在这里
    frame_texts: list[dict] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: object) -> "PageSnapshot":
        if not isinstance(raw, dict):
            return cls()
        vp = raw.get("viewport") or {}
        sc = raw.get("scroll") or {}
        fr = raw.get("iframes") or {}
        els = [
            Element.from_raw(e)
            for e in (raw.get("elements") or [])
            if isinstance(e, dict)
        ]
        return cls(
            title=str(raw.get("title") or ""),
            url=str(raw.get("url") or ""),
            token=str(raw.get("token") or ""),
            elements=els,
            headings=[h for h in (raw.get("headings") or []) if isinstance(h, dict)],
            text=str(raw.get("text") or ""),
            viewport_w=float(vp.get("w") or 1280),
            viewport_h=float(vp.get("h") or 800),
            scroll_y=float(sc.get("y") or 0),
            scroll_max_y=float(sc.get("max_y") or 0),
            total_elements=int(raw.get("total_elements") or len(els)),
            truncated=bool(raw.get("truncated")),
            iframe_count=int(fr.get("count") or 0),
            cross_iframe_count=int(fr.get("cross_origin") or 0),
            cross_iframe_srcs=[
                str(s) for s in (fr.get("cross_srcs") or []) if isinstance(s, str)
            ],
            frame_texts=[
                f for f in (raw.get("frame_texts") or []) if isinstance(f, dict)
            ],
        )

    def by_ref(self, ref: object) -> Element | None:
        """按编号取元素；越界返回 None（不抛错，让调用方给可读提示）。"""
        try:
            i = int(ref)
        except (TypeError, ValueError):
            return None
        if 0 <= i < len(self.elements):
            return self.elements[i]
        return None

    def find(self, text: str, *, role: str | None = None) -> Element | None:
        """按名称子串找第一个匹配元素（AI 没给 ref 时的兜底路径）。"""
        t = (text or "").strip().lower()
        if not t:
            return None
        for el in self.elements:
            if role and el.role != role:
                continue
            if t in el.name.lower():
                return el
        return None


# ---------------------------------------------------------------- 快照格式化


def format_snapshot(
    snap: PageSnapshot, *, max_text: int = TEXT_MAX, max_elements: int = MAX_ELEMENTS,
) -> str:
    """把快照渲染成给 LLM 看的紧凑文本。

    为什么要紧凑：一个电商首页的按钮就有 60+ 个，
    逐个带坐标会把上下文吃掉；坐标只在"需要精确点某处"时才展示。
    """
    lines: list[str] = []
    head = snap.title.strip() or "(无标题)"
    lines.append(f"# {head}")
    lines.append(snap.url)
    lines.append("")
    lines.append(
        f"视口 {int(snap.viewport_w)}×{int(snap.viewport_h)}，"
        f"已滚动 {int(snap.scroll_y)}/{int(snap.scroll_max_y)}，"
        f"可交互元素 {snap.total_elements} 个"
        + ("（已截断，只列前若干个）" if snap.truncated else "")
    )
    # iframe 必须显式说明：读不到的内容如果被静默跳过，AI 会认定"页面上没有"，
    # 然后在正确的页面上反复重试或去猜选择器。
    if snap.iframe_count:
        lines.append(f"其中 {snap.iframe_count} 个内嵌框架（iframe）里的元素已一并列出")
    if snap.cross_iframe_count:
        srcs = "、".join(s or "(无 src)" for s in snap.cross_iframe_srcs[:3])
        lines.append(
            f"另有 {snap.cross_iframe_count} 个**跨域 iframe 无法读取**（{srcs}）——"
            "它们的按钮/输入框不在下面列表里。若目标在支付/登录框内，"
            "需要用户在浏览器窗口里手动点击，或改用该框的独立页面地址。"
        )
    lines.append("")

    if snap.headings:
        lines.append("## 页面结构")
        for h in snap.headings[:20]:
            try:
                lvl = int(h.get("level") or 2)
            except (TypeError, ValueError):
                lvl = 2
            lines.append(f"{'  ' * max(0, lvl - 1)}- {h.get('text') or ''}")
        lines.append("")

    if snap.elements:
        lines.append("## 可交互元素（[编号] 角色 \"名称\"）")
        for el in snap.elements[:max_elements]:
            bits = [f"[{el.idx}]", el.role]
            if el.name:
                bits.append(f'"{el.name}"')
            else:
                # 无名称元素对 AI 毫无用处（点它等于盲点），必须标出来
                bits.append('"(无名称)"')
            notes: list[str] = []
            if el.type and el.type not in ("text", ""):
                notes.append(f"type={el.type}")
            if el.disabled:
                notes.append("disabled")
            if el.checked is True:
                notes.append("已勾选")
            elif el.checked is False:
                notes.append("未勾选")
            if el.value and el.role in ("textbox", "searchbox", "spinbutton", "combobox"):
                notes.append(f"value={el.value!r}")
            if el.frame:
                # 只标"在某个框里"，不铺开路径：AI 需要知道的是"这一项在别处"，
                # 而不是框架树的编号 —— 后者对它毫无用处，只会占上下文。
                notes.append("in iframe")
            if notes:
                bits.append("(" + ", ".join(notes) + ")")
            lines.append(" ".join(bits))
        lines.append("")

    body = (snap.text or "").strip()
    if body:
        if len(body) > max_text:
            body = body[:max_text] + f"\n…（正文已截断，全文 {len(snap.text)} 字）"
        lines.append("## 正文")
        lines.append(body)

    if snap.frame_texts:
        lines.append("")
        lines.append("## 内嵌框架正文")
        for ft in snap.frame_texts:
            t = str(ft.get("text") or "").strip()
            if not t:
                continue
            tag = str(ft.get("frame") or "")
            lines.append(f"### 框架 {tag}" if tag else "### 框架")
            lines.append(t[:2000])

    return "\n".join(lines).strip()


# ---------------------------------------------------------------- ref 匹配


def _name_key(name: str) -> str:
    """名称归一：空白折叠、大小写无关。

    页面里同一个按钮的文本常因换行/空格不同而"看起来不同"，
    直接比较会让匹配率低到不可用。
    """
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def match_candidates(
    want: Element, candidates: list[Element], *, tolerance: float = 24.0,
) -> Element | None:
    """把快照里的元素重新对上页面当前元素（快照可能已过期）。

    匹配优先级（从强到弱，命中即返回）：
    1. 角色 + 名称 + 位置都一致  → 几乎可以确定是同一个元素
    2. 角色 + 名称一致           → 文案没变，页面可能只是滚动了
    3. 位置一致（容差内）        → 文案变了（如"加入购物车"→"继续购物"）
    4. 只有名称一致              → 角色被换掉（a→button 之类）

    把位置放在第 3 而不是第 1 顺位：列表页里同名按钮极多
    （"删除"重复 20 次），只靠名称会点到别的行去。
    """
    wn = _name_key(want.name)
    wx, wy = want.center

    def _pos_ok(c: Element) -> bool:
        cx, cy = c.center
        return abs(cx - wx) <= tolerance and abs(cy - wy) <= tolerance

    for c in candidates:
        if c.role == want.role and _name_key(c.name) == wn and wn and _pos_ok(c):
            return c
    for c in candidates:
        if c.role == want.role and _name_key(c.name) == wn and wn:
            return c
    for c in candidates:
        if c.role == want.role and _pos_ok(c):
            return c
    for c in candidates:
        if wn and _name_key(c.name) == wn:
            return c
    return None


# ---------------------------------------------------------------- 步骤校验


# 每个动作需要的字段。`*` 表示二选一（ref 或 text）。
_STEP_REQUIRED: dict[str, tuple[str, ...]] = {
    "click": (),
    "dblclick": (),
    "fill": ("value",),
    "type": ("text",),
    "select": ("value",),
    "check": (),
    "uncheck": (),
    "hover": (),
    "press": ("key",),
    "scroll": (),
    "navigate": ("url",),
    "back": (),
    "reload": (),
    "wait_for": (),
    "wait": (),
    "read": (),
    "screenshot": (),
    "assert_text": ("text",),
}

# 需要「目标元素」的动作：必须给 ref 或 text（名称子串）
_NEEDS_TARGET = {
    "click", "dblclick", "fill", "type", "select", "check", "uncheck", "hover",
}

_NAV_SCHEMES = ("http://", "https://")


@dataclass
class Step:
    """校验后的单个步骤。"""

    action: str
    ref: int | None = None
    text: str = ""
    value: str = ""
    url: str = ""
    key: str = ""
    dy: float = 0.0
    timeout_ms: int = 0
    wait_ms: int = 0
    # 列表懒加载：重复滚动直到「没有新内容」为止。
    # 0 表示只滚一次（老行为）；>0 表示最多滚几轮。
    rounds: int = 0
    # 每轮之间等多久（毫秒）：无限滚动的加载是异步的，
    # 滚完立刻判断会得到「没变化」，于是提前停止。
    interval_ms: int = 0

    def label(self) -> str:
        """给日志/回显的一句话描述。"""
        tgt = f"#{self.ref}" if self.ref is not None else (self.text or "")
        if self.action in ("click", "dblclick", "hover", "check", "uncheck"):
            return f"{self.action} {tgt}".strip()
        if self.action == "fill":
            return f"fill {tgt} = {self.value[:40]!r}".strip()
        if self.action == "type":
            return f"type {tgt} {self.text[:40]!r}".strip()
        if self.action == "select":
            return f"select {tgt} = {self.value!r}".strip()
        if self.action == "press":
            return f"press {self.key}".strip()
        if self.action == "navigate":
            return f"navigate {self.url}"
        if self.action == "scroll":
            if self.rounds:
                return f"scroll x{self.rounds} dy={self.dy:+.0f}"
            return f"scroll dy={self.dy:+.0f}"
        if self.action == "wait":
            return f"wait {self.wait_ms}ms"
        if self.action == "wait_for":
            return f"wait_for {tgt or self.text}".strip()
        if self.action == "assert_text":
            return f"assert_text {self.text[:40]!r}"
        return self.action


def _as_float(v: object, default: float = 0.0) -> float:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return 0.0 if f != f or f in (float("inf"), float("-inf")) else f


def _as_int(v: object, default: int = 0) -> int:
    f = _as_float(v, float(default))
    return int(f)


def parse_step(raw: object, index: int = 0) -> Step:
    """校验并规范化一个步骤；不合法抛 ValueError（带步骤序号，便于定位）。

    一次性校验整个列表再执行：跑到第 7 步才发现参数缺失时，
    前 6 步的副作用已经落到页面上，回滚不了。
    """
    where = f"第 {index + 1} 步"
    if not isinstance(raw, dict):
        raise ValueError(f"{where}必须是对象")
    action = str(raw.get("action") or "").strip().lower()
    if action not in _STEP_REQUIRED:
        raise ValueError(
            f"{where}的 action 不支持：{action!r}（可选："
            + "、".join(sorted(_STEP_REQUIRED)) + "）"
        )

    step = Step(action=action)

    ref = raw.get("ref")
    if ref is not None and ref != "":
        step.ref = _as_int(ref, -1)
        if step.ref < 0:
            raise ValueError(f"{where}的 ref 必须是非负整数（收到 {ref!r}）")

    step.text = str(raw.get("text") or "").strip()
    step.value = str(raw.get("value") if raw.get("value") is not None else "")
    step.url = str(raw.get("url") or "").strip()
    step.key = str(raw.get("key") or "").strip()
    step.dy = _as_float(raw.get("dy"), 0.0)
    step.timeout_ms = max(0, min(_as_int(raw.get("timeout_ms"), 10000), 120000))
    step.wait_ms = max(0, min(_as_int(raw.get("ms"), 1000), 60000))
    # 轮数上限 30：无限滚动页面上"一直有变化"（时间戳、动画）会让循环永不结束，
    # 必须有个硬上限，否则一次工具调用就能把整个会话挂死在里面。
    step.rounds = max(0, min(_as_int(raw.get("rounds"), 0), MAX_SCROLL_ROUNDS))
    step.interval_ms = max(0, min(_as_int(raw.get("interval_ms"), 0), 10000))

    # rounds 只对 scroll 有意义：别的动作传了它一定是模型误解了参数，
    # 静默忽略会让它以为"我让它滚了 5 轮"而实际没滚。
    if step.rounds and action != "scroll":
        raise ValueError(f"{where}（{action}）不支持 rounds，只有 scroll 可以多轮")

    for f_ in _STEP_REQUIRED[action]:
        if not getattr(step, f_):
            raise ValueError(f"{where}（{action}）缺少 {f_}")

    if action in _NEEDS_TARGET and step.ref is None and not step.text:
        raise ValueError(f"{where}（{action}）需要 ref 或 text 指定目标元素")

    if action == "navigate":
        if not step.url.lower().startswith(_NAV_SCHEMES):
            # 与实时面板同一口径：不允许 file:// 或 chrome:// 进来
            raise ValueError(f"{where}（navigate）只支持 http/https 地址")
    if action == "wait_for" and step.ref is None and not step.text:
        raise ValueError(f"{where}（wait_for）需要 ref 或 text")
    if action == "press" and not step.key:
        raise ValueError(f"{where}（press）缺少 key")

    return step


def parse_steps(raw: object) -> list[Step]:
    """把 tool 传入的步骤数组整体校验。空数组视为错误（调用方应改用单动作）。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("steps 必须是非空数组")
    return [parse_step(s, i) for i, s in enumerate(raw)]


# ---------------------------------------------------------------- 等待条件

def wait_script(*, selector: str = "", text: str = "") -> str:
    """生成「等某元素可见」的判断脚本，返回布尔值。

    以「可见」而不是「存在」为准：React 里常有先挂载再淡入的做法，
    元素存在但不可见时点击会落在透明层上，等于没点。

    同样跨同源 iframe 查找：验证码/支付按钮在 iframe 里时，
    只看顶层文档会让 `wait_for` 一直超时 —— 而元素其实早就出现了。
    """
    if selector:
        body = (
            "const hit = __reaFind(%s);"
            " if (!hit) return false;"
            " const el = hit.el;"
            " const r = el.getBoundingClientRect();"
            " if (r.width < 1 || r.height < 1) return false;"
            " const W = el.ownerDocument.defaultView || window;"
            " const st = W.getComputedStyle(el);"
            " return st.visibility !== 'hidden' && st.display !== 'none';"
            % json.dumps(selector)
        )
        return with_frames("try {" + body + "} catch (e) { return false; }")

    # 文本等待：在**每个**文档的可见元素里找包含该文本的节点。
    # 只扫顶层时，iframe 里已经渲染出来的"支付成功"会被判成不存在。
    body = (
        "const needle = %s;"
        " for (const f of __reaFrames.docs) {"
        "   let all = [];"
        "   try { all = Array.from(f.d.querySelectorAll('body *')); } catch (e) { continue; }"
        "   for (const n of all) {"
        "     const t = (n.innerText || '').trim();"
        "     if (!t || t.indexOf(needle) < 0) continue;"
        "     const r = n.getBoundingClientRect();"
        "     if (r.width < 1 || r.height < 1) continue;"
        "     const W = n.ownerDocument.defaultView || window;"
        "     const st = W.getComputedStyle(n);"
        "     if (st.visibility === 'hidden' || st.display === 'none') continue;"
        "     return true;"
        "   }"
        " }"
        " return false;" % json.dumps(text)
    )
    return with_frames("try {" + body + "} catch (e) { return false; }")


# ---------------------------------------------------------------- 面板跟随


def center_normalized(rect: dict, page_w: float, page_h: float) -> tuple[float, float]:
    """元素中心 → 面板归一化坐标（0~1），供实时画面高亮/指针跟随。

    越界夹紧而不是丢弃：元素可能一半在视口外，
    夹到边上至少能让用户看到"AI 正在往那个方向动"。
    """
    x = _as_float((rect or {}).get("x")) + _as_float((rect or {}).get("w")) / 2
    y = _as_float((rect or {}).get("y")) + _as_float((rect or {}).get("h")) / 2
    pw = max(1.0, _as_float(page_w, 1.0))
    ph = max(1.0, _as_float(page_h, 1.0))
    nx = min(max(0.0, x / pw), 1.0)
    ny = min(max(0.0, y / ph), 1.0)
    return nx, ny


# ---------------------------------------------------------------- 懒加载列表
#
# 电商的订单列表、搜索结果、商品流几乎都是懒加载：首屏只渲染十几个，
# 滚到底才追加。AI 拿到第一屏快照后如果不滚，会得出「只有 12 条订单」的
# **错误**结论 —— 它不是没找到，是没看到。
#
# 判据取「文档高度 + 元素数」：只看高度会漏掉等高的替换式加载，
# 只看元素数会被轮播/动画的增删误判成"一直有新内容"。


def load_probe_script() -> str:
    """返回「当前加载了多少内容」的快照指标，供多轮滚动比对新内容。

    `sig` 是给调用方直接比对的指纹；`h`/`n` 用于向 AI 解释为什么停。

    `atBottom` 的判据必须包含**滚动容器**：列表放在 `overflow:auto` 的 div 里时，
    文档本身高度不变（window 永远"到底"），只按 window 判断会在第一轮就得出
    「已到底」，多轮滚动退化成零轮 —— 而列表其实还能继续加载。
    因此这里报「文档到底了 **且** 没有未到底的可滚容器」。
    """
    body = (
        "const d = document.documentElement || {};"
        "const b = document.body || {};"
        "const h = Math.max(d.scrollHeight || 0, b.scrollHeight || 0);"
        "const n = document.getElementsByTagName('*').length || 0;"
        "const y = Math.round(window.scrollY || window.pageYOffset || 0);"
        "const vh = window.innerHeight || 0;"
        "const winBottom = (y + vh) >= (h - 8);"
        # 还有没滚到底的可滚容器 → 内容没看完，不能算到底
        "let boxLeft = false, boxH = 0;"
        "try {"
        "  const els = b ? b.querySelectorAll('*') : [];"
        "  for (const el of els) {"
        "    const sh = el.scrollHeight || 0;"
        "    if (sh - (el.clientHeight || 0) < 40) continue;"
        "    if ((el.clientHeight || 0) < 80) continue;"
        "    boxH = Math.max(boxH, sh);"
        "    if ((el.scrollTop + el.clientHeight) < (sh - 8)) { boxLeft = true; break; }"
        "  }"
        "} catch (e) {}"
        "return {h: Math.max(h, boxH), n: n, y: y, vh: vh,"
        "        atBottom: winBottom && !boxLeft,"
        # 指纹里带上容器高度：替换式加载时文档高度不变，但容器高度会变
        "        sig: h + ':' + boxH + ':' + n};"
    )
    return with_frames("try {" + body + "} catch (e) { return null; }")


# 滚动容器兜底：很多电商把列表放在 `overflow:auto` 的 div 里，
# 滚 window 完全没反应（页面高度不变），表现为「AI 滚了但什么都没加载」。
# 这里找出「可滚且明显没滚到底」的最大容器，作为滚轮落点。
#
# 与 `load_probe_script` 的 atBottom 同一口径：有容器判断时**只看容器**，
# 混用 window 与容器的判据会让两边得出相反的结论（容器滚到底了但 window 没到底，
# 于是继续滚 window —— 落在容器上，什么都不会发生）。
SCROLL_TARGET_SCRIPT = r"""
(() => {
  const vh = window.innerHeight || 800;
  const vw = window.innerWidth || 1280;
  function scrollable(el) {
    if (!el || !el.scrollHeight) return 0;
    if (el.scrollHeight - el.clientHeight < 40) return 0;
    const r = el.getBoundingClientRect();
    if (r.width < 80 || r.height < 80) return 0;
    return el.clientHeight;
  }
  let best = null, bestScore = 0;
  const els = document.body ? document.body.querySelectorAll('*') : [];
  for (const el of els) {
    const s = scrollable(el);
    if (s > bestScore) { best = el; bestScore = s; }
  }
  if (!best) {
    const h = document.documentElement.scrollHeight || 0;
    return {target: 'window', x: vw / 2, y: vh / 2,
            atBottom: (window.scrollY + vh) >= (h - 8)};
  }
  const r = best.getBoundingClientRect();
  return {
    target: 'element',
    x: r.left + r.width / 2,
    y: r.top + Math.min(r.height / 2, vh / 2),
    atBottom: (best.scrollTop + best.clientHeight) >= (best.scrollHeight - 8),
  };
})()
"""
