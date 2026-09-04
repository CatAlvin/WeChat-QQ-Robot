from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.channels.base import ChannelConnector, InboundEvent, OutboundMessage, SendPermit, SendResult
from app.core.config import Settings
from app.llm.base import LLMMessage, LLMResponse
from app.llm.gateway import LLMGateway
from app.models.entities import Account, Contact, Message, PersonaProfile, RuntimeState, TodoItem
from app.services.delegated_todo import detect_delegated_todo
from app.services import online_tools
from app.services.online_tools import _extract_location, _geocoding_candidates, _source_tier, calendar_snapshot, online_context_for_message
from app.services.pipeline import MessagePipeline


class RecordingConnector(ChannelConnector):
    platform = "QQ_NAPCAT"
    real_channel = False

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
        permit.assert_valid_for(message)
        self.sent.append(message)
        return SendResult(True, external_message_id=f"relay-test-{len(self.sent)}")

    async def status(self) -> dict[str, str]:
        return {"status": "ONLINE"}


class RelayProvider:
    name = "relay-provider"

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        assert "管理员答复：我现在正在吃饭" in messages[-1].content
        return LLMResponse("主人说他现在正在吃饭。", "relay-model", self.name, 1, 12, 15)

    async def test_connection(self) -> bool:
        return True

    async def models(self) -> list[str]:
        return ["relay-model"]


def _event(contact: Contact, account_id: str, message_id: str, content: str, *, reply_to: str | None = None) -> InboundEvent:
    return InboundEvent(
        platform="QQ_NAPCAT",
        account_id=account_id,
        message_id=message_id,
        conversation_id=f"napcat:private:{contact.platform_user_id}",
        sender_id=contact.platform_user_id,
        sender_name=contact.display_name,
        content=content,
        reply_to_message_id=reply_to,
        timestamp=datetime.now(timezone.utc),
        raw={"event_type": "ONEBOT11_MESSAGE", "text_content": content},
    )


def test_delegated_intent_requires_whitelist_owner_and_action() -> None:
    contact = Contact(platform="QQ_NAPCAT", platform_user_id="1", display_name="大臭狐", relationship_label="朋友", whitelisted=True)
    assert detect_delegated_todo(contact, "帮我问问你主人在做什么", is_group=False, message_type="TEXT") is not None
    assert detect_delegated_todo(contact, "帮我和主人说外卖不要留我电话", is_group=False, message_type="TEXT") is not None
    direct = detect_delegated_todo(contact, "你主人在做什么", is_group=False, message_type="TEXT")
    assert direct is not None and direct.asks_question
    assert "后台待办" in direct.acknowledgement
    assert detect_delegated_todo(contact, "你主人在干什么，怎么不找我玩", is_group=False, message_type="TEXT") is not None
    assert detect_delegated_todo(contact, "你主人今天忙吗", is_group=False, message_type="TEXT") is not None
    assert detect_delegated_todo(contact, "你主人今天看起来很忙", is_group=False, message_type="TEXT") is None
    contact.whitelisted = False
    assert detect_delegated_todo(contact, "帮我问问主人", is_group=False, message_type="TEXT") is None


@pytest.mark.asyncio
async def test_calendar_context_uses_beijing_time_without_network() -> None:
    result = await online_context_for_message("今天星期几？")
    assert result.tools == ("CALENDAR",)
    assert "北京时间日历" in result.text
    assert calendar_snapshot()["timezone"] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_weather_and_search_context_use_fresh_tool_results(monkeypatch) -> None:
    async def fake_weather(location: str) -> dict:
        assert location == "天津"
        return {
            "location": "天津 · 中国",
            "observed_at": "2026-08-30T12:00",
            "current": {
                "weather": "晴",
                "temperature": 28,
                "apparent_temperature": 29,
                "humidity": 40,
                "wind_speed": 8,
            },
            "forecast": [
                {"date": "2026-08-30", "weather": "晴", "temperature_min": 20, "temperature_max": 30, "precipitation_probability": 0}
            ],
        }

    async def fake_search(query: str) -> dict:
        assert "SAM" in query
        return {"results": [{"title": "SAM", "snippet": "分割模型资料", "url": "https://example.com/sam"}]}

    monkeypatch.setattr(online_tools, "weather_lookup", fake_weather)
    monkeypatch.setattr(online_tools, "web_search", fake_search)
    result = await online_context_for_message("查一下天津天气，再联网搜索 SAM 最新资料")
    assert result.tools == ("WEATHER", "WEB_SEARCH")
    assert "天津 · 中国" in result.text
    assert "https://example.com/sam" in result.text


