from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Create audit_logs table ---
    op.create_table(
        "audit_logs",
        sa.Column("audit_id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False, index=True),
        sa.Column("action_id", sa.String(32), nullable=False, unique=True),
        sa.Column("request_json", postgresql.JSONB, nullable=False),
        sa.Column("decision_json", postgresql.JSONB, nullable=False),
        sa.Column("execution_json", postgresql.JSONB, nullable=False),
        sa.Column("execution_status", sa.String(32), nullable=False),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.Column("policy_version", sa.String(32), server_default="policy-0.1"),
        sa.Column("detector_version", sa.String(32), server_default="detector-0.1"),
        sa.Column("latency", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        comment="Immutable audit log — UPDATE/DELETE blocked by DB trigger",
    )

    # --- Create immutable trigger function ---
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION prevent_audit_modification()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'Audit logs are immutable: UPDATE and DELETE are not allowed on the audit_logs table'
            USING HINT = 'Audit logs must never be modified or deleted for compliance and data integrity';
        END;
        $$ LANGUAGE plpgsql;
        """)
    )

    # --- Apply trigger to audit_logs table ---
    op.execute(
        sa.text("""
        CREATE TRIGGER trg_audit_logs_immutable
            BEFORE UPDATE OR DELETE ON audit_logs
            FOR EACH ROW
            EXECUTE FUNCTION prevent_audit_modification();
        """)
    )


def downgrade() -> None:
    # Remove trigger and function
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS prevent_audit_modification()"))

    # Drop table
    op.drop_table("audit_logs")
