"""工具契约测试：注册表里的每个内置工具都必须真的能被 react 循环调用。

为什么需要这一层：`react.run_react_loop` 只调 `tool.run(args, ctx)`。
写工具若只实现了 `execute()` 而忘了继承 `WriteToolMixin`，注册和列表接口
都一切正常（它们只读 name/description/parameters），直到用户在聊天里
真的触发那次写操作才炸成「工具执行错误」—— 排查成本极高。
这里把该约定固化成断言。
"""
from __future__ import annotations

import inspect

from app.agents.tools.base import WriteToolMixin
from app.agents.tools.registry import registry


def test_every_builtin_tool_has_callable_run():
    missing = [
        (t.name, type(t).__name__)
        for t in registry.all_builtin()
        if not callable(getattr(t, "run", None))
    ]
    assert not missing, f"以下工具没实现 run()，react 循环调用时会 AttributeError：{missing}"


def test_every_admin_tool_has_callable_run():
    missing = [
        (t.name, type(t).__name__)
        for t in registry.all_admin()
        if not callable(getattr(t, "run", None))
    ]
    assert not missing, f"以下管理工具没实现 run()：{missing}"


def test_write_tools_are_mixin_based():
    """kind=write 且非 auto_approve 的工具，必须是 WriteToolMixin 子类。

    否则缺 run()；退一步说，即便自带 run()，HITL 卡片也拿不到 summarize()。
    """
    bad = [
        (t.name, type(t).__name__)
        for t in registry.all_builtin() + registry.all_admin()
        if getattr(t, "kind", "read") == "write"
        and not getattr(t, "auto_approve", False)
        and not isinstance(t, WriteToolMixin)
    ]
    assert not bad, f"以下写工具未继承 WriteToolMixin（无 run/summarize）：{bad}"


def test_tool_signatures_are_async():
    """run/execute 必须是协程函数，否则 react 循环里的 await 会拿到非可等待对象。"""
    bad = []
    for t in registry.all_builtin() + registry.all_admin():
        run = getattr(t, "run", None)
        if not callable(run):
            continue
        if not inspect.iscoroutinefunction(run):
            # WriteToolMixin.run 是 async；子类直接覆盖成同步才会命中
            bad.append((t.name, "run"))
    assert not bad, f"以下工具的 run() 不是协程函数：{bad}"


def test_browser_action_is_confirmable_write_tool():
    """回归：browser_action 曾因漏继承 WriteToolMixin 而无 run()。"""
    from app.agents.tools.browser_tools import BrowserActionTool

    t = BrowserActionTool()
    assert isinstance(t, WriteToolMixin)
    assert t.kind == "write"
    assert callable(t.run)
    assert inspect.iscoroutinefunction(t.run)
    # 网页点击/填表有外部副作用，必须经人工确认
    assert t.needs_confirm({"action": "click", "selector": "#submit"}) is True
    assert t.needs_confirm({"action": "click", "ref": 3}) is True


def test_browser_readonly_actions_skip_confirmation():
    """只读步骤免确认，否则"看一眼正文"也要点审批，交互成本过高。"""
    from app.agents.tools.browser_tools import BrowserActionTool

    t = BrowserActionTool()
    for readonly in ("read", "screenshot", "wait", "wait_for", "assert_text", "scroll"):
        assert t.needs_confirm({"action": readonly}) is False, readonly


def test_browser_action_batch_confirm_follows_write_steps():
    """批量步骤里只要有一个写动作就要确认；全是只读则不确认。"""
    from app.agents.tools.browser_tools import BrowserActionTool

    t = BrowserActionTool()
    assert t.needs_confirm({"steps": [
        {"action": "read"}, {"action": "click", "ref": 1},
    ]}) is True
    assert t.needs_confirm({"steps": [
        {"action": "read"}, {"action": "wait", "ms": 10},
    ]}) is False


def test_browser_snapshot_tool_is_registered_read_only():
    """browser_snapshot 必须存在且是只读工具：它只取页面结构，不改页面。"""
    from app.agents.tools.browser_tools import BROWSER_TOOLS

    by_name = {t.name: t for t in BROWSER_TOOLS}
    assert "browser_snapshot" in by_name, "缺少 browser_snapshot 工具"
    snap = by_name["browser_snapshot"]
    assert snap.kind == "read"
    assert callable(snap.run)
    assert inspect.iscoroutinefunction(snap.run)
