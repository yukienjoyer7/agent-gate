import importlib
import unittest

from app.database.base import Base
from app.database.models.audit_log import AuditLog


class AuditModelRegistrationTest(unittest.TestCase):
    def test_audit_model_is_registered_with_alembic_metadata(self) -> None:
        import app.database.migrations.env as alembic_env

        importlib.reload(alembic_env)

        table_names = {table.name for table in Base.metadata.sorted_tables}
        self.assertIn("audit_logs", table_names)

        alembic_table_names = {table.name for table in alembic_env.Base.metadata.sorted_tables}
        self.assertIn("audit_logs", alembic_table_names)

        self.assertEqual(
            ["audit_id", "run_id", "action_id", "request_json", "decision_json", "execution_json", "execution_status", "error_type", "policy_version", "detector_version", "latency", "created_at"],
            list(AuditLog.__table__.columns.keys()),
        )


if __name__ == "__main__":
    unittest.main()
