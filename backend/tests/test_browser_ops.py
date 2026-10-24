"""browser_ops：快照编号 / ref 匹配 / 步骤校验 / 等待脚本 / 坐标换算。

为什么单独测这一层：这里的每一条约定都对应一个"真机上极难复现"的故障：

- 编号漂移 → 点了没反应或点错按钮（真机上一次只能看一个现象）
- 步骤校验缺失 → 第 7 步报错时前 6 步的副作用已经落下，回滚不了
- 等待判据用「存在」而非「可见」→ 点到透明层上，等于没点
- 坐标不夹紧 → NaN 直接发 CDP，报错毫无指向性

不连浏览器、不起进程、不联网。
"""
from __future__ import annotations

import json

import pytest

from app.services import browser_ops as O


# ---------------------------------------------------------------- 快照脚本


class TestSnapshotScript:
    def _js(self, **kw) -> str:
        return O.snapshot_script(**kw)

    def test_no_unreplaced_placeholder(self):
        """占位符必须全部替换：漏一个会让浏览器侧直接语法错误。"""
        js = self._js()
        assert "%SELECTOR%" not in js
        assert "%ROLE_BY_INPUT%" not in js
        assert "%ROLE_BY_TAG%" not in js
        assert "%NAME_MAX%" not in js
        assert "%MAX%" not in js
        assert "%TOKEN%" not in js

    def test_token_is_embedded_as_json_string(self):
        js = self._js(token="abc123")
        assert await_token(js) == "abc123"

    def test_selectors_are_valid_json_literals(self):
        """选择器/角色表是用 json.dumps 注入的，必须能原样解析回来。"""
        js = self._js(token="t")
        line = next(l for l in js.splitlines() if l.strip().startswith("const SEL ="))
        literal = line.split("=", 1)[1].strip().rstrip(";")
        assert isinstance(json.loads(literal), str)
        assert "button" in json.loads(literal)

    def test_max_is_clamped(self):
        # 上限存在的原因：几千个编号会把上下文撑爆，AI 反而找不到重点
        assert "const MAX = 150;" in self._js(max_elements=99999)
        # 下限：太小等于没列，没有意义
        assert "const MAX = 10;" in self._js(max_elements=1)

    def test_marks_elements_with_token_and_index(self):
        """标记回指是整套动作的基石：没有它就只能靠名称猜，列表页必然点错。"""
        js = self._js()
        assert "data-rea-snap" in js
        assert "data-rea-idx" in js

    def test_clears_old_marks_before_snapshot(self):
        """旧标记残留会让按 token 查询命中过期元素，必须先清。"""
        js = self._js()
        assert "removeAttribute('data-rea-snap')" in js


def await_token(js: str) -> str:
    line = next(l for l in js.splitlines() if l.strip().startswith("const TOKEN ="))
    return json.loads(line.split("=", 1)[1].strip().rstrip(";"))


class TestMarkedSelector:
    def test_selector_shape(self):
        sel = O.marked_selector("abc", 3)
        assert 'data-rea-snap="abc"' in sel
        assert 'data-rea-idx="3"' in sel

    def test_index_is_coerced_to_int(self):
        # 传进来可能是字符串或浮点（JSON 数字），统一成整数下标
        assert 'data-rea-idx="7"' in O.marked_selector("t", "7")
        assert 'data-rea-idx="7"' in O.marked_selector("t", 7.0)


# ---------------------------------------------------------------- 元素解析


def _el(idx: int, role: str = "button", name: str = "确定",
        x: float = 0, y: float = 0, w: float = 80, h: float = 30) -> O.Element:
    return O.Element(idx=idx, role=role, name=name, x=x, y=y, w=w, h=h)


