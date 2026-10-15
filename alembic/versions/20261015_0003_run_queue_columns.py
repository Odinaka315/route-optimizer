"""queue bookkeeping on optimization_runs for the separate worker

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "optimization_runs",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("optimization_runs", sa.Column("run_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("optimization_runs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("optimization_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("optimization_runs", sa.Column("locked_by", sa.String(length=100), nullable=True))
    op.create_index("ix_optimization_runs_status_created", "optimization_runs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_optimization_runs_status_created", table_name="optimization_runs")
    with op.batch_alter_table("optimization_runs") as batch:
        batch.drop_column("locked_by")
        batch.drop_column("heartbeat_at")
        batch.drop_column("started_at")
        batch.drop_column("run_after")
        batch.drop_column("attempts")
