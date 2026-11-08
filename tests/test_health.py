"""Day 1 tests. No real network calls: the OpenRouter call is replaced with a fake."""

from fastapi.testclient import TestClient

from app import vision
from app.main import MAX_UPLOAD_BYTES, app

client = TestClient(app)

# Smallest bytes that pass our PNG signature check (not a viewable image, which is fine here).
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_describe_rejects_non_image():
    response = client.post(
        "/describe", files={"file": ("notes.txt", b"hello", "text/plain")}
    )
    assert response.status_code == 400


def test_describe_rejects_fake_image_content():
    # Claims to be a PNG but the bytes are plain text.
    response = client.post(
        "/describe", files={"file": ("fake.png", b"not really a png", "image/png")}
    )
    assert response.status_code == 400


def test_describe_rejects_oversized_image():
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_UPLOAD_BYTES
    response = client.post("/describe", files={"file": ("big.png", big, "image/png")})
    assert response.status_code == 400


def test_describe_returns_model_reply(monkeypatch):
    async def fake_describe(image_bytes, mime_type):
        assert mime_type == "image/png"
        return "Row 1: cola bottles, 2 gaps."

    monkeypatch.setattr(vision, "describe_shelf", fake_describe)
    response = client.post("/describe", files={"file": ("shelf.png", PNG_BYTES, "image/png")})
    assert response.status_code == 200
    assert response.json()["description"] == "Row 1: cola bottles, 2 gaps."


def test_describe_returns_502_when_openrouter_fails(monkeypatch):
    async def failing_describe(image_bytes, mime_type):
        raise vision.OpenRouterError("OpenRouter returned status 500")

    monkeypatch.setattr(vision, "describe_shelf", failing_describe)
    response = client.post("/describe", files={"file": ("shelf.png", PNG_BYTES, "image/png")})
    assert response.status_code == 502
