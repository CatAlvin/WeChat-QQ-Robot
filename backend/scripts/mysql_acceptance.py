from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.models.entities import Contact, Conversation, Message


REQUIRED_TABLES = {
    "acceptance_runs",
    "accounts",
    "api_credentials",
    "audit_logs",
    "contacts",
    "connector_events",
    "conversations",
    "conversation_summaries",
    "chat_groups",
    "incidents",
    "llm_providers",
    "memories",
    "message_attachments",
    "messages",
    "persona_profiles",
    "runtime_state",
    "users",
}


def describe_operational_error(error: OperationalError) -> str:
    args = getattr(error.orig, "args", ())
    code = args[0] if args else None
    messages = {
        1044: (
            "MySQL 权限不足（1044）：当前账号无权访问目标数据库。"
            "请使用 MySQL 管理员账号为该账号授予目标数据库权限后重试。"
        ),
        1045: "MySQL 登录失败（1045）：请检查用户名、密码以及账号允许登录的 Host。",
        1049: "MySQL 数据库不存在（1049）：请先用管理员账号创建目标数据库。",
        2003: "无法连接 MySQL（2003）：请确认服务已启动，并检查服务器地址和端口。",
    }
    return messages.get(
        code,
        f"MySQL 连接或验收失败（错误码 {code if code is not None else '未知'}）。",
    )


def main() -> None:
    database_url = os.environ.get("NEKO_DATABASE_URL", "")
    if not database_url:
        raise SystemExit("NEKO_DATABASE_URL is required")
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "mysql":
        raise SystemExit("MySQL acceptance refuses non-MySQL targets")

    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")

    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            assert connection.scalar(select(1)) == 1
            tables = set(inspect(connection).get_table_names())
            missing = sorted(REQUIRED_TABLES - tables)
            if missing:
                raise RuntimeError(f"missing required tables: {', '.join(missing)}")
            revision = str(connection.scalar(text("SELECT version_num FROM alembic_version")))
            connection.commit()

            transaction = connection.begin()
            suffix = uuid4().hex
            contact_id = str(uuid4())
            conversation_id = str(uuid4())
            message_id = str(uuid4())
            try:
                connection.execute(
                    insert(Contact).values(
                        id=contact_id,
                        platform="SIMULATOR",
                        platform_user_id=f"mysql-acceptance-{suffix}",
                        display_name="MySQL acceptance",
                        relationship_label="朋友",
                        whitelisted=False,
                        importance="MANUAL_ONLY",
                        ai_enabled=False,
                        style_profile="",
                        custom_prompt="",
                        memory_enabled=False,
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                connection.execute(
                    insert(Conversation).values(
                        id=conversation_id,
                        platform="SIMULATOR",
                        external_id=f"mysql-acceptance:{suffix}",
                        contact_id=contact_id,
                        mode="AUTO_READY",
                        managed_rounds=0,
                        consecutive_ai_sends=0,
                        last_active_at=datetime.now(timezone.utc),
                        created_at=datetime.now(timezone.utc),
                    )
                )
                connection.execute(
                    insert(Message).values(
                        id=message_id,
                        platform="SIMULATOR",
                        external_message_id=f"mysql-acceptance-{suffix}",
                        sender_id="mysql-acceptance-sender",
                        receiver_id="SIMULATOR:SELF",
                        event_at=datetime.now(timezone.utc),
                        conversation_id=conversation_id,
                        contact_id=contact_id,
                        direction="INBOUND",
                        author="CONTACT",
                        content="transactional acceptance probe",
                        message_type="TEXT",
                        status="RECEIVED",
                        input_tokens=0,
                        output_tokens=0,
                        latency_ms=0,
                        created_at=datetime.now(timezone.utc),
                    )
                )
                observed = connection.scalar(select(func.count(Message.id)).where(Message.id == message_id))
                if observed != 1:
                    raise RuntimeError("transactional CRUD probe was not readable")
            finally:
                transaction.rollback()
    finally:
        engine.dispose()

    reconnect_engine = create_engine(database_url, pool_pre_ping=True, future=True)
    try:
        with reconnect_engine.connect() as connection:
            reconnect_ok = connection.scalar(select(1)) == 1
    finally:
        reconnect_engine.dispose()

    print(
        json.dumps(
            {
                "status": "PASSED",
                "dialect": "mysql",
                "revision": revision,
                "required_tables": len(REQUIRED_TABLES),
                "transactional_crud_rolled_back": True,
                "reconnect": reconnect_ok,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except OperationalError as error:
        raise SystemExit(describe_operational_error(error)) from None