class TestElementFromRaw:
    def test_parses_rect(self):
        e = O.Element.from_raw({
            "idx": 2, "role": "button", "name": "提交", "tag": "button",
            "rect": {"x": 10, "y": 20, "w": 100, "h": 40},
        })
        assert (e.idx, e.role, e.name) == (2, "button", "提交")
        assert e.center == (60.0, 40.0)

    def test_bad_numbers_do_not_raise(self):
        """NaN/None 坐标必须被兜成 0：上层拿 None 做算术会 TypeError。"""
        e = O.Element.from_raw({"idx": 0, "rect": {"x": None, "y": "abc", "w": float("nan")}})
        assert e.x == 0.0 and e.y == 0.0 and e.w == 0.0

    def test_checked_tristate(self):
        assert O.Element.from_raw({"idx": 0, "checked": True}).checked is True
        assert O.Element.from_raw({"idx": 0, "checked": False}).checked is False
        # null / 缺失 / 非布尔都表示"不适用"（普通按钮）
        assert O.Element.from_raw({"idx": 0, "checked": None}).checked is None
        assert O.Element.from_raw({"idx": 0}).checked is None


class TestPageSnapshot:
    def test_from_raw_non_dict(self):
        snap = O.PageSnapshot.from_raw(None)
        assert snap.elements == [] and snap.url == ""

    def test_by_ref_bounds(self):
        snap = O.PageSnapshot(elements=[_el(0), _el(1)])
        assert snap.by_ref(1) is not None
        assert snap.by_ref(2) is None
        assert snap.by_ref(-1) is None
        # 非数字不抛错，返回 None 让调用方给可读提示
        assert snap.by_ref("x") is None
        assert snap.by_ref(None) is None

    def test_find_by_name_substring(self):
        snap = O.PageSnapshot(elements=[
            _el(0, name="取消"), _el(1, name="确认提交"),
        ])
        assert snap.find("提交").idx == 1
        # 大小写与空白无关
        assert snap.find("  取消 ").idx == 0
        assert snap.find("不存在") is None
        assert snap.find("") is None

    def test_find_with_role_filter(self):
        snap = O.PageSnapshot(elements=[
            _el(0, role="link", name="设置"), _el(1, role="button", name="设置"),
        ])
        assert snap.find("设置").idx == 0
        assert snap.find("设置", role="button").idx == 1
        assert snap.find("设置", role="checkbox") is None


# ---------------------------------------------------------------- 重新匹配

class TestMatchCandidates:
    def test_role_name_and_position_wins(self):
        want = _el(0, name="搜索", x=10, y=20)
        cands = [
            _el(5, name="取消", x=10, y=20),
            _el(6, name="搜索", x=12, y=21),
        ]
        assert O.match_candidates(want, cands).idx == 6

    def test_same_name_uses_position_to_disambiguate(self):
        """列表页里"删除"重复 20 次，只靠名称会点到别的行。"""
        want = _el(0, name="删除", x=10, y=300)
        cands = [
            _el(1, name="删除", x=10, y=100),
            _el(2, name="删除", x=10, y=300),
            _el(3, name="删除", x=10, y=500),
        ]
        assert O.match_candidates(want, cands).idx == 2

    def test_position_within_tolerance(self):
        want = _el(0, name="", x=100, y=100, w=10, h=10)
        # 中心距不到 24px，视为同一元素（页面滚动了几像素不该判为不同）
        cand = _el(1, name="", x=110, y=105, w=10, h=10)
        assert O.match_candidates(want, [cand]).idx == 1

    def test_position_out_of_tolerance(self):
        want = _el(0, name="", x=100, y=100, w=10, h=10)
        cand = _el(1, name="", x=300, y=300, w=10, h=10)
        assert O.match_candidates(want, [cand]) is None

    def test_falls_back_to_name_only(self):
        """文案没变但元素被换掉（a→button）时仍应认出它。"""
        want = _el(0, role="link", name="下一步", x=0, y=0)
        cand = _el(1, role="button", name="下一步", x=900, y=900)
        assert O.match_candidates(want, [cand]).idx == 1

    def test_no_candidates(self):
        assert O.match_candidates(_el(0, name="x"), []) is None

    def test_empty_name_does_not_match_everything(self):
        """空名称不能退化成"匹配第一个"：那会点到完全不相干的元素。"""
        want = _el(0, name="", x=0, y=0, w=10, h=10)
        cand = _el(1, name="", x=500, y=500, w=10, h=10)
        assert O.match_candidates(want, [cand]) is None


