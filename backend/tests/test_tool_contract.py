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