def test_weather_location_ignores_relative_day_and_has_geocoding_fallbacks() -> None:
    assert _extract_location("今天东莞市天气怎么样呢") == "东莞市"
    assert _geocoding_candidates("广东省东莞市") == ("广东省东莞市", "东莞市", "东莞")


@pytest.mark.asyncio
async def test_weather_location_followup_reuses_recent_intent(monkeypatch) -> None:
    async def fake_weather(location: str) -> dict:
        assert location == "广东省东莞市"
        return {
            "location": "东莞市 · 广东 · 中国",
            "observed_at": "2026-08-30T15:00",
            "current": {"weather": "多云", "temperature": 30, "apparent_temperature": 34, "humidity": 70, "wind_speed": 7},
            "forecast": [{"date": "2026-08-30", "weather": "多云", "temperature_min": 25, "temperature_max": 32, "precipitation_probability": 20}],
        }

    monkeypatch.setattr(online_tools, "weather_lookup", fake_weather)
    result = await online_context_for_message(
        "广东省东莞市",
        recent_messages=("请告诉我具体城市，我再查询天气。", "今天东莞市天气怎么样呢"),
    )
    assert result.tools == ("WEATHER",)
    assert "东莞市 · 广东 · 中国" in result.text


@pytest.mark.asyncio
async def test_unrelated_network_question_is_not_weather_followup() -> None:
    result = await online_context_for_message(
        "你现在可以联网了吗",
        recent_messages=("请告诉我具体城市，我再查询天气。", "今天东莞天气怎么样"),
    )
    assert result.tools == ()
    assert result.errors == ()


@pytest.mark.asyncio
async def test_plain_lookup_request_triggers_web_search(monkeypatch) -> None:
    async def fake_search(query: str) -> dict:
        assert query == "2026年世界杯获胜队伍是哪个吗"
        return {"results": [{"title": "比赛结果", "snippet": "冠军资料", "url": "https://example.com/result"}]}

    monkeypatch.setattr(online_tools, "web_search", fake_search)
    result = await online_context_for_message("那你可以查查2026年世界杯获胜队伍是哪个吗")
    assert result.tools == ("WEB_SEARCH",)
    assert "冠军资料" in result.text
    assert "普通网页" in result.text