# ---------------------------------------------------------------- 步骤校验


class TestParseStep:
    def test_basic_click(self):
        s = O.parse_step({"action": "click", "ref": 3})
        assert s.action == "click" and s.ref == 3

    def test_click_by_text_without_ref(self):
        s = O.parse_step({"action": "click", "text": "提交"})
        assert s.ref is None and s.text == "提交"

    def test_click_without_target_rejected(self):
        with pytest.raises(ValueError, match="需要 ref 或 text"):
            O.parse_step({"action": "click"})

    def test_fill_requires_value(self):
        with pytest.raises(ValueError, match="缺少 value"):
            O.parse_step({"action": "fill", "ref": 1})

    def test_unknown_action_lists_alternatives(self):
        with pytest.raises(ValueError) as ei:
            O.parse_step({"action": "drag"})
        assert "不支持" in str(ei.value)
        # 报错要能自解释，AI 才知道该换哪个动作
        assert "click" in str(ei.value)

    def test_navigate_rejects_non_http(self):
        """与实时面板同一口径：不允许 file:// 或 chrome:// 进来。"""
        with pytest.raises(ValueError, match="http"):
            O.parse_step({"action": "navigate", "url": "file:///etc/passwd"})
        with pytest.raises(ValueError, match="http"):
            O.parse_step({"action": "navigate", "url": "javascript:alert(1)"})
        assert O.parse_step({"action": "navigate", "url": "https://a.test"}).url

    def test_negative_ref_rejected(self):
        with pytest.raises(ValueError, match="非负整数"):
            O.parse_step({"action": "click", "ref": -1})

    def test_negative_width_int_ref_coerced(self):
        # "2" 这类字符串编号是模型常见输出，应被接受
        assert O.parse_step({"action": "click", "ref": "2"}).ref == 2

    def test_timeout_is_clamped(self):
        assert O.parse_step({"action": "wait_for", "text": "x", "timeout_ms": 10**9}).timeout_ms == 120000
        assert O.parse_step({"action": "wait_for", "text": "x", "timeout_ms": -5}).timeout_ms == 0

    def test_wait_ms_clamped(self):
        assert O.parse_step({"action": "wait", "ms": -100}).wait_ms == 0
        assert O.parse_step({"action": "wait", "ms": 10**9}).wait_ms == 60000

    def test_wait_for_requires_text(self):
        with pytest.raises(ValueError, match="需要 ref 或 text"):
            O.parse_step({"action": "wait_for"})

    def test_error_message_carries_step_number(self):
        """报错必须带序号：批量执行时才知道是哪一步崩的。"""
        with pytest.raises(ValueError, match="第 2 步"):
            O.parse_step({"action": "bogus"}, index=1)


class TestParseSteps:
    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="非空数组"):
            O.parse_steps([])
        with pytest.raises(ValueError, match="非空数组"):
            O.parse_steps(None)

    def test_rejects_non_dict_item(self):
        with pytest.raises(ValueError, match="必须是对象"):
            O.parse_steps(["click"])

    def test_validates_all_before_returning(self):
        """整体校验：绝不在校验失败时返回部分步骤（否则调用方可能已执行前半段）。"""
        with pytest.raises(ValueError):
            O.parse_steps([
                {"action": "click", "ref": 1},
                {"action": "fill", "ref": 2},   # 缺 value
            ])

    def test_order_is_preserved(self):
        steps = O.parse_steps([
            {"action": "fill", "ref": 1, "value": "a"},
            {"action": "click", "ref": 2},
        ])
        assert [s.action for s in steps] == ["fill", "click"]


