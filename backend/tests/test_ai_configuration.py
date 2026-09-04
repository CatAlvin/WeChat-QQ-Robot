from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes import create_credential, create_provider, delete_provider, reorder_providers, update_provider
from app.models.entities import ApiCredential, AuditLog, Contact, PersonaProfile, ProviderConfig
from app.schemas import CredentialCreate, ProviderCreate, ProviderReorder, ProviderUpdate
from app.services.prompt import SAFETY_POLICY, build_messages


def _credential(db) -> ApiCredential:
    item = ApiCredential(
        label="Test key",
        provider="OPENAI",
        encrypted_secret="test-encrypted-value",
        masked_hint="sk-••••test",
    )
    db.add(item)
    db.commit()
    return item


def test_cloud_provider_requires_an_existing_encrypted_credential(db):
    payload = ProviderCreate(
        name="Cloud",
        provider_type="OPENAI",
        base_url="https://api.openai.com/v1",
        model="gpt-test",
        credential_id="missing",
    )
    with pytest.raises(HTTPException, match="有效的加密凭据"):
        create_provider(payload, db)


def test_credential_workflow_never_persists_or_logs_plaintext(db):
    raw = "sk-acceptance-secret-123456789"
    result = create_credential(CredentialCreate(label="Acceptance key", provider="DEEPSEEK", secret=raw), db)
    stored = db.get(ApiCredential, result.id)
    audits = list(db.scalars(select(AuditLog)))
    assert stored is not None
    assert raw not in stored.encrypted_secret
    assert raw not in stored.masked_hint
    assert all(raw not in str(item.detail) for item in audits)


def test_provider_can_be_updated_disabled_reordered_and_deleted(db):
    credential = _credential(db)
    item = create_provider(
        ProviderCreate(
            name="Primary",
            provider_type="OPENAI",
            base_url="https://api.openai.com/v1",
            model="gpt-test",
            credential_id=credential.id,
            priority=10,
        ),
        db,
    )

    updated = update_provider(
        item.id,
        ProviderUpdate(name="Fallback", enabled=False, priority=80, timeout_seconds=12),
        db,
    )
    assert updated.name == "Fallback"
    assert updated.enabled is False
    assert updated.priority == 80
    assert updated.timeout_seconds == 12
    audit = db.scalar(select(AuditLog).where(AuditLog.event == "PROVIDER_UPDATED"))
    assert audit and set(audit.detail["changed_fields"]) == {"enabled", "name", "priority", "timeout_seconds"}

    response = delete_provider(item.id, db)
    assert response.status_code == 204
    assert db.get(type(item), item.id) is None


def test_switching_provider_to_ollama_removes_cloud_credential(db):
    credential = _credential(db)
    item = create_provider(
        ProviderCreate(
            name="Switchable",
            provider_type="OPENAI_COMPATIBLE",
            base_url="https://example.com/v1",
            model="remote",
            credential_id=credential.id,
        ),
        db,
    )

    updated = update_provider(
        item.id,
        ProviderUpdate(provider_type="OLLAMA", base_url="http://127.0.0.1:11434", model="qwen3:8b"),
        db,
    )

    assert updated.provider_type == "OLLAMA"
    assert updated.credential_id is None


def test_qwen_is_a_first_class_encrypted_cloud_provider(db):
    credential = _credential(db)
    item = create_provider(
        ProviderCreate(
            name="Qwen Cloud",
            provider_type="QWEN",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="qwen-plus",
            credential_id=credential.id,
            priority=50,
        ),
        db,
    )

    assert item.provider_type == "QWEN"
    assert item.credential_id == credential.id


def test_kimi_is_a_first_class_encrypted_cloud_provider(db):
    credential = _credential(db)
    item = create_provider(
        ProviderCreate(
            name="Kimi Cloud",
            provider_type="KIMI",
            base_url="https://api.moonshot.cn/v1",
            model="kimi-k3",
            credential_id=credential.id,
            priority=40,
            timeout_seconds=120,
        ),
        db,
    )

    assert item.provider_type == "KIMI"
    assert item.model == "kimi-k3"
    assert item.credential_id == credential.id


def test_provider_capability_order_is_strongest_first_and_audited(db):
    providers = [
        ProviderConfig(name="Ollama Local", provider_type="OLLAMA", base_url="http://127.0.0.1:11434", model="llama3.1:8b", priority=1),
        ProviderConfig(name="Qwen Local", provider_type="OLLAMA", base_url="http://127.0.0.1:11434", model="qwen3.5:9b", priority=2),
        ProviderConfig(name="Qwen Cloud", provider_type="QWEN", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen-plus", priority=3),
        ProviderConfig(name="DeepSeek", provider_type="DEEPSEEK", base_url="https://api.deepseek.com", model="deepseek-v4-flash", priority=4),
        ProviderConfig(name="Kimi Cloud", provider_type="KIMI", base_url="https://api.moonshot.cn/v1", model="kimi-k3", priority=5),
    ]
    db.add_all(providers)
    db.commit()

    result = reorder_providers(ProviderReorder(strategy="CAPABILITY"), db)

    assert [item.model for item in result] == [
        "kimi-k3",
        "deepseek-v4-flash",
        "qwen-plus",
        "qwen3.5:9b",
        "llama3.1:8b",
    ]
    assert [item.priority for item in result] == [10, 20, 30, 40, 50]
    audit = db.scalar(select(AuditLog).where(AuditLog.event == "PROVIDERS_REORDERED"))
    assert audit is not None
    assert audit.detail["strategy"] == "CAPABILITY"
    assert [item["model"] for item in audit.detail["order"]] == [item.model for item in result]


def test_provider_manual_order_requires_and_applies_complete_queue(db):
    first = ProviderConfig(name="First", provider_type="OLLAMA", base_url="http://127.0.0.1:11434", model="first", priority=10)
    second = ProviderConfig(name="Second", provider_type="OLLAMA", base_url="http://127.0.0.1:11434", model="second", priority=20)
    db.add_all([first, second])
    db.commit()

    with pytest.raises(HTTPException, match="全部模型"):
        reorder_providers(ProviderReorder(strategy="MANUAL", ordered_ids=[first.id]), db)

    result = reorder_providers(
        ProviderReorder(strategy="MANUAL", ordered_ids=[second.id, first.id]),
        db,
    )
    assert [item.name for item in result] == ["Second", "First"]
    assert [item.priority for item in result] == [10, 20]


def test_persona_safety_preference_is_used_without_replacing_hard_policy():
    persona = PersonaProfile(
        id=1,
        global_persona="轻微猫咪感",
        user_style="简短",
        safety_policy="不要讨论用户自定义的敏感话题。",
    )
    contact = Contact(
        platform="SIMULATOR",
        platform_user_id="prompt-user",
        display_name="测试联系人",
        whitelisted=True,
    )

    prompt = build_messages(contact=contact, persona=persona, memories=[], recent=[], current_message="你好")
    system = prompt[0]["content"]

    assert SAFETY_POLICY in system
    assert "只能加强、不能覆盖" in system
    assert persona.safety_policy in system
    assert "所有自然语言回复必须使用简体中文" in system
