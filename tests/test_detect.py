"""Day 2 tests: JSON parsing, retry logic and /detect. No real network calls."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import vision
from app.config import Settings
from app.main import app
from app.schemas import DetectedShelf

client = TestClient(app)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SETTINGS = Settings(_env_file=None, openrouter_api_key="test-key", openrouter_model="test/model")

GOOD_REPLY = json.dumps(
    {
        "rows": [
            {
                "row": 1,
                "products": [
                    {"name": "Coca-Cola 500ml", "facings": 3},
                    {"name": "Pepsi 500ml", "facings": 2},
                ],
                "empty_gaps": 1,
                "misplaced_items": [],
            }
        ],
        "overall_notes": "",
    }
)


def script_replies(monkeypatch, replies):
    """Replace the OpenRouter call with one that returns the given replies in order."""
    calls = []

    async def fake_call(messages, settings):
        calls.append(list(messages))  # snapshot the conversation at this call
        return replies[len(calls) - 1]

    monkeypatch.setattr(vision, "_call_openrouter", fake_call)
    return calls


# ---- parse_shelf_json -------------------------------------------------------


def test_parse_accepts_fenced_json_with_surrounding_text():
    reply = f"Sure! Here is the result:\n```json\n{GOOD_REPLY}\n```\nHope that helps."
    shelf = vision.parse_shelf_json(reply)
    assert shelf.rows[0].products[0].facings == 3


def test_parse_rejects_reply_without_json():
    with pytest.raises(ValueError):
        vision.parse_shelf_json("I can see a shelf with some bottles.")


def test_parse_rejects_invalid_json():
    with pytest.raises(ValueError):
        vision.parse_shelf_json('{"rows": [')


def test_parse_rejects_schema_violation():
    bad = json.dumps({"rows": [{"row": 1, "products": [{"name": "X", "facings": -3}]}]})
    with pytest.raises(ValueError):
        vision.parse_shelf_json(bad)


# ---- detect_shelf retry logic -----------------------------------------------


def test_detect_succeeds_on_first_try(monkeypatch):
    calls = script_replies(monkeypatch, [GOOD_REPLY])
    shelf = asyncio.run(vision.detect_shelf(PNG_BYTES, "image/png", SETTINGS))
    assert isinstance(shelf, DetectedShelf)
    assert len(calls) == 1


def test_detect_retries_after_bad_json_then_succeeds(monkeypatch):
    calls = script_replies(monkeypatch, ["not json at all", GOOD_REPLY])
    shelf = asyncio.run(vision.detect_shelf(PNG_BYTES, "image/png", SETTINGS))
    assert shelf.rows[0].empty_gaps == 1
    assert len(calls) == 2
    # The second call carries the bad reply and a correction request.
    second = calls[1]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert "not valid" in second[2]["content"]


def test_detect_gives_up_after_max_attempts(monkeypatch):
    calls = script_replies(monkeypatch, ["bad", "still bad", "nope"])
    with pytest.raises(vision.InvalidModelOutputError):
        asyncio.run(vision.detect_shelf(PNG_BYTES, "image/png", SETTINGS))
    assert len(calls) == vision.MAX_ATTEMPTS


# ---- POST /detect ------------------------------------------------------------


def test_detect_endpoint_returns_structured_shelf(monkeypatch):
    async def fake_detect(image_bytes, mime_type):
        return vision.parse_shelf_json(GOOD_REPLY)

    monkeypatch.setattr(vision, "detect_shelf", fake_detect)
    response = client.post("/detect", files={"file": ("shelf.png", PNG_BYTES, "image/png")})
    assert response.status_code == 200
    body = response.json()
    assert body["shelf"]["rows"][0]["products"][1]["name"] == "Pepsi 500ml"


def test_detect_endpoint_returns_502_on_invalid_model_output(monkeypatch):
    async def failing_detect(image_bytes, mime_type):
        raise vision.InvalidModelOutputError("Model did not return valid shelf JSON")

    monkeypatch.setattr(vision, "detect_shelf", failing_detect)
    response = client.post("/detect", files={"file": ("shelf.png", PNG_BYTES, "image/png")})
    assert response.status_code == 502


def test_detect_endpoint_returns_500_when_key_missing(monkeypatch):
    async def no_key(image_bytes, mime_type):
        raise vision.MissingAPIKeyError("OPENROUTER_API_KEY is not set.")

    monkeypatch.setattr(vision, "detect_shelf", no_key)
    response = client.post("/detect", files={"file": ("shelf.png", PNG_BYTES, "image/png")})
    assert response.status_code == 500


def test_detect_endpoint_rejects_non_image():
    response = client.post("/detect", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert response.status_code == 400
