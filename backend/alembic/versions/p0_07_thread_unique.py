"""Enforce durable one-content-per-workflow-thread idempotency."""

from alembic import op

revision = "p0_07_thread_unique"
down_revision = "p0_04_quality_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical duplicates are intentionally not deleted; resolve them before migration.
    op.create_unique_constraint("uq_content_item_thread_id", "content_item", ["thread_id"])


def downgrade() -> None:
    op.drop_constraint("uq_content_item_thread_id", "content_item", type_="unique")
