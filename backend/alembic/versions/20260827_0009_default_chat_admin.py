"""Mark the requested NapCat contact as the default chat administrator.

Revision ID: 20260827_0009
Revises: 20260826_0008
Create Date: 2026-08-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260827_0009"
down_revision = "20260826_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE contacts SET relationship_label = '管理员', whitelisted = :enabled, ai_enabled = :enabled "
            "WHERE platform = 'QQ_NAPCAT' AND platform_user_id = '1032556054'"
        ).bindparams(enabled=True)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE contacts SET relationship_label = '朋友' "
            "WHERE platform = 'QQ_NAPCAT' AND platform_user_id = '1032556054' AND relationship_label = '管理员'"
        )
    )
