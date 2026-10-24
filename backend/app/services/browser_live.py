"""实时画面会话语义（纯函数部分）：坐标换算、按键映射、消息校验、帧节流。

为什么单独抽出来：
1. 这几件事都是**没有浏览器也能断言**的纯逻辑，混进 WS 处理里就只能靠真机碰运气；
2. 坐标换算是这套功能里最易错的一环（帧被 CDP 缩放过、面板又被 CSS 缩放过，
   两次缩放必须分开算并各自兜住边界），必须能被单元测试钉死。

设计要点（勿凭直觉改动）：

1. **两次缩放不能合并**。screencast 帧的尺寸是浏览器按 `maxWidth/maxHeight`
   等比缩小后的结果，与页面 CSS 视口不同；前端 canvas 又按面板宽度再缩一次。
   前端上报的是"帧内像素"，这里要乘 `page_w / frame_w` 才回到页面坐标。
   直接把面板像素当页面坐标，点击会整体偏移 —— 面板越窄偏得越多。

2. **越界要夹紧而不是丢弃**。鼠标拖拽时指针很容易移出面板，若丢弃这些点，
   页面上的拖拽会中断（DOM 收不到 mousemove）；夹到边界上更接近真实行为。

3. **按键映射表只收常用键**。未知键返回 None 由调用方走 `insertText` 兜底，
   不要瞎猜 keyCode —— 猜错的 keyCode 可能触发完全不相干的快捷键。
"""
from __future__ import annotations

from dataclasses import dataclass

# 帧尺寸下限：CDP 偶尔会给出 0 或异常小的尺寸（页面刚导航完），
# 用它做除数会得到无穷大坐标，必须有个可信下限。
_MIN_DIM = 1.0

# modifier 位掩码，与 CDP Input 的约定一致
MOD_ALT = 1
MOD_CTRL = 2
MOD_META = 4
MOD_SHIFT = 8

_MOD_KEYS = {"Alt": MOD_ALT, "Control": MOD_CTRL, "Meta": MOD_META, "Shift": MOD_SHIFT}


@dataclass(frozen=True)
class KeyInfo:
    """一个按键的 CDP 三要素。"""

    key: str      # KeyboardEvent.key
    code: str     # KeyboardEvent.code
    key_code: int  # windowsVirtualKeyCode


# 只收录面板里真正会用到的键。方向键/回车/退格/Tab/Esc 是网页表单的刚需。
_KEYS: dict[str, KeyInfo] = {
    "Enter": KeyInfo("Enter", "Enter", 13),
    "Tab": KeyInfo("Tab", "Tab", 9),
    "Backspace": KeyInfo("Backspace", "Backspace", 8),
    "Escape": KeyInfo("Escape", "Escape", 27),
    "Delete": KeyInfo("Delete", "Delete", 46),
    "ArrowLeft": KeyInfo("ArrowLeft", "ArrowLeft", 37),
    "ArrowUp": KeyInfo("ArrowUp", "ArrowUp", 38),
    "ArrowRight": KeyInfo("ArrowRight", "ArrowRight", 39),
    "ArrowDown": KeyInfo("ArrowDown", "ArrowDown", 40),
    "Home": KeyInfo("Home", "Home", 36),
    "End": KeyInfo("End", "End", 35),
    "PageUp": KeyInfo("PageUp", "PageUp", 33),
    "PageDown": KeyInfo("PageDown", "PageDown", 34),
    " ": KeyInfo(" ", "Space", 32),
    "Space": KeyInfo(" ", "Space", 32),
}
# 标点与符号：网页搜索框/表单里高频，缺了就只能走 insertText 兜底（部分站点不认）
_PUNCT = {
    "-": ("Minus", 189), "=": ("Equal", 187), "[": ("BracketLeft", 219),
    "]": ("BracketRight", 221), "\\": ("Backslash", 220), ";": ("Semicolon", 186),
    "'": ("Quote", 222), ",": ("Comma", 188), ".": ("Period", 190),
    "/": ("Slash", 191), "`": ("Backquote", 192),
    "!": ("Digit1", 49), "@": ("Digit2", 50), "#": ("Digit3", 51),
    "$": ("Digit4", 52), "%": ("Digit5", 53), "^": ("Digit6", 54),
    "&": ("Digit7", 55), "*": ("Digit8", 56), "(": ("Digit9", 57),
    ")": ("Digit0", 48), "_": ("Minus", 189), "+": ("Equal", 187),
    "{": ("BracketLeft", 219), "}": ("BracketRight", 221),
    "|": ("Backslash", 220), ":": ("Semicolon", 186), '"': ("Quote", 222),
    "<": ("Comma", 188), ">": ("Period", 190), "?": ("Slash", 191),
    "~": ("Backquote", 192),
}
for _p, (_code, _kc) in _PUNCT.items():
    _KEYS[_p] = KeyInfo(_p, _code, _kc)
