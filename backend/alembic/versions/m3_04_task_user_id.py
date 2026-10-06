"""Add creator user reference to generation tasks."""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "m3_04_task_user_id"
down_revision: Union[str, None] = "m3_03_model_hash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "generation_task",
        sa.Column("user_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_generation_task_user_id_users",
        "generation_task",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_generation_task_user_id", "generation_task", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_generation_task_user_id", table_name="generation_task")
    op.drop_constraint("fk_generation_task_user_id_users", "generation_task", type_="foreignkey")
    op.drop_column("generation_task", "user_id")