@pytest.mark.asyncio
async def test_search_capability_question_probes_channel_without_searching_the_question(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_search(query: str, **_: object) -> dict:
        calls.append(query)
        return {"results": [{"title": "OpenAI", "snippet": "official", "url": "https://openai.com"}]}

    monkeypatch.setattr(online_tools, "web_search", fake_search)
    result = await online_context_for_message("你现在可以联网搜索了吗")
    assert result.tools == ("WEB_SEARCH_STATUS",)
    assert calls == ["OpenAI 官方网站"]
    assert "本轮连通性探测成功" in result.text


def test_search_source_tier_only_trusts_registered_official_domains() -> None:
    assert _source_tier("https://www.fifa.com/en/tournaments/worldcup") == "权威来源"
    assert _source_tier("https://inside.fifa.com/news/result") == "权威来源"
    assert _source_tier("https://fifa.com.example.org/fake") == "普通网页"
    assert _source_tier("http://www.bnbu.edu.cn/about_us/overview/introducing.htm") == "权威来源"


@pytest.mark.asyncio
async def test_contact_relay_creates_todo_notifies_admin_and_completes_from_quote(db, tmp_path) -> None:
    account = Account(
        platform="QQ_NAPCAT",
        display_name="测试账号",
        connector_kind="NAPCAT_ONEBOT11",
        status="ONLINE",
        enabled=True,
        credential_id="test",
        config={"managed_qq_id": "1000", "risk_acknowledged": True, "api_base": "http://127.0.0.1:3001"},
    )
    db.add(account)
    db.flush()
    admin = Contact(
        platform="QQ_NAPCAT",
        account_id=account.id,
        platform_user_id="1000",
        display_name="管理员",
        relationship_label="管理员",
        whitelisted=True,
        ai_enabled=True,
    )
    source = Contact(
        platform="QQ_NAPCAT",
        account_id=account.id,
        platform_user_id="2000",
        display_name="大臭狐",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
    )
    db.add_all(
        [
            RuntimeState(id=1, release_gate="LIVE", global_mode="AUTO", kill_switch=False, live_time_window_enabled=False),
            PersonaProfile(id=1),
            admin,
            source,
        ]
    )
    db.commit()
    connector = RecordingConnector()
    pipeline = MessagePipeline(
        gateway=LLMGateway([RelayProvider()]),
        connectors={"QQ_NAPCAT": connector},
        settings=Settings(secret_key="z" * 40, data_dir=tmp_path, contact_per_minute=8, max_consecutive_sends=8),
    )

    created = await pipeline.handle(
        _event(source, account.id, "source-request", "帮我问问你主人在做什么"), db
    )
    todo = db.scalar(select(TodoItem).where(TodoItem.kind == "CONTACT_RELAY"))
    assert created.code == "CONTACT_RELAY_CREATED"
    assert todo is not None and todo.status == "OPEN" and todo.delivery_status == "NOTIFIED"
    assert [item.target_id for item in connector.sent] == [admin.platform_user_id, source.platform_user_id]
    notification = db.get(Message, todo.admin_notification_message_id)
    assert notification is not None and notification.external_message_id == "relay-test-1"
    assert "大臭狐" in notification.content and "直接引用这条消息回复" in notification.content

    completed = await pipeline.handle(
        _event(admin, account.id, "admin-answer", "我现在正在吃饭", reply_to=notification.external_message_id), db
    )
    db.refresh(todo)
    assert completed.code == "CONTACT_RELAY_DELIVERED"
    assert todo.status == "DONE" and todo.delivery_status == "DELIVERED"
    assert todo.response_text == "主人说他现在正在吃饭。"
    assert connector.sent[-2].target_id == source.platform_user_id
    assert connector.sent[-2].content == "主人说他现在正在吃饭。"
    assert connector.sent[-1].target_id == admin.platform_user_id
    assert "待办已完成" in connector.sent[-1].content


@pytest.mark.asyncio
async def test_contact_relay_records_todo_and_honestly_acks_when_admin_notice_fails(db, tmp_path) -> None:
    account = Account(
        platform="QQ_NAPCAT",
        display_name="测试账号",
        connector_kind="NAPCAT_ONEBOT11",
        status="ONLINE",
        enabled=True,
        credential_id="test",
        config={"managed_qq_id": "1000", "risk_acknowledged": True, "api_base": "http://127.0.0.1:3001"},
    )
    db.add(account)
    db.flush()
    admin = Contact(
        platform="QQ_NAPCAT",
        account_id=account.id,
        platform_user_id="1000",
        display_name="管理员",
        relationship_label="管理员",
        whitelisted=True,
        ai_enabled=True,
    )
    source = Contact(
        platform="QQ_NAPCAT",
        account_id=account.id,
        platform_user_id="2000",
        display_name="大臭狐",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
    )
    db.add_all(
        [
            RuntimeState(id=1, release_gate="LIVE", global_mode="AUTO", kill_switch=False, live_time_window_enabled=False),
            PersonaProfile(id=1),
            admin,
            source,
        ]
    )
    db.commit()

    class NoticeFailureConnector(RecordingConnector):
        async def send(self, message: OutboundMessage, permit: SendPermit) -> SendResult:
            permit.assert_valid_for(message)
            self.sent.append(message)
            if message.target_id == admin.platform_user_id:
                return SendResult(False, error_code="NAPCAT_HTTP_502", error_detail="HTTP 502")
            return SendResult(True, external_message_id=f"relay-test-{len(self.sent)}")

    connector = NoticeFailureConnector()
    pipeline = MessagePipeline(
        gateway=LLMGateway([RelayProvider()]),
        connectors={"QQ_NAPCAT": connector},
        settings=Settings(secret_key="x" * 40, data_dir=tmp_path, contact_per_minute=8, max_consecutive_sends=8),
    )

    result = await pipeline.handle(
        _event(source, account.id, "source-natural-question", "你主人在做什么"), db
    )
    todo = db.scalar(select(TodoItem).where(TodoItem.kind == "CONTACT_RELAY"))

    assert result.code == "CONTACT_RELAY_NOTIFY_FAILED"
    assert result.sent
    assert todo is not None and todo.status == "OPEN" and todo.delivery_status == "NOTIFY_FAILED"
    assert [item.target_id for item in connector.sent] == [admin.platform_user_id, source.platform_user_id]
    assert "后台待办" in connector.sent[-1].content and "没有送达" in connector.sent[-1].content


@pytest.mark.asyncio
async def test_contact_relay_never_crosses_account_boundary(db, tmp_path) -> None:
    source_account = Account(
        platform="QQ_NAPCAT",
        display_name="当前账号",
        connector_kind="NAPCAT_ONEBOT11",
        status="ONLINE",
        enabled=True,
        credential_id="source",
        config={"managed_qq_id": "1000", "risk_acknowledged": True},
    )
    other_account = Account(
        platform="QQ_NAPCAT",
        display_name="另一账号",
        connector_kind="NAPCAT_ONEBOT11",
        status="OFFLINE",
        enabled=False,
        credential_id="other",
        config={"managed_qq_id": "3000", "risk_acknowledged": True},
    )
    db.add_all([source_account, other_account])
    db.flush()
    source = Contact(
        platform="QQ_NAPCAT",
        account_id=source_account.id,
        platform_user_id="2000",
        display_name="大臭狐",
        relationship_label="朋友",
        whitelisted=True,
        ai_enabled=True,
    )
    other_admin = Contact(
        platform="QQ_NAPCAT",
        account_id=other_account.id,
        platform_user_id="3000",
        display_name="另一账号管理员",
        relationship_label="管理员",
        whitelisted=True,
        ai_enabled=True,
    )
    db.add_all([RuntimeState(id=1, release_gate="LIVE", global_mode="AUTO"), PersonaProfile(id=1), source, other_admin])
    db.commit()
    connector = RecordingConnector()
    pipeline = MessagePipeline(
        gateway=LLMGateway([RelayProvider()]),
        connectors={"QQ_NAPCAT": connector},
        settings=Settings(secret_key="y" * 40, data_dir=tmp_path),
    )

    result = await pipeline.handle(
        _event(source, source_account.id, "source-cross-account", "帮我问问你主人在做什么"), db
    )
    todo = db.scalar(select(TodoItem).where(TodoItem.kind == "CONTACT_RELAY"))
    assert result.code == "CONTACT_RELAY_ADMIN_MISSING"
    assert todo is not None and todo.admin_contact_id is None and todo.delivery_status == "ADMIN_MISSING"
    assert result.sent
    assert [item.target_id for item in connector.sent] == [source.platform_user_id]
    assert "后台待办" in connector.sent[0].content and "管理员" in connector.sent[0].content
