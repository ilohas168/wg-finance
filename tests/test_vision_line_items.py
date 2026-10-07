import json
import threading
from types import SimpleNamespace

from app.services import vision_line_items


def test_parse_overlaps_name_ocr_and_keeps_matching_names(monkeypatch):
    name_ocr_started = threading.Event()
    prepare_calls = 0
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
                    assert name_ocr_started.wait(timeout=1), "name OCR did not start with the full parse"
                    return SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(content=json.dumps(payload))
                            )
                        ]
                    )

            completions = Completions()

        chat = Chat()

    def fake_names(model_name, image_content):
        name_ocr_started.set()
        return ["Printed OCR name"]

    def fake_prepare(image_bytes):
        nonlocal prepare_calls
        prepare_calls += 1
        return [(b"prepared-image", "image/jpeg")]

    monkeypatch.setattr(vision_line_items, "_get_client", lambda: FakeClient())
    monkeypatch.setattr(vision_line_items, "_prepare_images", fake_prepare)
    monkeypatch.setattr(vision_line_items, "_request_focused_names", fake_names)
    monkeypatch.setattr(vision_line_items, "_VISION_MODEL_CANDIDATES", ["test-model"])

    receipt = vision_line_items.parse_itemized_receipt(b"receipt-image")

    assert receipt.items[0].name == "Printed OCR name"
    assert prepare_calls == 1