class TestStepLabel:
    def test_labels_are_readable(self):
        assert O.parse_step({"action": "click", "ref": 3}).label() == "click #3"
        assert "fill" in O.parse_step({"action": "fill", "ref": 1, "value": "张三"}).label()
        assert O.parse_step({"action": "press", "key": "Enter"}).label() == "press Enter"
        assert O.parse_step({"action": "navigate", "url": "https://a.test"}).label().endswith("https://a.test")


# ---------------------------------------------------------------- 等待脚本


class TestWaitScript:
    def test_text_script_is_boolean_expression(self):
        js = O.wait_script(text="加载完成")
        assert js.startswith("(() =>")
        assert "return false" in js
        # 文本要作为 JSON 字面量注入，避免中文/引号破坏 JS 语法
        assert json.dumps("加载完成") in js

    def test_text_wait_requires_visibility_not_just_presence(self):
        """用「存在」而非「可见」会点到透明层上：必须检查 display/visibility/尺寸。"""
        js = O.wait_script(text="x")
        assert "visibility" in js
        assert "display" in js
        assert "width < 1" in js

    def test_selector_script_uses_json_literal(self):
        js = O.wait_script(selector='#app [data-x="1"]')
        assert json.dumps('#app [data-x="1"]') in js

    def test_script_is_wrapped_in_try(self):
        """页面正在导航时 evaluate 会抛错，脚本自身要吞掉异常返回 false。"""
        assert "catch" in O.wait_script(text="x")


# ---------------------------------------------------------------- 坐标换算


class TestCenterNormalized:
    def test_center_of_rect(self):
        nx, ny = O.center_normalized({"x": 400, "y": 300, "w": 200, "h": 100}, 1000, 600)
        assert (nx, ny) == (0.5, pytest.approx(350 / 600))

    def test_clamped_when_outside(self):
        """元素一半在视口外时夹紧：夹到边上至少能看出"往那个方向动"。"""
        nx, ny = O.center_normalized({"x": -500, "y": -500, "w": 10, "h": 10}, 1000, 600)
        assert (nx, ny) == (0.0, 0.0)
        nx, ny = O.center_normalized({"x": 5000, "y": 5000, "w": 10, "h": 10}, 1000, 600)
        assert (nx, ny) == (1.0, 1.0)

    def test_zero_page_does_not_divide_by_zero(self):
        nx, ny = O.center_normalized({"x": 0, "y": 0, "w": 0, "h": 0}, 0, 0)
        assert nx == 0.0 and ny == 0.0


# ---------------------------------------------------------------- 格式化


class TestFormatSnapshot:
    def _snap(self) -> O.PageSnapshot:
        return O.PageSnapshot.from_raw({
            "title": "订单列表",
            "url": "https://x.test/orders",
            "viewport": {"w": 1280, "h": 800},
            "scroll": {"y": 0, "max_y": 1200},
            "total_elements": 3,
            "headings": [{"level": 1, "text": "订单"}],
            "elements": [
                {"idx": 0, "role": "button", "name": "搜索", "rect": {"x": 1, "y": 1, "w": 5, "h": 5}},
                {"idx": 1, "role": "textbox", "name": "关键词", "type": "text",
                 "value": "", "placeholder": "请输入", "rect": {"x": 1, "y": 1, "w": 5, "h": 5}},
                {"idx": 2, "role": "checkbox", "name": "已发货", "type": "checkbox",
                 "checked": True, "disabled": True, "rect": {"x": 1, "y": 1, "w": 5, "h": 5}},
            ],
            "text": "共 12 条订单",
        })

    def test_lists_elements_with_indices(self):
        out = O.format_snapshot(self._snap())
        assert "[0] button" in out
        assert '"搜索"' in out
        assert "[1] textbox" in out

    def test_marks_disabled_and_checked(self):
        out = O.format_snapshot(self._snap())
        assert "disabled" in out
        assert "已勾选" in out

    def test_unnamed_element_is_flagged(self):
        """无名称元素对 AI 毫无用处（点它等于盲点），必须显式标出。"""
        snap = O.PageSnapshot.from_raw({
            "elements": [{"idx": 0, "role": "button", "name": ""}],
        })
        assert "(无名称)" in O.format_snapshot(snap)

    def test_text_is_truncated_with_count(self):
        snap = self._snap()
        snap.text = "字" * 100
        out = O.format_snapshot(snap, max_text=10)
        assert "已截断" in out
        assert "100" in out

    def test_truncated_elements_are_announced(self):
        snap = self._snap()
        snap.truncated = True
        out = O.format_snapshot(snap)
        assert "已截断" in out

    def test_empty_snapshot_does_not_crash(self):
        out = O.format_snapshot(O.PageSnapshot())
        assert isinstance(out, str)


