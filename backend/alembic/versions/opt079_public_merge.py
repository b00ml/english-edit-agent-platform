"""Join published user-reference branch with the local OPT077 migration history."""

revision = "opt079_public_merge"
down_revision = ("opt077_trace_samples", "m3_04_task_user_id")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
