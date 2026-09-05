from app.database.base import Base
from app.database.models.telegram_contact import TelegramContact


def test_telegram_contact_model_has_no_credential_column() -> None:
    columns = list(TelegramContact.__table__.columns.keys())

    assert columns == [
        "id",
        "chat_id",
        "chat_type",
        "username",
        "first_name",
        "last_name",
        "display_name",
        "is_active",
        "first_seen_at",
        "last_seen_at",
    ]
    assert "telegram_contacts" in Base.metadata.tables
    assert not any("token" in column.lower() for column in columns)
    assert TelegramContact.__table__.c.chat_id.unique is True
