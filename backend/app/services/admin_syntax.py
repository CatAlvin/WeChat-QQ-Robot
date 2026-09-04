from __future__ import annotations


COMMAND_SEPARATOR = "-"
LEGACY_COMMAND_SEPARATOR = "|"


def split_command_payload(
    arguments: tuple[str, ...],
    *,
    required: bool,
    usage: str,
) -> tuple[str, str]:
    """Split a human-facing admin command at the first ASCII hyphen.

    Payloads may contain additional hyphens.  The former vertical-bar syntax is
    rejected with a focused migration hint so an old command cannot silently be
    interpreted as a contact name.
    """

    value = " ".join(arguments).strip()
    if LEGACY_COMMAND_SEPARATOR in value and COMMAND_SEPARATOR not in value:
        raise ValueError(f"命令分隔符已改为“-”。请使用：{usage}")
    if COMMAND_SEPARATOR not in value:
        if required:
            raise ValueError(f"格式：{usage}")
        return value, ""
    target, payload = value.split(COMMAND_SEPARATOR, 1)
    return target.strip(), payload.strip()
