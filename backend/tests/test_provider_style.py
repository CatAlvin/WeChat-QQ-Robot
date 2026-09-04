from datetime import datetime, timedelta, timezone

from app.core.enums import MessageAuthor
from app.models.entities import Message
from app.services.provider_style import normalize_provider_reply


def _recent_kimi(content: str, *, seconds_ago: int = 0) -> Message:
    return Message(
        id=f"recent-{seconds_ago}",
        platform="QQ_NAPCAT",
        external_message_id=f"external-{seconds_ago}",
        conversation_id="conversation-1",
        author=MessageAuthor.AI,
        content=content,
        provider="Kimi Cloud",
        created_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago),
    )


def test_kimi_fixed_paw_sparkle_signature_is_removed() -> None:
    result = normalize_provider_reply(
        content="当然可以，我来帮你看看。🐾  ✨",
        provider="Kimi Cloud",
        inbound_content="帮我看看这个问题",
        recent=[],
    )
    assert result.content == "当然可以，我来帮你看看。"
    assert result.changed
    assert result.reason == "FIXED_EMOJI_SIGNATURE"
    assert result.removed_emoji_count == 2


def test_kimi_multiple_emojis_are_limited_to_one() -> None:
    result = normalize_provider_reply(
        content="今天真不错😊🎈😄",
        provider="Kimi Cloud",
        inbound_content="今天天气真好",
        recent=[],
    )
    assert result.content == "今天真不错😊"
    assert result.removed_emoji_count == 2


def test_kimi_emoji_is_suppressed_when_recent_reply_used_one() -> None:
    result = normalize_provider_reply(
        content="好呀，我们继续😊",
        provider="Kimi Cloud",
        inbound_content="继续说",
        recent=[_recent_kimi("没问题🙂")],
    )
    assert result.content == "好呀，我们继续"
    assert result.reason == "EMOJI_COOLDOWN"


def test_kimi_does_not_reuse_a_signature_emoji_after_cooldown() -> None:
    result = normalize_provider_reply(
        content="我会记住的🐾",
        provider="Kimi Cloud",
        inbound_content="记住了吗",
        recent=[
            _recent_kimi("记住啦。", seconds_ago=1),
            _recent_kimi("我在听。", seconds_ago=2),
            _recent_kimi("当然可以🐾", seconds_ago=3),
        ],
    )
    assert result.content == "我会记住的"
    assert result.reason == "REPEATED_EMOJI"


def test_explicit_emoji_output_request_is_not_modified() -> None:
    original = "😊✨"
    result = normalize_provider_reply(
        content=original,
        provider="Kimi Cloud",
        inbound_content="请只回复😊✨",
        recent=[_recent_kimi("好的🐾")],
    )
    assert result.content == original
    assert not result.changed


def test_other_providers_are_not_modified() -> None:
    original = "本地模型回复😊✨"
    result = normalize_provider_reply(
        content=original,
        provider="Ollama Local",
        inbound_content="你好",
        recent=[],
    )
    assert result.content == original
    assert not result.changed