# ---------------------------------------------------------------- iframe


class TestFramePrelude:
    def test_snapshot_has_no_unreplaced_frame_placeholder(self):
        """%FRAME% 漏替换会让浏览器侧直接语法错误 —— 整个快照失效。"""
        js = O.snapshot_script()
        assert "%FRAME%" not in js
        assert "__reaFrames" in js
        # 遍历与定位函数必须都在，否则下面所有元素级动作都会退化成"只看顶层文档"
        assert "__reaFind" in js
        assert "__reaRect" in js

    def test_frame_script_wraps_body_with_helpers(self):
        js = O.frame_script("return 1;")
        assert js.startswith("(() =>")
        assert js.rstrip().endswith("})()")
        assert "__reaFind" in js and "return 1;" in js

    def test_tracks_iframe_chain_for_inner_scrolling(self):
        """嵌套 iframe 必须由外向内逐层滚：只滚里层时外层框可能还在视口外。"""
        js = O.frame_script("return 1;")
        assert "chain" in js
        assert "scrollIntoView" not in js  # 前奏只负责定位，不替调用方决定怎么滚

    def test_offsets_include_iframe_border(self):
        """坐标要加 clientLeft/clientTop（边框宽度），否则点在内边距外。"""
        js = O.frame_script("return 1;")
        assert "clientLeft" in js and "clientTop" in js

    def test_cross_origin_frames_are_counted_not_ignored(self):
        """跨域 iframe 读不到是浏览器边界，必须记账 —— 静默跳过会骗 AI「页面上没有」。"""
        js = O.snapshot_script()
        assert "crossCount" in js
        assert "cross_origin" in js


class TestElementFrame:
    def test_frame_field_parsed(self):
        e = O.Element.from_raw({"idx": 0, "name": "确认支付", "frame": "0"})
        assert e.frame == "0"

    def test_top_level_element_has_empty_frame(self):
        assert O.Element.from_raw({"idx": 0, "name": "外层"}).frame == ""

    def test_snapshot_iframe_counts(self):
        snap = O.PageSnapshot.from_raw({
            "iframes": {"count": 1, "cross_origin": 2,
                        "cross_srcs": ["https://pay.test/x", "https://cap.test/y"]},
        })
        assert snap.iframe_count == 1
        assert snap.cross_iframe_count == 2
        assert len(snap.cross_iframe_srcs) == 2

    def test_missing_iframes_key_defaults_to_zero(self):
        snap = O.PageSnapshot.from_raw({"elements": []})
        assert snap.iframe_count == 0 and snap.cross_iframe_count == 0


