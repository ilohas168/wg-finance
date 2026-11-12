import base64
import json
from types import SimpleNamespace

from app.services import vision_line_items


def test_parse_uses_one_model_request_with_original_photo(monkeypatch):
    model_calls = []
    original_image = b"\xff\xd8\xffuploaded-jpeg-bytes"
    payload = {
        "merchant": "Coop",
        "date": "2026-09-12",
        "total_amount": 3.5,
        "items": [
            {
                "name": "Initial parse name",
                "price": 3.5,
                "qty": 1,
                "category": "Food",
                "beneficiary": "ALL",
            }
        ],
    }

    class FakeClient:
        class Chat:
            class Completions:
                @staticmethod
                def create(**kwargs):
                    model_calls.append(kwargs)
                    return SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(content=json.dumps(payload))
                            )
                        ]
                    )

            completions = Completions()

        chat = Chat()

    monkeypatch.setattr(vision_line_items, "_get_client", lambda: FakeClient())
    monkeypatch.setattr(vision_line_items, "_VISION_MODEL", "test-model")

    receipt = vision_line_items.parse_itemized_receipt(original_image)

    assert receipt.items[0].name == "Initial parse name"
    assert len(model_calls) == 1
    assert model_calls[0]["model"] == "test-model"
    content = model_calls[0]["messages"][1]["content"]
    images = [block for block in content if block["type"] == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"] == (
        "data:image/jpeg;base64," + base64.b64encode(original_image).decode("ascii")
    )


def test_image_data_url_preserves_png_bytes():
    original_image = b"\x89PNG\r\n\x1a\noriginal-png-bytes"

    assert vision_line_items._image_data_url(original_image) == (
        "data:image/png;base64," + base64.b64encode(original_image).decode("ascii")
    )


import pytest
from datetime import date


@pytest.mark.parametrize(
    "printed, model_iso, expected",
    [
        # The reported bug: the model turned 7.11.2026 into July 11.
        ("7.11.2026", "2026-07-11", date(2026, 11, 7)),
        ("07.11.2026", "2026-11-07", date(2026, 11, 7)),
        ("12.9.26", "2026-12-09", date(2026, 9, 12)),
        ("Datum: 03/10/2026 14:22", "2026-03-10", date(2026, 10, 3)),
        ("31.10.2026", "2026-10-31", date(2026, 10, 31)),
        # Nothing printed: trust the model, unless its date is in the future
        # and swapping day/month gives a past date.
        ("", "2026-10-31", date(2026, 10, 31)),
        ("", "2026-12-07", date(2026, 7, 12)),
        ("", "2026-07-11", date(2026, 7, 11)),
        ("", "not a date", None),
    ],
)
def test_receipt_dates_are_read_day_first(printed, model_iso, expected):
    today = date(2026, 11, 7)
    assert vision_line_items.resolve_receipt_date(printed, model_iso, today) == expected


def test_parse_fixes_a_month_first_date_from_the_model(monkeypatch):
    payload = {
        "merchant": "Migros",
        "date": "2026-07-11",          # what the model wrongly returned
        "date_printed": "7.11.2026",   # what the receipt says
        "total_amount": 3.5,
        "items": [{"name": "Milch", "price": 3.5}],
    }

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    message = SimpleNamespace(content=json.dumps(payload))
                    return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(vision_line_items, "_get_client", lambda: FakeClient())

    receipt = vision_line_items.parse_itemized_receipt(b"\xff\xd8\xffjpeg")

    assert receipt.date == "2026-11-07"
