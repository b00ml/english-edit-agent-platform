"""Propagate known task/content tenants without assigning orphan knowledge owners."""

import sqlalchemy as sa

from alembic import op

revision = "p1_01_tenant_context"
down_revision = "p0_07_thread_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not op.get_context().as_sql:
        # Contradictory non-null owners need an audit; never blindly reassign private history.
        conflicts = [
            "SELECT count(*) FROM content_item c JOIN generation_task t ON c.task_id=t.id WHERE c.tenant_id IS NOT NULL AND c.tenant_id IS DISTINCT FROM t.tenant_id",
            "SELECT count(*) FROM trace_log l JOIN generation_task t ON l.task_id=t.id WHERE l.tenant_id IS NOT NULL AND l.tenant_id IS DISTINCT FROM t.tenant_id",
            "SELECT count(*) FROM quality_evaluation e JOIN generation_task t ON e.task_id=t.id WHERE e.tenant_id IS NOT NULL AND e.tenant_id IS DISTINCT FROM t.tenant_id",
            "SELECT count(*) FROM quality_record q JOIN content_item c ON q.item_id=c.id JOIN generation_task t ON c.task_id=t.id WHERE q.tenant_id IS NOT NULL AND q.tenant_id IS DISTINCT FROM t.tenant_id",
            "SELECT count(*) FROM sample_pool s JOIN content_item c ON s.item_id=c.id JOIN generation_task t ON c.task_id=t.id WHERE s.tenant_id IS NOT NULL AND s.tenant_id IS DISTINCT FROM t.tenant_id",
        ]
        if any(op.get_bind().execute(sa.text(sql)).scalar() for sql in conflicts):
            raise RuntimeError(
                "Conflicting historical tenant owners require manual audit before migration"
            )
    op.alter_column(
        "generation_task_item",
        "tenant_id",
        existing_type=sa.String(36),
        type_=sa.String(64),
        existing_nullable=False,
        nullable=True,
    )
    op.execute(
        "UPDATE generation_task_item i SET tenant_id = t.tenant_id FROM generation_task t WHERE i.task_id = t.id"
    )
    op.execute(
        "UPDATE content_item c SET tenant_id = t.tenant_id FROM generation_task t WHERE c.task_id = t.id AND c.tenant_id IS NULL"
    )
    op.execute(
        "UPDATE quality_record q SET tenant_id = c.tenant_id FROM content_item c WHERE q.item_id = c.id AND q.tenant_id IS NULL"
    )
    op.execute(
        "UPDATE trace_log l SET tenant_id = t.tenant_id FROM generation_task t WHERE l.task_id = t.id AND l.tenant_id IS NULL"
    )
    op.execute(
        "UPDATE quality_evaluation e SET tenant_id = t.tenant_id FROM generation_task t WHERE e.task_id = t.id AND e.tenant_id IS NULL"
    )
    op.execute(
        "UPDATE sample_pool s SET tenant_id = c.tenant_id FROM content_item c WHERE s.item_id = c.id AND s.tenant_id IS NULL"
    )


def downgrade() -> None:
    # Do not erase propagated owners. Refuse a lossy rollback for null or long tenants.
    connection = op.get_bind()
    invalid = connection.execute(
        sa.text(
            "SELECT count(*) FROM generation_task_item WHERE tenant_id IS NULL OR length(tenant_id)>36"
        )
    ).scalar()
    if invalid:
        raise RuntimeError(
            "Rollback requires explicit resolution of null/long item tenants; no data is auto-deleted"
        )
    op.alter_column(
        "generation_task_item",
        "tenant_id",
        existing_type=sa.String(64),
        type_=sa.String(36),
        existing_nullable=True,
        nullable=False,
    )