class TestFormatSnapshotWithFrames:
    def test_element_in_iframe_is_marked(self):
        snap = O.PageSnapshot.from_raw({"elements": [
            {"idx": 0, "role": "button", "name": "确认支付", "frame": "0"},
        ]})
        out = O.format_snapshot(snap)
        assert "in iframe" in out

    def test_cross_origin_frames_are_announced_with_src(self):
        """必须让 AI 知道"这部分读不到"，否则它会在读不到的区域里无限找下去。"""
        snap = O.PageSnapshot.from_raw({"iframes": {
            "count": 0, "cross_origin": 1,
            "cross_srcs": ["https://pay.taobao.com/cashier"],
        }})
        out = O.format_snapshot(snap)
        assert "跨域" in out
        assert "https://pay.taobao.com/cashier" in out

    def test_no_frame_noise_on_plain_pages(self):
        """普通页面上不该出现 iframe 提示：那是无意义上下文。"""
        snap = O.PageSnapshot.from_raw({"title": "x", "elements": []})
        out = O.format_snapshot(snap)
        assert "iframe" not in out

    def test_frame_texts_are_included(self):
        """支付金额/按钮文案常常只在 iframe 正文里，丢掉等于让 AI 看不见。"""
        snap = O.PageSnapshot.from_raw({
            "text": "外层",
            "frame_texts": [{"frame": "0", "text": "金额 ¥128.00"}],
        })
        out = O.format_snapshot(snap)
        assert "¥128.00" in out
        assert "内嵌框架正文" in out


# ---------------------------------------------------------------- 懒加载


class TestScrollRounds:
    def test_rounds_parsed_and_clamped(self):
        s = O.parse_step({"action": "scroll", "rounds": 5})
        assert s.rounds == 5
        # 无限滚动页面上"一直有变化"是常态，没有硬上限会挂死会话
        assert O.parse_step({"action": "scroll", "rounds": 10**6}).rounds == O.MAX_SCROLL_ROUNDS

    def test_rounds_rejected_on_non_scroll(self):
        """静默忽略会让 AI 以为"我让它滚了 5 轮"，而实际一轮都没滚。"""
        with pytest.raises(ValueError, match="不支持 rounds"):
            O.parse_step({"action": "click", "ref": 1, "rounds": 3})

    def test_interval_ms_clamped(self):
        s = O.parse_step({"action": "scroll", "rounds": 2, "interval_ms": 10**9})
        assert s.interval_ms == 10000

    def test_label_shows_rounds(self):
        assert "x5" in O.parse_step({"action": "scroll", "rounds": 5}).label()
        # 单轮不带 x1，避免噪音
        assert "x1" not in O.parse_step({"action": "scroll"}).label()

    def test_wait_for_accepts_ref(self):
        """按元素等比按文案等更准：「提交成功」在页面别处也常有。"""
        s = O.parse_step({"action": "wait_for", "ref": 3})
        assert s.ref == 3 and not s.text

    def test_wait_for_still_requires_one_of_them(self):
        with pytest.raises(ValueError):
            O.parse_step({"action": "wait_for"})


class TestLoadProbeScript:
    def test_metrics_include_signature(self):
        js = O.load_probe_script()
        assert "sig" in js and "atBottom" in js

    def test_at_bottom_considers_scroll_containers(self):
        """列表放在 overflow:auto 的 div 里时文档高度不变，只看 window 会误判成已到底。"""
        js = O.load_probe_script()
        assert "scrollHeight" in js and "clientHeight" in js
        assert "boxLeft" in js

    def test_signature_includes_container_height(self):
        """替换式加载时文档高度不变，只能靠容器高度发现"其实又有新内容"。"""
        assert "boxH" in O.load_probe_script()

    def test_scroll_target_falls_back_to_window(self):
        js = O.SCROLL_TARGET_SCRIPT
        assert "target: 'window'" in js
        assert "'element'" in js


def test_wait_script_searches_all_frames():
    """iframe 里已经出现的"支付成功"不能被判成不存在。"""
    js = O.wait_script(text="支付成功")
    assert "__reaFrames.docs" in js
    # 中文按 JSON 字面量注入（保证转义一致），用同样的方式比对
    assert json.dumps("支付成功") in js


def test_wait_script_selector_uses_frame_lookup():
    """只查顶层文档会让 wait_for 一直超时 —— 而元素其实早就出现了。"""
    js = O.wait_script(selector="#pay")
    assert "__reaFind" in js
    assert "ownerDocument.defaultView" in js
