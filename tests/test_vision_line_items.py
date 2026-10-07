import io
import json
from types import SimpleNamespace

from PIL import Image

from app.services import vision_line_items


def test_parse_uses_one_model_request(monkeypatch):
    prepare_calls = 0
    model_calls = []
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

    def fake_prepare(image_bytes):
        nonlocal prepare_calls
        prepare_calls += 1
        return [(b"prepared-image", "image/jpeg")]

    monkeypatch.setattr(vision_line_items, "_get_client", lambda: FakeClient())
    monkeypatch.setattr(vision_line_items, "_prepare_images", fake_prepare)
    monkeypatch.setattr(vision_line_items, "_VISION_MODEL", "test-model")

    receipt = vision_line_items.parse_itemized_receipt(b"receipt-image")

    assert receipt.items[0].name == "Initial parse name"
    assert prepare_calls == 1
    assert len(model_calls) == 1
    assert model_calls[0]["model"] == "test-model"


def test_tall_receipt_is_sent_as_three_detail_crops():
    source = io.BytesIO()
    Image.new("RGB", (100, 400), "white").save(source, format="JPEG")
    prepared = vision_line_items._prepare_images(source.getvalue())

    assert len(prepared) == 3

    for image_bytes, mime_type in prepared:
        assert mime_type == "image/jpeg"
        with Image.open(io.BytesIO(image_bytes)) as image:
            assert image.size == (100, 200)
