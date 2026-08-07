from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Create pending_approvals table ---
    # Deliberately NOT immutable: no trigger applied here, unlike audit_logs
    # in 0001. This is a working queue -- rows are deleted once an action
    # resolves (approved/rejected/expired), at which point exactly one row
    # lands in audit_logs. See pending-approval-design.md section 2/3.
    op.create_table(
        "pending_approvals",
        sa.Column("action_id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False, index=True),
        sa.Column("request_json", postgresql.JSONB, nullable=False),
        sa.Column("decision_json", postgresql.JSONB, nullable=False),
        sa.Column("pending_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        comment="Mutable pending-approval queue -- NOT an audit record, rows are deleted on resolution",
    )


def downgrade() -> None:
    op.drop_table("pending_approvals")
