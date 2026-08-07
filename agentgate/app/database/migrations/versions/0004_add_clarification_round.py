from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Supports multi-round ASK_USER clarification: when a user's response
    # still doesn't clarify/complete the proposal enough to pass the
    # confidence threshold, the action goes back into the queue for another
    # round instead of failing closed after exactly one round. This counter
    # is what bounds that loop (see MAX_CLARIFICATION_ROUNDS in
    # app/domains/clarification/services/clarification_service.py).
    op.add_column(
        "pending_user_questions",
        sa.Column("clarification_round", sa.Integer, nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("pending_user_questions", "clarification_round")
