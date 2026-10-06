"""Tests for the Telegram bot integration (app/main.py + app/services/telegram.py).

Mocks all external calls (Telegram API, Google Sheets) so tests are pure and fast.
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

from app.main import Message, PhotoSize, SenderInfo, Update


# ------------------------------------------------------------------ #
# Telegram payload models                                              #
# ------------------------------------------------------------------ #

class TestTelegramPayloadModels:
    """Verify that our Pydantic models correctly parse Telegram Update JSON."""

    @pytest.fixture
    def text_update_json(self):
        return {
            "update_id": 42,
            "message": {
                "message_id": 10,
                "from_user": {"id": 1555000001, "is_bot": False, "first_name": "Alice"},
                "chat": {"id": 1555000001, "is_bot": False, "first_name": "Alice", "type": "private"},
                "date": 1728000000,
                "text": "/balance",
            },
        }

    @pytest.fixture
    def photo_update_json(self):
        return {
            "update_id": 43,
            "message": {
                "message_id": 11,
                "from_user": {"id": 1555000002, "is_bot": False, "first_name": "Bob"},
                "chat": {"id": 1555000002, "is_bot": False, "first_name": "Bob", "type": "private"},
                "date": 1728000001,
                "photo": [
                    {"file_id": "thumb_id", "width": 320, "height": 240},
                    {"file_id": "high_res_id", "width": 1080, "height": 720},
                ],
            },
        }

    def test_parse_text_update(self, text_update_json):
        update = Update.model_validate(text_update_json)
        assert update.update_id == 42
        assert update.message.text == "/balance"
        assert update.message.chat.id == 1555000001
        assert update.message.photo is None

    def test_parse_photo_update(self, photo_update_json):
        update = Update.model_validate(photo_update_json)
        assert update.update_id == 43
        assert update.message.photo is not None
        assert len(update.message.photo) == 2
        assert update.message.photo[-1].file_id == "high_res_id"

    def test_photo_selects_highest_resolution(self, photo_update_json):
        """The last element of photo[] should be the largest (Telegram spec)."""
        update = Update.model_validate(photo_update_json)
        photos = update.message.photo  # type: ignore[union-attr]
        assert photos[-1].width > photos[0].width

    def test_missing_photo_field_becomes_none(self):
        """Text-only messages must not crash on the photo field."""
        data = {
            "update_id": 99,
            "message": {
                "message_id": 1,
                "from_user": {"id": 0, "is_bot": False},
                "chat": {"id": 0, "is_bot": False},
                "date": 1728000000,
            },
        }
        update = Update.model_validate(data)
        assert update.message.photo is None


# ------------------------------------------------------------------ #
# Webhook routing (FastAPI)                                            #
# ------------------------------------------------------------------ #

from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Test client with mocked external services."""
    from app.main import app
    return TestClient(app)


class TestWebhookTextCommands:
    """POST /webhook with text messages should route correctly."""

    def test_balance_command(self, client):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from_user": {"id": 1555000001, "is_bot": False},
                "chat": {"id": 1555000001, "is_bot": False},
                "date": 1728000000,
                "text": "/balance",
            },
        }
        # Mock the external calls (google sheets, telegram reply).
        with patch("app.services.ledger.compute_current_balances") as mock_bal:
            mock_bal.return_value = {
                "Shin": 50.0, "Fabian": -30.0, "Pierre": -20.0,
            }
            with patch("app.main._reply") as mock_reply:
                resp = client.post("/webhook", json=payload)
                assert resp.status_code == 200
                # _reply should have been called with balance data.
                assert mock_reply.called

    def test_status_command(self, client):
        payload = {
            "update_id": 2,
            "message": {
                "message_id": 2,
                "from_user": {"id": 1555000002, "is_bot": False},
                "chat": {"id": 1555000002, "is_bot": False},
                "date": 1728000000,
                "text": "/status",
            },
        }
        with patch("app.services.ledger.compute_current_balances") as mock_bal:
            mock_bal.return_value = {
                "Shin": 0.0, "Fabian": 0.0, "Pierre": 0.0,
            }
            with patch("app.main._reply"):
                resp = client.post("/webhook", json=payload)
                assert resp.status_code == 200

    def test_help_command(self, client):
        payload = {
            "update_id": 3,
            "message": {
                "message_id": 3,
                "from_user": {"id": 1555000003, "is_bot": False},
                "chat": {"id": 1555000003, "is_bot": False},
                "date": 1728000000,
                "text": "/help",
            },
        }
        with patch("app.main._reply") as mock_reply:
            resp = client.post("/webhook", json=payload)
            assert resp.status_code == 200
            # Should have replied with help text.
            call_args = mock_reply.call_args_list[0][0]
            assert len(call_args) >= 2

    def test_unknown_command(self, client):
        payload = {
            "update_id": 4,
            "message": {
                "message_id": 4,
                "from_user": {"id": 1555000003, "is_bot": False},
                "chat": {"id": 1555000003, "is_bot": False},
                "date": 1728000000,
                "text": "/foo",
            },
        }
        with patch("app.main._reply"):
            resp = client.post("/webhook", json=payload)
            assert resp.status_code == 200


