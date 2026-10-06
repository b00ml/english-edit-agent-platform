"""Canonical task semantics, atomic active dedup and persistent admin model configuration."""

import sqlalchemy as sa

from alembic import op

revision = "opt076_states_models"
down_revision = "opt075_content_validation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(
        "UPDATE generation_task SET status='awaiting_review', progress=(SELECT count(*)::float/greatest(generation_task.quantity,1) FROM generation_task_item i WHERE i.task_id=generation_task.id AND i.status IN ('succeeded','stored','failed','rejected','cancelled')) WHERE status='partially_succeeded' AND EXISTS (SELECT 1 FROM generation_task_item i WHERE i.task_id=generation_task.id AND i.status='awaiting_review') AND NOT EXISTS (SELECT 1 FROM generation_task_item i WHERE i.task_id=generation_task.id AND i.status IN ('pending','queued','running'))"
    )
    duplicates = bind.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT request_hash FROM generation_task WHERE request_hash IS NOT NULL AND status IN ('pending','dispatched','running','awaiting_review') GROUP BY request_hash HAVING count(*)>1) q"
        )
    ).scalar()
    if duplicates:
        raise RuntimeError("存在重复活动请求，迁移停止；请人工核对，不自动取消或合并任务")
    op.execute(
        "UPDATE generation_task_item SET status=CASE status WHEN 'queued' THEN 'pending' WHEN 'stored' THEN 'succeeded' WHEN 'rejected' THEN 'failed' END WHERE status IN ('queued','stored','rejected')"
    )
    op.create_check_constraint(
        "ck_generation_task_status",
        "generation_task",
        "status IN ('pending','dispatched','running','awaiting_review','succeeded','partially_succeeded','failed','cancelled')",
    )
    op.create_check_constraint(
        "ck_generation_item_status",
        "generation_task_item",
        "status IN ('pending','running','awaiting_review','succeeded','failed','cancelled')",
    )
    op.create_index(
        "uq_active_generation_request",
        "generation_task",
        ["request_hash"],
        unique=True,
        postgresql_where=sa.text(
            "request_hash IS NOT NULL AND status IN ('pending','dispatched','running','awaiting_review')"
        ),
    )
    op.create_table(
        "model_provider",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(64), unique=True, nullable=False),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=False),
        sa.Column("key_revision", sa.String(36), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column("model_profile", sa.Column("provider_id", sa.String(36), nullable=True))
    op.add_column("model_profile", sa.Column("provider_config_hash", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_model_profile_provider", "model_profile", "model_provider", ["provider_id"], ["id"]
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_default_model_scope ON model_profile (coalesce(tenant_id, '')) WHERE is_default"
    )
    op.create_table(
        "model_route",
        sa.Column("template_id", sa.String(64), primary_key=True),
        sa.Column("generation_profile", sa.String(64), nullable=True),
        sa.Column("judge_profile", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_constraint("ck_generation_task_status", "generation_task", type_="check")
    op.drop_constraint("ck_generation_item_status", "generation_task_item", type_="check")
    op.drop_index("uq_default_model_scope", table_name="model_profile")
    op.drop_table("model_route")
    op.drop_constraint("fk_model_profile_provider", "model_profile", type_="foreignkey")
    op.drop_column("model_profile", "provider_config_hash")
    op.drop_column("model_profile", "provider_id")
    op.drop_table("model_provider")
    op.drop_index("uq_active_generation_request", table_name="generation_task")
