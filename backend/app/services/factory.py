from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.base import ChannelConnector
from app.channels.experimental import AutoWxDraftConnector, NapCatConnector
from app.channels.qq import QQOfficialBotConnector
from app.channels.simulator import SimulatorConnector
from app.channels.wechat import WeChatDisabledConnector
from app.core.config import get_settings
from app.core.security import CredentialVault
from app.llm.base import LLMProvider
from app.llm.gateway import LLMGateway
from app.llm.providers import KimiProvider, OllamaProvider, OpenAICompatibleProvider, QwenProvider, SimulatorProvider, UnavailableProvider
from app.models.entities import Account, ApiCredential, ProviderConfig, RuntimeState
from app.services.account_selection import get_active_account
from app.services.pipeline import MessagePipeline
from app.services.provider_health import record_provider_failure, record_provider_success


simulator_connector = SimulatorConnector()
wechat_connector = WeChatDisabledConnector()


def build_providers(db: Session) -> list[LLMProvider]:
    settings = get_settings()
    state = db.get(RuntimeState, 1)
    configs = list(db.scalars(select(ProviderConfig).where(ProviderConfig.enabled.is_(True)).order_by(ProviderConfig.priority.asc())))
    providers: list[LLMProvider] = []
    vault = CredentialVault(settings)
    for config in configs:
        try:
            if config.provider_type == "OLLAMA":
                providers.append(OllamaProvider(base_url=config.base_url, model=config.model, timeout=config.timeout_seconds))
                continue
            credential = db.get(ApiCredential, config.credential_id) if config.credential_id else None
            if credential is None:
                continue
            provider_class = (
                QwenProvider
                if config.provider_type == "QWEN"
                else KimiProvider
                if config.provider_type == "KIMI"
                else OpenAICompatibleProvider
            )
            providers.append(
                provider_class(
                    name=config.name,
                    base_url=config.base_url,
                    model=config.model,
                    api_key=vault.decrypt(credential.encrypted_secret),
                    timeout=config.timeout_seconds,
                )
            )
        except Exception:
            # Keep the configured provider visible in the fallback trace. This
            # never exposes the secret and prevents an opaque "No providers".
            providers.append(UnavailableProvider(config.name, "本机凭据无法读取，请在后台重新填写一次"))
    if not providers and (state is None or state.release_gate == "SIMULATION"):
        providers.append(SimulatorProvider())
    return providers


def build_connectors(db: Session) -> dict[str, ChannelConnector]:
    settings = get_settings()
    connectors: dict[str, ChannelConnector] = {"SIMULATOR": simulator_connector, "WECHAT": wechat_connector}
    qq_account = db.scalar(select(Account).where(Account.platform == "QQ"))
    qq_secret = None
    if qq_account and qq_account.credential_id:
        credential = db.get(ApiCredential, qq_account.credential_id)
        if credential:
            try:
                qq_secret = CredentialVault(settings).decrypt(credential.encrypted_secret)
            except Exception:
                qq_secret = None
    connectors["QQ"] = QQOfficialBotConnector(
        app_id=(qq_account.config or {}).get("app_id") if qq_account else None,
        app_secret=qq_secret,
        enabled=bool(qq_account and qq_account.enabled),
        observed_status=qq_account.status if qq_account else "DISCONNECTED",
    )
    napcat_account = get_active_account(db, "QQ_NAPCAT")
    napcat_token = _account_secret(db, napcat_account, settings)
    connectors["QQ_NAPCAT"] = NapCatConnector(
        api_base=(napcat_account.config or {}).get("api_base") if napcat_account else None,
        access_token=napcat_token,
        enabled=bool(napcat_account and napcat_account.enabled),
        observed_status=napcat_account.status if napcat_account else "DISCONNECTED",
    )
    autowx_account = db.scalar(select(Account).where(Account.platform == "WECHAT_AUTOWX"))
    connectors["WECHAT_AUTOWX"] = AutoWxDraftConnector(
        enabled=bool(autowx_account and autowx_account.enabled),
        credential_present=bool(_account_secret(db, autowx_account, settings)),
        observed_status=autowx_account.status if autowx_account else "DISCONNECTED",
    )
    return connectors


def build_napcat_connector(db: Session, account: Account) -> NapCatConnector:
    """Build the connector for one stored NapCat profile.

    Profile switching currently permits exactly one enabled NapCat account, but
    keeping construction account-scoped makes later true multi-process support
    a data-compatible extension.
    """
    if account.platform != "QQ_NAPCAT":
        raise ValueError("account is not a NapCat profile")
    settings = get_settings()
    return NapCatConnector(
        api_base=(account.config or {}).get("api_base"),
        access_token=_account_secret(db, account, settings),
        enabled=bool(account.enabled),
        observed_status=account.status,
    )


def _account_secret(db: Session, account: Account | None, settings) -> str | None:
    if account is None or not account.credential_id:
        return None
    credential = db.get(ApiCredential, account.credential_id)
    if credential is None:
        return None
    try:
        return CredentialVault(settings).decrypt(credential.encrypted_secret)
    except Exception:
        return None


def build_pipeline(db: Session) -> MessagePipeline:
    settings = get_settings()

    async def on_error(provider_name: str, exc: Exception) -> None:
        record_provider_failure(db, provider_name, exc)

    async def on_success(response) -> None:
        record_provider_success(db, response)

    return MessagePipeline(
        gateway=LLMGateway(
            build_providers(db),
            timeout_seconds=settings.llm_timeout_seconds,
            on_error=on_error,
            on_success=on_success,
        ),
        connectors=build_connectors(db),
        settings=settings,
    )
