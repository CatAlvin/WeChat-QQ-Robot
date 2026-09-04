from __future__ import annotations

from sqlalchemy.exc import OperationalError

from scripts.mysql_acceptance import describe_operational_error


def make_operational_error(code: int) -> OperationalError:
    original = RuntimeError(code, "sensitive database detail")
    return OperationalError("statement", {}, original)


def test_mysql_permission_error_has_actionable_message_without_driver_detail() -> None:
    message = describe_operational_error(make_operational_error(1044))

    assert "权限不足" in message
    assert "管理员账号" in message
    assert "sensitive database detail" not in message


def test_unknown_mysql_error_does_not_echo_driver_detail() -> None:
    message = describe_operational_error(make_operational_error(9999))

    assert "9999" in message
    assert "sensitive database detail" not in message