# 字母与数字：key/code/keyCode 三者规则固定，用循环生成比手抄 36 行可靠。
# key 必须保留大小写 —— 页面靠 KeyboardEvent.key 区分 Shift+A 与 a
# （code/keyCode 才是大小写无关的物理键位）。
for _c in "abcdefghijklmnopqrstuvwxyz":
    _up = _c.upper()
    _KEYS[_c] = KeyInfo(_c, f"Key{_up}", ord(_up))
    _KEYS[_up] = KeyInfo(_up, f"Key{_up}", ord(_up))
for _d in "0123456789":
    _KEYS[_d] = KeyInfo(_d, f"Digit{_d}", ord(_d))


def map_key(key: str) -> KeyInfo | None:
    """前端 `KeyboardEvent.key` → CDP 按键三要素；不认识则 None。

    未命中时调用方应退回 `Input.insertText`：猜 keyCode 会触发不相干的快捷键。
    """
    return _KEYS.get(key)


def command_for(key: str, modifiers: str) -> str | None:
    """修饰键自身的变化 → Input 的按键类型。

    修饰键必须显式发 keyDown/keyUp：否则页面在按 Ctrl 期间收不到修饰状态，
    `Ctrl+A` 这类组合会被识别成单独的 'a'。`rawKeyDown`/`rawKeyUp` 不改文本，正合适。
    """
    if key not in _MOD_KEYS:
        return None
    return "rawKeyDown" if modifiers == "down" else "rawKeyUp"


def modifiers_from(names: list[str] | None) -> int:
    """把前端上报的修饰键名列表折算成位掩码。未知名称忽略（不自造位）。"""
    total = 0
    for n in names or []:
        total |= _MOD_KEYS.get(str(n), 0)
    return total


# ---- 坐标换算 ----


def _num(v: object, default: float = 0.0) -> float:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    # NaN/inf 会让 CDP 直接报协议错误，且报错信息毫无指向性，这里先挡掉
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def scale_normalized(
    nx: object, ny: object, *, page_w: object, page_h: object,
) -> tuple[float, float]:
    """面板归一化坐标（0~1）→ 页面 CSS 像素，并夹紧到视口内。

    为什么用归一化而不是帧像素：帧的尺寸取决于 CDP 的 `maxWidth/maxHeight`
    与页面宽高比，前端 canvas 又按面板宽度缩过一次；只要任何一环取值不同步，
    按帧像素算就会整体偏移（面板越窄偏得越多）。归一化把"前端怎么显示"和
    "页面多宽"彻底解耦：前端只报指针在画面里的相对位置，缩放全在浏览器一侧算。
    """
    pw = max(_MIN_DIM, _num(page_w, _MIN_DIM))
    ph = max(_MIN_DIM, _num(page_h, _MIN_DIM))

    # 越界点夹到 [0, 1]：拖拽时指针常移出面板，丢弃会让拖拽中断
    x = min(max(0.0, _num(nx)), 1.0) * pw
    y = min(max(0.0, _num(ny)), 1.0) * ph
    # 再夹到 [0, w-1]：页面坐标左闭右开，等于宽度会落到视口外
    return min(x, pw - 1.0), min(y, ph - 1.0)


def viewport_of(metrics: object) -> tuple[float, float]:
    """从 `Page.getLayoutMetrics` 结果里取 CSS 视口宽高。

    优先 `cssLayoutViewport`（CSS 像素，与 Input 事件同一坐标系），
    退回 `layoutViewport`；都没有时给安全默认值而不是抛错 ——
    拿不到尺寸时面板仍可显示画面，只是暂不能点击。
    """
    if isinstance(metrics, dict):
        for key in ("cssLayoutViewport", "layoutViewport"):
            vp = metrics.get(key)
            if isinstance(vp, dict):
                w = _num(vp.get("clientWidth"), 0.0)
                h = _num(vp.get("clientHeight"), 0.0)
                if w > 0 and h > 0:
                    return w, h
    return 1280.0, 800.0


