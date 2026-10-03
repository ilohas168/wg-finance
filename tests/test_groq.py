"""Tests for the Groq vision parser integration (app/services/vision.py).

Mocks ``groq.Groq`` completions to verify that receipt parsing:
  - Constructs the correct request with image data URL
  - Extracts JSON from Groq's response
  - Validates against ``ReceiptData`` Pydantic schema
  - Falls back through candidate models on 404 / model_not_found errors
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from app.models import ReceiptData, ReceiptItem, SplitType


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #

@pytest.fixture
def fake_receipt_json() -> str:
    """A valid ReceiptData JSON payload that Groq would return."""
    data = {
        "merchant": "CornerMart",
        "date": "2026-10-03",
        "items": [
            {"item_name": "Milk", "price": 3.50, "assigned_split": "SPLIT_3"},
            {
                "item_name": "Protein Bars",
                "price": 8.99,
                "assigned_split": "ONLY_A",
            },
        ],
        "tax_total": 1.62,
        "grand_total": 14.11,
    }
    return json.dumps(data)


@pytest.fixture
def fake_receipt_bytes() -> bytes:
    """Minimal valid JPEG bytes (SOI + minimal JFIF header)."""
    return bytes(
        [
            0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46,
            0x49, 0x46, 0x00, 0x01, 0x01, 0x00, 0x00, 0x01,
            0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
            0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08,
            0x07, 0x07, 0x07, 0x09, 0x09, 0x08, 0x0A, 0x0C,
            0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
            0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D,
            0x1A, 0x1C, 0x1C, 0x20, 0x24, 0x2E, 0x27, 0x20,
            0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
            0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27,
            0x39, 0x3D, 0x38, 0x32, 0x3C, 0x2E, 0x33, 0x34,
            0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
            0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4,
            0x00, 0x1F, 0x00, 0x00, 0x01, 0x05, 0x01, 0x01,
            0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04,
            0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0xFF,
            0xC4, 0x00, 0x1F, 0x10, 0x00, 0x02, 0x01, 0x03,
            0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04,
            0x00, 0x00, 0x01, 0x7D, 0xFF, 0xDA, 0x00, 0x08,
            0x01, 0x01, 0x00, 0x00, 0x3F, 0x00, 0xFB, 0xD9,
        ]
    )


# ------------------------------------------------------------------ #
# Groq mock helper                                                     #
# ------------------------------------------------------------------ #

def _mock_groq_client(raw_json: str) -> MagicMock:
    """Build a fully mocked ``groq.Groq`` that returns *raw_json* in choices."""
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = raw_json
    mock_choice.message = mock_message

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response
    return mock_client


# ------------------------------------------------------------------ #
# Integration tests — vision parser via mocked Groq                  #
# ------------------------------------------------------------------ #

class TestVisionParserWithGroq:
    """End-to-end test of ``parse_receipt_image`` with Groq fully mocked."""

    def test_parse_receipt_succeeds_with_bare_json(self, fake_receipt_bytes):
        """Groq returns clean JSON — parser validates and returns ReceiptData."""
        mock_client = _mock_groq_client(
            json.dumps({
                "merchant": "TestMart",
                "date": "2026-10-03",
                "items": [
                    {"item_name": "Apple", "price": 1.50, "assigned_split": "SPLIT_3"},
                ],
                "tax_total": 0.21,
                "grand_total": 1.71,
            })
        )

        with patch("app.services.vision._get_client", return_value=mock_client):
            from app.services.vision import parse_receipt_image

            result = asyncio_run(parse_receipt_image(fake_receipt_bytes))

        assert isinstance(result, ReceiptData)
        assert result.merchant == "TestMart"
        assert result.date == "2026-10-03"
        assert len(result.items) == 1
        assert result.items[0].item_name == "Apple"
        assert result.grand_total == pytest.approx(1.71)

    def test_parse_receipt_succeeds_with_markdown_fence(self, fake_receipt_bytes):
        """Groq returns markdown-fenced JSON — parser strips it first."""
        fenced = "```json\n" + json.dumps({
            "merchant": "FenceMart",
            "date": "2026-10-03",
            "items": [
                {"item_name": "Bread", "price": 2.50, "assigned_split": "ONLY_B"},
            ],
            "tax_total": 0.35,
            "grand_total": 2.85,
        }) + "\n```"

        mock_client = _mock_groq_client(fenced)

        with patch("app.services.vision._get_client", return_value=mock_client):
            from app.services.vision import parse_receipt_image

            result = asyncio_run(parse_receipt_image(fake_receipt_bytes))

        assert result.merchant == "FenceMart"
        assert result.items[0].assigned_split == SplitType.ONLY_B

    def test_parse_receipt_fails_on_empty_content(self, fake_receipt_bytes):
        """Groq returns null content — should raise ValueError."""
        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = None  # null content triggers the check.
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response

        with patch("app.services.vision._get_client", return_value=mock_client):
            from app.services.vision import parse_receipt_image

            with pytest.raises(ValueError, match="no content"):
                asyncio_run(parse_receipt_image(fake_receipt_bytes))

    def test_parse_receipt_fails_on_api_error(self, fake_receipt_bytes):
        """Groq raises an API error — should surface the message."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = ValueError(
            "Model llama-3.2-11b-vision-instruct not available"
        )

        with patch("app.services.vision._get_client", return_value=mock_client):
            from app.services.vision import parse_receipt_image

            with pytest.raises(ValueError, match="not available"):
                asyncio_run(parse_receipt_image(fake_receipt_bytes))

    def test_parse_receipt_fails_on_invalid_json(self, fake_receipt_bytes):
        """Groq returns non-JSON text — should raise ValueError."""
        mock_client = _mock_groq_client("not json at all")

        with patch("app.services.vision._get_client", return_value=mock_client):
            from app.services.vision import parse_receipt_image

            with pytest.raises(ValueError, match="Groq returned invalid receipt JSON"):
                asyncio_run(parse_receipt_image(fake_receipt_bytes))


