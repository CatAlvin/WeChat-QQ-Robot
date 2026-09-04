from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import product

import pytest

from app.core.enums import ConversationMode, GlobalMode, Importance
from app.policy.engine import PolicyEngine, PolicyInput, requires_human_for_formal_topic


TZ = timezone(timedelta(hours=8))
MIDDAY = datetime(2026, 8, 24, 12, 0, tzinfo=TZ)


def baseline(**changes) -> PolicyInput:
    values = dict(
        global_mode=GlobalMode.AUTO,
        kill_switch=False,
        whitelisted=True,
        ai_enabled=True,
        importance=Importance.NORMAL,
        relationship_label="朋友",
        is_group=False,
        group_allowed=False,
        mentioned_user=False,
        conversation_mode=ConversationMode.AUTO_READY,
        managed_rounds=0,
        now=MIDDAY,
        inbound_content="晚上吃什么？",
    )
    values.update(changes)
    return PolicyInput(**values)


@pytest.mark.parametrize(
    "whitelisted,kill,auto,manual,is_group,group_allowed,mentioned",
    list(product([False, True], repeat=7)),
)
def test_safety_invariants_are_never_bypassed(whitelisted, kill, auto, manual, is_group, group_allowed, mentioned):
    decision = PolicyEngine().evaluate(
        baseline(
            whitelisted=whitelisted,
            kill_switch=kill,
            global_mode=GlobalMode.AUTO if auto else GlobalMode.SILENT,
            importance=Importance.MANUAL_ONLY if manual else Importance.NORMAL,
            is_group=is_group,
            group_allowed=group_allowed,
            mentioned_user=mentioned,
            relationship_label="朋友",
        )
    )
    expected = whitelisted and not kill and auto and not manual and (not is_group or (group_allowed and mentioned))
    assert decision.allowed is expected


@pytest.mark.parametrize("hour", list(range(24)))
def test_time_window_is_exact(hour: int):
    now = datetime(2026, 8, 24, hour, 0, tzinfo=TZ)
    decision = PolicyEngine().evaluate(baseline(now=now))
    assert decision.allowed is (10 <= hour <= 23)


def test_2330_boundary_is_blocked():
    assert PolicyEngine().evaluate(baseline(now=datetime(2026, 8, 24, 23, 29, tzinfo=TZ))).allowed
    assert not PolicyEngine().evaluate(baseline(now=datetime(2026, 8, 24, 23, 30, tzinfo=TZ))).allowed


def test_disabled_time_window_allows_model_chain_but_not_other_hard_denials():
    outside = baseline(now=datetime(2026, 8, 24, 0, 50, tzinfo=TZ), enforce_time_window=False)
    assert PolicyEngine().evaluate(outside).allowed
    assert PolicyEngine().evaluate(baseline(now=outside.now, enforce_time_window=False, kill_switch=True)).code == "KILL_SWITCH"


@pytest.mark.parametrize(
    "hour,allowed",
    [(21, False), (22, True), (23, True), (0, True), (1, True), (2, False), (3, False)],
)
def test_overnight_time_window(hour: int, allowed: bool):
    decision = PolicyEngine().evaluate(
        baseline(
            now=datetime(2026, 8, 24, hour, 0, tzinfo=TZ),
            auto_start=datetime.strptime("22:00", "%H:%M").time(),
            auto_end=datetime.strptime("02:00", "%H:%M").time(),
        )
    )
    assert decision.allowed is allowed


def test_disabled_channel_is_a_hard_policy_denial():
    decision = PolicyEngine().evaluate(baseline(channel_enabled=False))
    assert not decision.allowed
    assert decision.code == "CHANNEL_DISABLED"


@pytest.mark.parametrize("relationship", ["老师", "导师", "HR", "领导", "客户", "家人", "陌生人"])
def test_whitelisted_formal_relationships_can_receive_safe_replies(relationship: str):
    decision = PolicyEngine().evaluate(baseline(relationship_label=relationship))
    assert decision.allowed


@pytest.mark.parametrize("topic", ["学校申请", "老师找我", "导师意见", "HR 面试", "招聘", "工作合同", "领导", "客户", "签证", "律师", "法律"])
def test_formal_topics_are_detected_for_recording_but_allowed_to_reply(topic: str):
    decision = PolicyEngine().evaluate(baseline(inbound_content=topic))
    assert decision.allowed
    assert requires_human_for_formal_topic(topic)


@pytest.mark.parametrize(
    "question",
    [
        "BNBU是哪所学校，全称是什么，哪年建校，办学性质是什么",
        "爱因斯坦的老师是谁",
        "客户这个词是什么意思",
        "今天是不是工作日",
    ],
)
def test_factual_questions_mentioning_formal_entities_are_not_misclassified(question: str):
    assert PolicyEngine().evaluate(baseline(inbound_content=question)).allowed


def test_human_takeover_blocks_until_expiry():
    active = PolicyEngine().evaluate(
        baseline(conversation_mode=ConversationMode.HUMAN, human_until=MIDDAY + timedelta(minutes=9))
    )
    expired_but_not_transitioned = PolicyEngine().evaluate(
        baseline(conversation_mode=ConversationMode.HUMAN, human_until=MIDDAY - timedelta(seconds=1))
    )
    assert not active.allowed
    assert not expired_but_not_transitioned.allowed


def test_round_limit_is_programmatic():
    assert PolicyEngine().evaluate(baseline(managed_rounds=39)).allowed
    assert not PolicyEngine().evaluate(baseline(managed_rounds=40)).allowed


def test_admin_normal_chat_is_not_silenced_by_contact_round_cooldown():
    assert PolicyEngine().evaluate(
        baseline(relationship_label="管理员", conversation_mode=ConversationMode.CONTACT_COOLDOWN, managed_rounds=40)
    ).allowed


@pytest.mark.parametrize("secret", ["sk-abcdefghijklmnop1234", "api_key=abcdefghijk123", "password=my-secret-value"])
def test_inbound_secret_never_reaches_llm(secret: str):
    decision = PolicyEngine().evaluate(baseline(inbound_content=secret))
    assert not decision.allowed
    assert decision.code == "INBOUND_SECRET"
    assert decision.needs_attention
