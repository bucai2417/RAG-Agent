"""浏览器：懒加载列表多轮滚动、滚动容器兜底、元素定位重试。

这三件事共同的失败形态是「AI 以为自己看全了，其实只看到第一屏」：

- 只滚一次 → 首屏 12 条，AI 得出「只有 12 条订单」的**错误**结论
- 滚 window 而列表在 `overflow:auto` 的 div 里 → 滚了但什么也没加载
- 到底判据只看 window → 容器页面第一轮就「已到底」，多轮退化成零轮
- 快照到执行之间页面还在渲染 → 第一次找不到就报错，AI 去猜选择器

不连浏览器、不起进程、不联网（用假会话驱动滚动循环）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import cdp_client as C


@pytest.fixture(autouse=True)
def _no_real_wait(monkeypatch):
    """滚动循环里的等待是为真实异步加载留的，测试里没必要真等。"""
    real_sleep = asyncio.sleep

    async def fast(delay, *a, **kw):
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast)


class _ScrollSession(C.CDPSession):
    """按预设序列回放指标的假会话：只实现滚动循环用到的那几个方法。"""

    def __init__(self, metrics: list[dict], targets: list[dict] | None = None) -> None:
        super().__init__("ws://x", timeout=0.05)
        self._metrics = metrics
        self._targets = targets or [{"target": "window", "x": 100, "y": 100}]
        self._mi = 0
        self._ti = 0
        self.wheels: list[dict] = []

    async def load_metrics(self) -> dict:
        m = self._metrics[min(self._mi, len(self._metrics) - 1)]
        self._mi += 1
        return m

    async def evaluate(self, expression: str, *, await_promise: bool = False):
        # 每轮开始都会先探一次滚动容器
        t = self._targets[min(self._ti, len(self._targets) - 1)]
        self._ti += 1
        return t

    async def wheel(self, x, y, *, delta_y, delta_x=0, modifiers=0):
        self.wheels.append({"x": x, "y": y, "dy": delta_y})


def _m(sig: str, h: int, n: int, at_bottom: bool = False, vh: int = 800) -> dict:
    return {"sig": sig, "h": h, "n": n, "atBottom": at_bottom, "vh": vh}


class TestScrollLoad:
    def test_keeps_scrolling_while_content_grows(self):
        """每轮都有新内容时不提前停：列表可能还有十几屏。"""
        sess = _ScrollSession([
            _m("100:1", 100, 1),            # 初始
            _m("200:2", 200, 2),            # 第 1 轮滚出内容
            _m("300:3", 300, 3),            # 第 2 轮
            _m("400:4", 400, 4),            # 第 3 轮
        ])
        r = asyncio.run(sess.scroll_load(rounds=5))
        # 序列耗尽后指标停在 400，末两轮无新内容 → 判定列表已加载完
        assert r["rounds_done"] == 5
        assert r["reason"] == "no_new_content"
        assert r["height_before"] == 100 and r["height_after"] == 400
        assert len(sess.wheels) == 5
        assert [h["grew"] for h in r["history"]] == [True, True, True, False, False]

    def test_stops_when_at_bottom_and_nothing_new(self):
        """到底且没新内容：再滚也不会有东西，立刻收工别空转。"""
        sess = _ScrollSession([
            _m("100:1", 100, 1),
            _m("100:1", 100, 1, at_bottom=True),
        ])
        r = asyncio.run(sess.scroll_load(rounds=10))
        assert r["rounds_done"] == 1
        assert r["reason"] == "bottom"
        assert r["at_bottom"] is True

    def test_tolerates_one_stalled_round(self):
        """懒加载常有一轮延迟：第一轮没变化就停会把"还没加载"当成"没有了"。"""
        sess = _ScrollSession([
            _m("100:1", 100, 1),
            _m("100:1", 100, 1),           # 抖动（异步还没回来）
            _m("900:9", 900, 9),           # 其实加载了
            _m("900:9", 900, 9),
            _m("900:9", 900, 9),
        ])
        r = asyncio.run(sess.scroll_load(rounds=10))
        assert r["height_after"] == 900
        # 连续两轮无新内容才停
        assert r["reason"] == "no_new_content"

    def test_stops_after_two_idle_rounds(self):
        """时间戳/轮播动画会让指标抖动，但连续两轮不变就足以判断"没有了"。"""
        sess = _ScrollSession([
            _m("100:1", 100, 1),
            _m("100:1", 100, 1),
            _m("100:1", 100, 1),
            _m("100:1", 100, 1),
        ])
        r = asyncio.run(sess.scroll_load(rounds=20))
        assert r["rounds_done"] == 2
        assert r["reason"] == "no_new_content"

    def test_rounds_are_clamped(self):
        """无限滚动页面上"一直有变化"是常态，没有硬上限会挂死会话。"""
        from app.services.browser_ops import MAX_SCROLL_ROUNDS

        metrics = [_m(f"{i}:{i}", i, i) for i in range(1, 100)]
        sess = _ScrollSession(metrics)
        r = asyncio.run(sess.scroll_load(rounds=10**6))
        assert r["rounds_done"] == MAX_SCROLL_ROUNDS

    def test_uses_scroll_container_when_present(self):
        """列表放在 overflow:auto 的 div 里时，对 window 发滚轮完全没反应。"""
        sess = _ScrollSession(
            [_m("100:1", 100, 1), _m("100:1", 100, 1, at_bottom=True)],
            targets=[{"target": "element", "x": 321, "y": 222}],
        )
        asyncio.run(sess.scroll_load(rounds=2))
        assert sess.wheels[0]["x"] == 321 and sess.wheels[0]["y"] == 222
        assert sess.wheels[0]["dy"] > 0

    def test_default_delta_is_most_of_a_screen(self):
        """留一点重叠：正好一屏会把跨屏边界那条漏掉。"""
        sess = _ScrollSession([_m("1:1", 1, 1), _m("1:1", 1, 1, at_bottom=True)],
                              targets=[{"target": "window", "x": 0, "y": 0}])
        asyncio.run(sess.scroll_load(rounds=1))
        assert 500 < sess.wheels[0]["dy"] < 800

    def test_explicit_dy_wins(self):
        sess = _ScrollSession([_m("1:1", 1, 1), _m("1:1", 1, 1, at_bottom=True)],
                              targets=[{"target": "window", "x": 0, "y": 0}])
        asyncio.run(sess.scroll_load(rounds=1, dy=-300))
        assert sess.wheels[0]["dy"] == -300

    def test_history_records_every_round(self):
        """回给 AI 的是"滚了几轮、为什么停"，不能只有一句"已滚动"。"""
        sess = _ScrollSession([
            _m("100:1", 100, 1), _m("200:2", 200, 2), _m("200:2", 200, 2, at_bottom=True),
        ])
        r = asyncio.run(sess.scroll_load(rounds=5))
        assert [h["round"] for h in r["history"]] == [1, 2]
        assert r["history"][0]["grew"] is True
        assert r["history"][1]["grew"] is False

    def test_bad_evaluate_result_does_not_crash(self):
        """页面正在导航时容器探测会返回 null，不能因此中断整个滚动。"""
        sess = _ScrollSession([_m("1:1", 1, 1), _m("1:1", 1, 1, at_bottom=True)])

        async def bad_eval(expr, **_):
            return None

        sess.evaluate = bad_eval  # type: ignore[assignment]
        r = asyncio.run(sess.scroll_load(rounds=1))
        assert r["rounds_done"] == 1
        assert sess.wheels  # 仍按缺省落点滚了
