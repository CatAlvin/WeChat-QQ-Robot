from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.channels.base import InboundEvent
from app.channels.simulator import SimulatorConnector
from app.core.config import Settings
from app.core.enums import Importance, MessageAuthor, MessageStatus
from app.llm.gateway import LLMGateway
from app.llm.providers import SimulatorProvider
from app.database import Base
from app.models.entities import Contact, Message, PersonaProfile, RuntimeState
from app.services.pipeline import MessagePipeline


@pytest.mark.asyncio
async def test_one_thousand_events_have_no_loss_duplicate_or_wrong_recipient(db):
    db.add(RuntimeState(id=1, release_gate="SIMULATION"))
    db.add(PersonaProfile(id=1))
    allowed: list[Contact] = []
    denied: list[Contact] = []
    for index in range(100):
        allowed.append(Contact(platform="SIMULATOR", platform_user_id=f"allowed-{index}", display_name=f"好友 {index}", relationship_label="朋友", whitelisted=True, importance=Importance.NORMAL))
        denied.append(Contact(platform="SIMULATOR", platform_user_id=f"denied-{index}", display_name=f"陌生人 {index}", relationship_label="陌生人", whitelisted=False, importance=Importance.MANUAL_ONLY))
    db.add_all(allowed + denied)
    db.commit()
    connector = SimulatorConnector()
    settings = Settings(
        data_dir="data",
        auto_start="00:00",
        auto_end="23:59",
        daily_send_limit=500,
        daily_send_hard_limit=1000,
        contact_per_minute=5,
    )
    pipeline = MessagePipeline(
        gateway=LLMGateway([SimulatorProvider()]),
        connectors={"SIMULATOR": connector},
        settings=settings,
    )
    timestamp = datetime(2026, 8, 24, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    results = []
    for index, contact in enumerate(allowed + denied):
        for turn in range(5):
            message_id = f"event-{index}-{turn}"
            results.append(
                await pipeline.handle(
                    InboundEvent(
                        platform="SIMULATOR",
                        message_id=message_id,
                        conversation_id=f"sim:{contact.platform_user_id}",
                        sender_id=contact.platform_user_id,
                        sender_name=contact.display_name,
                        content="你好，今天怎么样？",
                        timestamp=timestamp,
                    ),
                    db,
                )
            )
    assert len(results) == 1000
    assert sum(item.sent for item in results) == 500
    assert len(connector.sent) == 500
    assert {item["target_id"] for item in connector.sent} == {item.platform_user_id for item in allowed}
    assert db.scalar(select(func.count(Message.id)).where(Message.direction == "INBOUND")) == 1000
    assert db.scalar(select(func.count(Message.id)).where(Message.author == MessageAuthor.AI, Message.status == MessageStatus.SENT)) == 500
    assert db.scalar(select(func.count(Message.external_message_id.distinct()))) == db.scalar(select(func.count(Message.id)))

    duplicate = await pipeline.handle(
        InboundEvent(
            platform="SIMULATOR",
            message_id="event-0-0",
            conversation_id="sim:allowed-0",
            sender_id="allowed-0",
            sender_name="好友 0",
            content="重复 webhook",
            timestamp=timestamp,
        ),
        db,
    )
    assert duplicate.duplicate
    assert len(connector.sent) == 500


@pytest.mark.asyncio
async def test_contact_minute_limit_survives_pipeline_reconstruction(db):
    db.add(RuntimeState(id=1, release_gate="SIMULATION"))
    db.add(PersonaProfile(id=1))
    contact = Contact(
        platform="SIMULATOR",
        platform_user_id="persistent-rate-user",
        display_name="频控联系人",
        relationship_label="朋友",
        whitelisted=True,
        importance=Importance.NORMAL,
    )
    db.add(contact)
    db.commit()
    connector = SimulatorConnector()
    timestamp = datetime(2026, 8, 24, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    results = []
    for index in range(6):
        # Real API/webhook processing constructs a fresh pipeline per event.
        pipeline = MessagePipeline(
            gateway=LLMGateway([SimulatorProvider()]),
            connectors={"SIMULATOR": connector},
            settings=Settings(contact_per_minute=5, auto_start="00:00", auto_end="23:59"),
        )
        results.append(
            await pipeline.handle(
                InboundEvent(
                    platform="SIMULATOR",
                    message_id=f"persistent-rate-{index}",
                    conversation_id="sim:persistent-rate-user",
                    sender_id=contact.platform_user_id,
                    sender_name=contact.display_name,
                    content="你好",
                    timestamp=timestamp,
                ),
                db,
            )
        )
    assert sum(item.sent for item in results) == 5
    assert results[-1].code == "CONTACT_MINUTE_LIMIT"
    assert len(connector.sent) == 5


@pytest.mark.asyncio
async def test_six_concurrent_events_reserve_at_most_five_sends(tmp_path):
    database_path = tmp_path / "concurrent-rate.db"
    local_engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(local_engine)
    factory = sessionmaker(bind=local_engine, expire_on_commit=False)
    with factory() as setup_db:
        setup_db.add(RuntimeState(id=1, release_gate="SIMULATION"))
        setup_db.add(PersonaProfile(id=1))
        setup_db.add(
            Contact(
                platform="SIMULATOR",
                platform_user_id="concurrent-user",
                display_name="并发频控联系人",
                relationship_label="朋友",
                whitelisted=True,
                importance=Importance.NORMAL,
            )
        )
        setup_db.commit()

    connector = SimulatorConnector()
    timestamp = datetime(2026, 8, 24, 12, 0, tzinfo=timezone(timedelta(hours=8)))

    async def run(index: int):
        with factory() as request_db:
            pipeline = MessagePipeline(
                    gateway=LLMGateway([SimulatorProvider()]),
                    connectors={"SIMULATOR": connector},
                    settings=Settings(contact_per_minute=5, auto_start="00:00", auto_end="23:59"),
            )
            return await pipeline.handle(
                InboundEvent(
                    platform="SIMULATOR",
                    message_id=f"concurrent-{index}",
                    conversation_id="sim:concurrent-user",
                    sender_id="concurrent-user",
                    sender_name="并发频控联系人",
                    content="你好",
                    timestamp=timestamp,
                ),
                request_db,
            )

    results = await asyncio.gather(*(run(index) for index in range(6)))
    assert sum(item.sent for item in results) == 5
    assert sum(item.code == "CONTACT_MINUTE_LIMIT" for item in results) == 1
    assert len(connector.sent) == 5
    local_engine.dispose()
