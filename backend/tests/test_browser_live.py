"""浏览器实时画面：坐标换算 / 消息校验 / 按键映射 / 帧节流。

为什么要单独测这层：这几件事都是**纯逻辑**，但一旦错了，
表现是"点了没反应"或"点偏了"，在没有浏览器的情况下根本查不出来；
而真机联调一次只能看一个现象。这里把每条约定钉成断言。

不连浏览器、不起进程、不联网。
"""
from __future__ import annotations

import pytest

from app.services import browser_live as L


# ---------------------------------------------------------------- 坐标换算


class TestScaleNormalized:
    def test_center_maps_to_page_center(self):
        x, y = L.scale_normalized(0.5, 0.5, page_w=1000, page_h=600)
        assert (x, y) == (500.0, 300.0)

    def test_corners(self):
        assert L.scale_normalized(0, 0, page_w=1000, page_h=600) == (0.0, 0.0)
        # 右下角夹到 w-1/h-1：页面坐标左闭右开，等于宽度会落到视口外
        assert L.scale_normalized(1, 1, page_w=1000, page_h=600) == (999.0, 599.0)

    def test_out_of_range_is_clamped_not_dropped(self):
        """拖拽时指针常移出画面，必须夹到边界而不是丢弃（丢弃会让页面上的拖拽中断）。"""
        assert L.scale_normalized(-0.4, -2, page_w=800, page_h=600) == (0.0, 0.0)
        assert L.scale_normalized(3.5, 4.0, page_w=800, page_h=600) == (799.0, 599.0)

    def test_nan_and_inf_fall_back_to_zero(self):
        # NaN/inf 直接发给 CDP 会得到毫无指向性的协议错误，必须在这里挡掉
        assert L.scale_normalized(float("nan"), float("inf"), page_w=800, page_h=600) == (0.0, 0.0)

    def test_non_numeric_input(self):
        assert L.scale_normalized("abc", None, page_w=800, page_h=600) == (0.0, 0.0)

    def test_zero_page_size_does_not_divide_by_zero(self):
        x, y = L.scale_normalized(0.5, 0.5, page_w=0, page_h=0)
        # 退化为下限 1px 的"视口"，坐标被夹到 0
        assert x == 0.0 and y == 0.0

    def test_degenerate_page_size_returns_finite(self):
        x, y = L.scale_normalized(0.5, 0.5, page_w=1, page_h=1)
        assert x == 0.0 and y == 0.0


class TestViewportOf:
    def test_prefers_css_layout_viewport(self):
        """CSS 视口才是 Input 事件的坐标系；物理视口在高 DPI 下会整体偏移。"""
        m = {
            "layoutViewport": {"clientWidth": 754, "clientHeight": 453},
            "cssLayoutViewport": {"clientWidth": 377, "clientHeight": 226},
        }
        assert L.viewport_of(m) == (377.0, 226.0)

    def test_falls_back_to_layout_viewport(self):
        m = {"layoutViewport": {"clientWidth": 900, "clientHeight": 500}}
        assert L.viewport_of(m) == (900.0, 500.0)

    def test_missing_metrics_uses_safe_default(self):
        assert L.viewport_of({}) == (1280.0, 800.0)
        assert L.viewport_of(None) == (1280.0, 800.0)

    def test_zero_sized_viewport_is_rejected(self):
        # 导航瞬间可能给出 0，此时应退回默认值而不是把坐标算成 0
        m = {"cssLayoutViewport": {"clientWidth": 0, "clientHeight": 0}}
        assert L.viewport_of(m) == (1280.0, 800.0)


# ---------------------------------------------------------------- 消息校验