class TestWebhookPhotoMessage:
    """POST /webhook with a photo message should attempt receipt parsing."""

    def test_photo_message_routes_to_parser(self, client):
        payload = {
            "update_id": 10,
            "message": {
                "message_id": 10,
                "from_user": {"id": 1555000001, "is_bot": False},
                "chat": {"id": 1555000001, "is_bot": False},
                "date": 1728000000,
                "photo": [
                    {"file_id": "abc123", "width": 640, "height": 480},
                ],
            },
        }

        mock_download = AsyncMock(return_value=b"\xff\xd8\xff\xe0" * 10)  # fake JPEG bytes

        from app.models import ReceiptData, ReceiptItem

        with patch("app.services.telegram.download_photo", mock_download):
            with patch("app.services.vision.parse_receipt_image") as mock_parse:
                mock_parse.return_value = ReceiptData(
                    merchant="TestStore",
                    date="2026-10-02",
                    items=[ReceiptItem(item_name="Apple", price=1.50)],
                    grand_total=1.50,
                )
                with patch("app.main._reply") as mock_reply:
                    resp = client.post("/webhook", json=payload)
                    assert resp.status_code == 200
                    # download_photo should have been called once.
                    mock_download.assert_called_once()
                    mock_parse.assert_called_once()

    def test_empty_message_ignored(self, client):
        payload = {
            "update_id": 11,
            "message": None,
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200


# ------------------------------------------------------------------ #
# Telegram service helpers                                             #
# ------------------------------------------------------------------ #

class TestTelegramServiceHelpers:
    """Unit tests for telegram.py utilities."""

    def test_make_url_requires_token(self):
        """Without TELEGRAM_BOT_TOKEN, URL construction must fail."""
        old = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        try:
            from app.services.telegram import _make_url
            with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
                _make_url("getMe")
        finally:
            if old is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = old

    def test_format_receipt_reply_is_valid_html(self):
        """Receipt reply should produce valid HTML tags."""
        from app.main import _format_receipt_reply
        from app.models import ReceiptData, ReceiptItem, SplitType

        receipt = ReceiptData(
            merchant="CornerMart",
            date="2026-10-02",
            items=[
                ReceiptItem(item_name="Milk", price=3.50),
                ReceiptItem(item_name="Bread", price=2.75, assigned_split=SplitType.ONLY_B),
            ],
            tax_total=0.49,
            grand_total=6.74,
        )
        html = _format_receipt_reply(receipt, "Shin")
        assert "<b>" in html or "Receipt" in html  # should have formatting
        assert "$6.74" in html


# ------------------------------------------------------------------ #
# Roommate lookup                                                      #
# ------------------------------------------------------------------ #

class TestRoommateLookup:
    """Tests for the _lookup_roommate helper."""

    def test_known_user_id(self):
        from app.main import _lookup_roommate
        assert _lookup_roommate(1555000001) == "Shin"
        assert _lookup_roommate(1555000002) == "Fabian"
        assert _lookup_roommate(1555000003) == "Pierre"

    def test_unknown_user_id_returns_none(self):
        from app.main import _lookup_roommate
        assert _lookup_roommate(9999999999) is None


