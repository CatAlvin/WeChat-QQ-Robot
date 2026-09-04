from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.policy.engine import ResponseGuard
from app.policy.rate_limit import RateLimiter
from app.core.language import to_simplified_chinese


@pytest.mark.parametrize(
    "content,code",
    [
        ("我现在给你转账", "MONEY_COMMITMENT"),
        ("我保证借你一万元", "MONEY_COMMITMENT"),
        ("银行卡号是 123", "MONEY_COMMITMENT"),
        ("sk-abcdefghijklmnopqrstuvwxyz123456", "SECRET_EXPOSURE"),
        ("api_key=abcdef123456789", "SECRET_EXPOSURE"),
        ("-----BEGIN PRIVATE KEY-----", "SECRET_EXPOSURE"),
        ("我代表公司确认签署合同", "FORMAL_COMMITMENT"),
    ],
)
def test_response_guard_blocks_high_risk_content(content: str, code: str):
    decision = ResponseGuard().inspect(content)
    assert not decision.allowed
    assert decision.code == code


def test_response_guard_keeps_cat_persona_subtle():
    decision = ResponseGuard().inspect("好呀喵，收到喵，等你喵")
    assert decision.allowed
    assert decision.content.count("喵") == 1


def test_response_guard_deterministically_converts_traditional_chinese():
    decision = ResponseGuard().inspect("歡迎來到網路世界，這個軟體很實用。")

    assert decision.allowed
    assert decision.content == "欢迎来到网络世界，这个软件很实用。"


def test_simplified_conversion_preserves_code_urls_and_windows_paths():
    content = (
        "請查看 `臺灣變數` 和 https://example.com/繁體 ，檔案在 "
        "D:\\資料\\繁體.txt。\n```python\n名稱 = '繁體'\n```"
    )

    converted = to_simplified_chinese(content)

    assert converted.startswith("请查看 `臺灣變數` 和 ")
    assert "https://example.com/繁體" in converted
    assert "D:\\資料\\繁體.txt" in converted
    assert "```python\n名稱 = '繁體'\n```" in converted


def test_important_contact_guard_blocks_strong_commitments_and_shortens_long_replies():
    commitment = ResponseGuard().inspect("我一定会把这件事处理好", strict=True)
    assert not commitment.allowed
    assert commitment.code == "IMPORTANT_COMMITMENT"
    assert ResponseGuard().inspect("我一定会把这件事处理好").allowed

    long_reply = ResponseGuard().inspect("好" * 500, strict=True)
    assert long_reply.allowed
    assert len(long_reply.content) == 400


def test_rate_limit_contact_burst():
    limiter = RateLimiter()
    now = datetime.now(timezone.utc)
    for index in range(5):
        assert limiter.allow("alice", now=now + timedelta(seconds=index)).allowed
        limiter.record("alice", now + timedelta(seconds=index))
    assert not limiter.allow("alice", now=now + timedelta(seconds=10)).allowed
    assert limiter.allow("alice", now=now + timedelta(seconds=61)).allowed


@pytest.mark.parametrize("daily", [500, 501, 999, 1000, 5000])
def test_daily_limit_forces_silent(daily: int):
    decision = RateLimiter().allow("alice", daily_sent=daily)
    assert not decision.allowed
    assert decision.force_silent


def test_consecutive_send_limit():
    limiter = RateLimiter()
    assert limiter.allow("alice", consecutive_sends=2).allowed
    assert not limiter.allow("alice", consecutive_sends=3).allowed
