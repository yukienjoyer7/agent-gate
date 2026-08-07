from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Create pending_user_questions table ---
    # Mirrors pending_approvals (0002): deliberately mutable, no immutable
    # trigger -- a working queue for the ASK_USER decision path, not the
    # audit record. Rows are deleted once the end user responds (or the
    # question expires), at which point exactly one row lands in
    # audit_logs. See app/domains/clarification for the resolution logic.
    op.create_table(
        "pending_user_questions",
        sa.Column("action_id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False, index=True),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("request_json", postgresql.JSONB, nullable=False),
        sa.Column("decision_json", postgresql.JSONB, nullable=False),
        sa.Column("pending_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        comment="Mutable ask-user clarification queue -- NOT an audit record, rows deleted on resolution",
    )


def downgrade() -> None:
    op.drop_table("pending_user_questions")
