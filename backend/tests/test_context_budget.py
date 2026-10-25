"""上下文预算：窗口推断、阈值推导、按 token 预算取原文。

这些是纯函数级用例，不碰数据库与 LLM —— 预算口径一旦算错，环上显示的数字
和真正发出去的 prompt 就会分叉，而这类偏差在用例里比在浏览器里好定位。
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.services import context_service as cs


class _Msg:
    """最小消息替身：_tail_within_budget 只读 role / content。"""

    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


@pytest.fixture()
def clean_cfg(monkeypatch):
    """把四项上下文配置复位到自动模式，避免用例互相污染。"""
    for name, val in (
        ("context_window_tokens", 0),
        ("context_window_ratio", 0.7),
        ("context_output_reserve_tokens", 4096),
        ("context_compress_threshold", 0),
        ("context_keep_recent", 6),
    ):
        monkeypatch.setattr(settings, name, val, raising=False)
    yield


# ------------------------------------------------------------------ 窗口推断


@pytest.mark.parametrize(
    ("model", "expect"),
    [
        ("gpt-4o", 128_000),
        ("gpt-4o-mini", 128_000),          # 更具体的 gpt-4o 必须先命中，不能落到 gpt-4 的 8k
        ("gpt-4-turbo", 128_000),
        ("gpt-4", 8_192),
        ("deepseek-chat", 65_536),
        ("qwen-max", 32_768),
        ("qwen-long", 1_000_000),
        ("claude-3-5-sonnet", 200_000),
        ("llama-3.1-70b", 131_072),
        ("llama-3-8b", 8_192),
    ],
)
def test_window_hints(clean_cfg, model, expect):
    assert cs.window_tokens(model) == expect


def test_window_unknown_model_uses_conservative_default(clean_cfg):
    assert cs.window_tokens("some-local-7b") == cs.DEFAULT_WINDOW_TOKENS
    assert cs.window_tokens(None) == cs.DEFAULT_WINDOW_TOKENS


def test_window_explicit_config_wins(clean_cfg, monkeypatch):
    monkeypatch.setattr(settings, "context_window_tokens", 16_384, raising=False)
    # 显式配置优先于模型名推断
    assert cs.window_tokens("gpt-4o") == 16_384


# ------------------------------------------------------------------ 阈值推导


def test_threshold_auto_derives_from_window(clean_cfg):
    win = 128_000
    expect = int(win * 0.7) - 4096
    assert cs.threshold_tokens("gpt-4o") == expect
    # 小窗口模型自动保守：输出预留不超过窗口 25%
    small = cs.threshold_tokens("gpt-4")
    assert small == max(1024, int(8_192 * 0.7) - int(8_192 * 0.25))


def test_threshold_ratio_is_clamped(clean_cfg, monkeypatch):
    monkeypatch.setattr(settings, "context_window_ratio", 5.0, raising=False)
    hi = cs.threshold_tokens("gpt-4o")
    monkeypatch.setattr(settings, "context_window_ratio", 0.01, raising=False)
    lo = cs.threshold_tokens("gpt-4o")
    assert lo == 1024 or lo < hi
    # 0.95 上限：阈值不可能超过窗口本身
    assert hi < 128_000


def test_threshold_explicit_overrides_auto(clean_cfg, monkeypatch):
    monkeypatch.setattr(settings, "context_compress_threshold", 3000, raising=False)
    assert cs.threshold_tokens("gpt-4o") == 3000


def test_output_reserve_capped_at_quarter_window(clean_cfg, monkeypatch):
    monkeypatch.setattr(settings, "context_output_reserve_tokens", 999_999, raising=False)
    assert cs.output_reserve_tokens(8_192) == int(8_192 * 0.25)
    # 下限 512：极小窗口也不会算出 0 预留
    assert cs.output_reserve_tokens(1024) >= 512


# ------------------------------------------------------------------ 预算取原文


def test_tail_takes_from_newest_within_budget(clean_cfg):
    rows = [_Msg("user", "甲" * 300), _Msg("assistant", "乙" * 300), _Msg("user", "丙" * 300)]
    tail = cs._tail_within_budget(rows, budget=10_000)
    assert [m.content[0] for m in tail] == ["甲", "乙", "丙"]


def test_tail_stops_at_budget_and_keeps_question_first(clean_cfg):
    rows = [_Msg("user", "甲" * 300), _Msg("assistant", "乙" * 300), _Msg("user", "丙" * 300),
            _Msg("assistant", "丁" * 300)]
    one = cs._msg_tokens(rows[0])
    # 预算只够最后两条 → 再往前加一条会超；且截断后首条必须是 user
    tail = cs._tail_within_budget(rows, budget=one * 2)
    assert tail
    assert tail[0].role == "user"
    assert tail[-1] is rows[-1]


def test_tail_always_keeps_last_message_even_over_budget(clean_cfg):
    rows = [_Msg("user", "短"), _Msg("assistant", "长" * 5000)]
    tail = cs._tail_within_budget(rows, budget=0)
    # 预算为 0 也必须留最后一条，否则模型看不到"刚刚说了什么"
    assert len(tail) == 1
    assert tail[0] is rows[-1]


def test_tail_drops_leading_assistant(clean_cfg):
    rows = [_Msg("user", "问" * 300), _Msg("assistant", "答" * 300), _Msg("user", "再问" * 150)]
    one = cs._msg_tokens(rows[1])
    # 预算够「答 + 再问」，但「问」进不来：首条 assistant 没有对应提问，丢掉
    tail = cs._tail_within_budget(rows, budget=one * 2)
    assert [m.role for m in tail] == ["user"]


def test_keep_min_respected_when_budget_tight(clean_cfg):
    rows = [_Msg("user", "甲" * 400), _Msg("assistant", "乙" * 400), _Msg("user", "丙" * 400)]
    one = cs._msg_tokens(rows[0])
    tail = cs._tail_within_budget(rows, budget=one, keep_min=3)
    assert len(tail) == 3