# ---- 消息校验 ----


class BadMessage(ValueError):
    """客户端消息不合法。带上可读原因，前端能直接展示。"""


_ALLOWED = {
    "ping", "attach", "tabs", "switch_tab", "mouse", "wheel", "key", "text",
    "navigate", "reload", "resize", "quality",
}

_MOUSE_TYPES = {"mousePressed", "mouseReleased", "mouseMoved"}
_BUTTONS = {"none", "left", "middle", "right", "back", "forward"}


def validate_message(msg: object) -> dict:
    """校验并规范化前端消息；不合法抛 BadMessage。

    白名单式校验：动作集合固定，**不接受**任意 CDP 方法名透传 ——
    否则这个 WS 就等价于给前端一把能 `Runtime.evaluate` 的钥匙。
    """
    if not isinstance(msg, dict):
        raise BadMessage("消息必须是对象")
    t = msg.get("type")
    if not isinstance(t, str) or t not in _ALLOWED:
        raise BadMessage(f"不支持的操作：{t!r}")

    out: dict = {"type": t}
    if t == "mouse":
        mt = msg.get("event")
        if mt not in _MOUSE_TYPES:
            raise BadMessage(f"mouse.event 非法：{mt!r}")
        btn = msg.get("button") or "none"
        if btn not in _BUTTONS:
            raise BadMessage(f"mouse.button 非法：{btn!r}")
        out.update(
            event=mt, button=btn,
            nx=_num(msg.get("nx")), ny=_num(msg.get("ny")),
            click_count=max(1, min(int(_num(msg.get("clickCount"), 1)), 3)),
            modifiers=modifiers_from(msg.get("modifiers")),
        )
    elif t == "wheel":
        out.update(
            nx=_num(msg.get("nx")), ny=_num(msg.get("ny")),
            delta_x=_num(msg.get("deltaX")), delta_y=_num(msg.get("deltaY")),
            modifiers=modifiers_from(msg.get("modifiers")),
        )
    elif t == "key":
        k = msg.get("key")
        if not isinstance(k, str) or not k:
            raise BadMessage("key.key 必填")
        # 修饰键按下/抬起必须原样保留：统一成 down 会让页面永远"按着"该键
        mod = msg.get("mod")
        out.update(
            key=k,
            mod=mod if mod in ("down", "up") else "press",
            # 前端没给可打印字符时，单个可见字符就是它自己
            text=msg.get("text") if isinstance(msg.get("text"), str) else None,
            modifiers=modifiers_from(msg.get("modifiers")),
        )
    elif t == "text":
        v = msg.get("text")
        if not isinstance(v, str):
            raise BadMessage("text.text 必填")
        if len(v) > 20000:
            raise BadMessage("单次输入过长（上限 20000 字）")
        out["text"] = v
    elif t == "navigate":
        u = msg.get("url")
        if not isinstance(u, str) or not u.strip():
            raise BadMessage("navigate.url 必填")
        out["url"] = u.strip()
    elif t == "switch_tab":
        tid = msg.get("tab_id")
        if not isinstance(tid, str) or not tid:
            raise BadMessage("switch_tab.tab_id 必填")
        out["tab_id"] = tid
    elif t == "quality":
        out["quality"] = max(10, min(int(_num(msg.get("quality"), 60)), 100))
    elif t == "resize":
        out["width"] = max(320, min(int(_num(msg.get("width"), 1280)), 3840))
        out["height"] = max(240, min(int(_num(msg.get("height"), 800)), 2160))
    return out


# ---- 帧节流 ----


@dataclass
class FrameThrottle:
    """按最小间隔丢弃过期帧的节流器（单调时钟，测试可注入 now）。

    为什么还要节流：ack 背压只保证"不快过浏览器生成"，但客户端解码慢时
    仍会攒下一串迟到的帧 —— 它们既过时又占带宽，直接丢掉更合理。
    """

    min_interval_ms: float = 33.0  # ≈30fps 上限
    _last_ms: float | None = None

    def should_send(self, now_ms: float) -> bool:
        if self._last_ms is None or (now_ms - self._last_ms) >= self.min_interval_ms:
            self._last_ms = now_ms
            return True
        return False

    def reset(self) -> None:
        self._last_ms = None
