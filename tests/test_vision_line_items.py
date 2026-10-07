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