class TestValidateMessage:
    def test_rejects_unknown_type(self):
        """白名单：不透传任意 CDP 方法名，否则这个 WS 等于一个 Runtime.evaluate 后门。"""
        with pytest.raises(L.BadMessage):
            L.validate_message({"type": "Runtime.evaluate"})
        with pytest.raises(L.BadMessage):
            L.validate_message({"type": "Page.navigate"})
        with pytest.raises(L.BadMessage):
            L.validate_message("not-a-dict")
        with pytest.raises(L.BadMessage):
            L.validate_message({"type": None})

    def test_mouse_normalizes(self):
        out = L.validate_message({
            "type": "mouse", "event": "mousePressed", "button": "left",
            "nx": 0.25, "ny": 0.75, "clickCount": 2,
            "modifiers": ["Control", "Shift"],
        })
        assert out["event"] == "mousePressed"
        assert out["button"] == "left"
        assert out["nx"] == 0.25 and out["ny"] == 0.75
        assert out["click_count"] == 2
        assert out["modifiers"] == L.MOD_CTRL | L.MOD_SHIFT

    def test_mouse_click_count_is_bounded(self):
        # clickCount=0 会被 CDP 当成"非点击"，>3 没有语义
        out = L.validate_message({"type": "mouse", "event": "mousePressed", "clickCount": 0})
        assert out["click_count"] == 1
        out = L.validate_message({"type": "mouse", "event": "mousePressed", "clickCount": 99})
        assert out["click_count"] == 3

    def test_mouse_rejects_bad_event_and_button(self):
        with pytest.raises(L.BadMessage):
            L.validate_message({"type": "mouse", "event": "click"})
        with pytest.raises(L.BadMessage):
            L.validate_message({"type": "mouse", "event": "mousePressed", "button": "thumb"})

    def test_mouse_defaults_to_no_button(self):
        out = L.validate_message({"type": "mouse", "event": "mouseMoved"})
        assert out["button"] == "none"

    def test_wheel_keeps_deltas(self):
        out = L.validate_message({
            "type": "wheel", "nx": 0.1, "ny": 0.2, "deltaX": 3, "deltaY": -120,
        })
        assert out["delta_x"] == 3.0 and out["delta_y"] == -120.0

    def test_key_requires_key(self):
        with pytest.raises(L.BadMessage):
            L.validate_message({"type": "key"})
        with pytest.raises(L.BadMessage):
            L.validate_message({"type": "key", "key": ""})

    def test_key_defaults_to_press(self):
        out = L.validate_message({"type": "key", "key": "a", "text": "a"})
        assert out["mod"] == "press"
        assert out["text"] == "a"

    def test_key_modifier_mode_is_preserved(self):
        # 修饰键按下/抬起必须能区分，否则 Ctrl+A 会被识别成单独的 'a'
        for m in ("down", "up"):
            out = L.validate_message({"type": "key", "key": "Control", "mod": m})
            assert out["mod"] == m
        # 非法值退回 press，而不是抛错（前端版本不一致时更宽容）
        out = L.validate_message({"type": "key", "key": "a", "mod": "bogus"})
        assert out["mod"] == "press"

    def test_key_text_must_be_string(self):
        out = L.validate_message({"type": "key", "key": "Enter", "text": 123})
        assert out["text"] is None

    def test_text_length_is_capped(self):
        out = L.validate_message({"type": "text", "text": "hi"})
        assert out["text"] == "hi"
        with pytest.raises(L.BadMessage):
            L.validate_message({"type": "text", "text": "x" * 20001})
        with pytest.raises(L.BadMessage):
            L.validate_message({"type": "text", "text": None})

    def test_navigate_requires_url(self):
        out = L.validate_message({"type": "navigate", "url": "  https://a.com  "})
        # 前后空白要去掉，否则 CDP 会把空格当 URL 的一部分
        assert out["url"] == "https://a.com"
        with pytest.raises(L.BadMessage):
            L.validate_message({"type": "navigate", "url": "   "})

    def test_switch_tab_requires_id(self):
        out = L.validate_message({"type": "switch_tab", "tab_id": "ABC123"})
        assert out["tab_id"] == "ABC123"
        with pytest.raises(L.BadMessage):
            L.validate_message({"type": "switch_tab"})

    def test_quality_and_resize_are_bounded(self):
        assert L.validate_message({"type": "quality", "quality": 5})["quality"] == 10
        assert L.validate_message({"type": "quality", "quality": 500})["quality"] == 100
        r = L.validate_message({"type": "resize", "width": 10, "height": 99999})
        assert r["width"] == 320 and r["height"] == 2160

    def test_simple_actions_pass_through(self):
        for t in ("ping", "attach", "reload", "tabs"):
            assert L.validate_message({"type": t}) == {"type": t}