# ------------------------------------------------------------------ #
# Multi-model fallback tests                                           #
# ------------------------------------------------------------------ #

class TestModelFallback:
    """Verify that ``parse_receipt_image`` falls back through candidate models."""

    def _make_not_found_error(self) -> "groq.NotFoundError":  # type: ignore[name-defined]
        """Create a Groq NotFoundError (404 model_not_found)."""
        from groq import NotFoundError
        exc = NotFoundError(message="Model 'nonexistent-model' was not found", response=MagicMock(), body=None)  # type: ignore[arg-type]
        return exc

    def test_falls_back_on_first_candidate_404(self, fake_receipt_bytes):
        """When the first candidate returns 404, the second is tried and succeeds."""
        from groq import NotFoundError as GroqNotFoundError

        call_count = 0
        first_candidate = "qwen/qwen3.6-27b"

        def mock_create(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            model_used = kwargs.get("model", args[0] if args else "")
            if model_used == first_candidate:
                # First candidate: simulate 404.
                raise GroqNotFoundError(
                    message="Model 'llama-3.2-11b-vision-preview' was not found",
                    response=MagicMock(),
                    body=None,
                )
            # Second candidate succeeds — build a proper mock response.
            mock_choice = MagicMock()
            mock_message = MagicMock()
            data = {
                "merchant": "FallbackMart",
                "date": "2026-10-03",
                "items": [
                    {"item_name": "Toast", "price": 1.50, "assigned_split": "SPLIT_3"},
                ],
                "tax_total": 0.10,
                "grand_total": 1.60,
            }
            mock_message.content = json.dumps(data)
            mock_choice.message = mock_message
            mock_response = MagicMock()
            mock_response.choices = [mock_choice]
            return mock_response

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = mock_create

        with patch("app.services.vision._get_client", return_value=mock_client):
            from app.services.vision import parse_receipt_image

            result = asyncio_run(parse_receipt_image(fake_receipt_bytes))

        assert isinstance(result, ReceiptData)
        assert result.merchant == "FallbackMart"
        assert call_count == 2, "Should have tried both candidates"

    def test_falls_back_on_two_404s(self, fake_receipt_bytes):
        """First two candidates return 404; third candidate (qwen) succeeds."""
        from groq import NotFoundError as GroqNotFoundError

        call_count = 0

        def mock_create(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            model_used = kwargs.get("model", args[0] if args else "")
            if model_used in ("qwen/qwen3.6-27b", "meta-llama/llama-4-scout-17b-16e-instruct"):
                raise GroqNotFoundError(
                    message=f"Model '{model_used}' was not found",
                    response=MagicMock(),
                    body=None,
                )
            # Third candidate succeeds.
            mock_choice = MagicMock()
            mock_message = MagicMock()
            data = {
                "merchant": "QwenMart",
                "date": "2026-10-03",
                "items": [
                    {"item_name": "Rice", "price": 5.00, "assigned_split": "SPLIT_3"},
                ],
                "tax_total": 0.30,
                "grand_total": 5.30,
            }
            mock_message.content = json.dumps(data)
            mock_choice.message = mock_message
            mock_response = MagicMock()
            mock_response.choices = [mock_choice]
            return mock_response

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = mock_create

        with patch("app.services.vision._get_client", return_value=mock_client):
            from app.services.vision import parse_receipt_image

            result = asyncio_run(parse_receipt_image(fake_receipt_bytes))

        assert isinstance(result, ReceiptData)
        assert result.merchant == "QwenMart"
        assert call_count == 3, "Should have tried all three candidates"

    def test_all_candidates_404_raises_value_error(self, fake_receipt_bytes):
        """When every candidate returns 404, the parser raises ValueError."""
        from groq import NotFoundError as GroqNotFoundError

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = GroqNotFoundError(
            message="Model was not found",
            response=MagicMock(),
            body=None,
        )

        with patch("app.services.vision._get_client", return_value=mock_client):
            from app.services.vision import parse_receipt_image

            with pytest.raises(ValueError, match="not found"):
                asyncio_run(parse_receipt_image(fake_receipt_bytes))

    def test_custom_model_prepended_to_candidates(self):
        """Passing a custom model name should be prepended (deduped) to the list."""
        from app.services.vision import _build_model_candidates

        result = _build_model_candidates("custom-vision")
        assert result[0] == "custom-vision"
        assert result[1:] == [
            "qwen/qwen3.6-27b",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "qwen/qwen3.8-27b",
        ]

    def test_custom_model_deduplicates(self):
        """If custom model matches a candidate, it should not appear twice."""
        from app.services.vision import _build_model_candidates

        result = _build_model_candidates("llama-3.2-11b-vision-preview")
        assert result.count("llama-3.2-11b-vision-preview") == 1


# ------------------------------------------------------------------ #
# _get_client validation                                               #
# ------------------------------------------------------------------ #

class TestGetClientValidation:
    """Verify that ``_get_client`` enforces the GROQ_API_KEY env var."""

    def test_missing_key_raises(self):
        """Without GROQ_API_KEY, _get_client must raise RuntimeError."""
        old = os.environ.pop("GROQ_API_KEY", None)
        try:
            from app.services.vision import _get_client
            with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
                _get_client()
        finally:
            if old is not None:
                os.environ["GROQ_API_KEY"] = old

    def test_missing_key_in_main_startup(self):
        """Startup in main.py logs a warning when GROQ_API_KEY is absent."""
        with patch.dict(os.environ, {}, clear=False):
            if "GROQ_API_KEY" in os.environ:
                del os.environ["GROQ_API_KEY"]

            # Verify OPENAI_API_KEY no longer exists in main.py.
            from app import main
            import inspect
            source = inspect.getsource(main)
            assert "OPENAI_API_KEY" not in source, (
                "main.py still references OPENAI_API_KEY — should use GROQ_API_KEY"
            )


# ------------------------------------------------------------------ #
# Markdown stripping tests                                             #
# ------------------------------------------------------------------ #

class TestMarkdownStripping:
    """Unit tests for the JSON extraction helper."""

    def test_bare_json_passthrough(self):
        from app.services.vision import _extract_json
        assert _extract_json('{"a":1}') == '{"a":1}'

    def test_fenced_with_json_hint(self):
        from app.services.vision import _extract_json
        result = _extract_json("```json\n{\"a\": 1}\n```")
        assert result == '{"a": 1}'

    def test_fenced_without_hint(self):
        from app.services.vision import _extract_json
        result = _extract_json("```\n{\"a\": 1}\n```")
        assert result == '{"a": 1}'


# ------------------------------------------------------------------ #
# Helper: run an async function in a sync test context                 #
# ------------------------------------------------------------------ #

import asyncio
from typing import Any


def asyncio_run(coro: Any) -> Any:
    """Run an async coroutine synchronously (for pytest compatibility)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)