# ---------------------------------------------------------------- 按键映射


class TestMapKey:
    def test_letters_case_insensitive_code(self):
        low = L.map_key("a")
        up = L.map_key("A")
        assert low is not None and up is not None
        assert low.code == "KeyA" and up.code == "KeyA"
        # key 保留大小写：页面靠它区分 Shift+A
        assert low.key == "a" and up.key == "A"
        assert low.key_code == up.key_code == 65

    def test_digits(self):
        assert L.map_key("7") == L.KeyInfo("7", "Digit7", 55)

    def test_control_keys(self):
        assert L.map_key("Enter").key_code == 13
        assert L.map_key("Tab").key_code == 9
        assert L.map_key("Escape").key_code == 27
        assert L.map_key("ArrowDown").key_code == 40

    def test_space_aliases(self):
        # KeyboardEvent.key 对空格是 " "，部分前端会发 "Space"
        assert L.map_key(" ").code == "Space"
        assert L.map_key("Space").code == "Space"

    def test_punctuation(self):
        assert L.map_key("/").key_code == 191
        assert L.map_key("?").key_code == 191  # 与 / 同键位，靠 Shift 区分

    def test_unknown_returns_none(self):
        """未知键必须返回 None 让调用方走 insertText：
        瞎猜 keyCode 可能触发完全不相干的快捷键。"""
        assert L.map_key("F13") is None
        assert L.map_key("MediaPlayPause") is None
        assert L.map_key("") is None


class TestModifiers:
    def test_bitmask(self):
        assert L.modifiers_from(["Control", "Shift"]) == L.MOD_CTRL | L.MOD_SHIFT
        assert L.modifiers_from(["Alt"]) == L.MOD_ALT
        assert L.modifiers_from(["Meta"]) == L.MOD_META

    def test_empty_and_unknown(self):
        assert L.modifiers_from(None) == 0
        assert L.modifiers_from([]) == 0
        # 未知名称不能自造位
        assert L.modifiers_from(["Hyper"]) == 0

    def test_command_for_modifier_only(self):
        assert L.command_for("Control", "down") == "rawKeyDown"
        assert L.command_for("Shift", "up") == "rawKeyUp"
        # 普通键不归它管（调用方走三元组）
        assert L.command_for("a", "press") is None
        assert L.command_for("Enter", "down") is None


# ---------------------------------------------------------------- 帧节流


class TestFrameThrottle:
    def test_first_frame_always_sent(self):
        t = L.FrameThrottle(min_interval_ms=33)
        assert t.should_send(1000.0) is True

    def test_too_soon_is_dropped(self):
        t = L.FrameThrottle(min_interval_ms=33)
        assert t.should_send(1000.0) is True
        assert t.should_send(1010.0) is False
        assert t.should_send(1020.0) is False
        # 超过间隔才放行
        assert t.should_send(1033.0) is True

    def test_dropped_frames_do_not_reset_clock(self):
        """被丢掉的帧不能推进时间基准，否则持续丢帧会把实际帧率压到很低。"""
        t = L.FrameThrottle(min_interval_ms=33)
        assert t.should_send(0.0) is True
        assert t.should_send(10.0) is False
        assert t.should_send(20.0) is False
        assert t.should_send(40.0) is True   # 距上次**发送** 40ms

    def test_reset_allows_immediate_send(self):
        t = L.FrameThrottle(min_interval_ms=1000)
        t.should_send(0.0)
        assert t.should_send(1.0) is False
        t.reset()
        # 切标签后要立刻看到新页面的首帧，不能等满一个间隔
        assert t.should_send(2.0) is True

    def test_zero_interval_sends_everything(self):
        t = L.FrameThrottle(min_interval_ms=0)
        assert t.should_send(0.0) is True
        assert t.should_send(0.0) is True
