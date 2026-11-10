"""Tests for /score, /audit and the website pages. The vision model is faked."""

import json

from fastapi.testclient import TestClient

from app import vision
from app.main import app
from app.schemas import DetectedShelf

client = TestClient(app)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

PLANOGRAM = {
    "rows": [
        {"row": 1, "expected": [{"product": "Coca-Cola 500ml", "facings": 6}, {"product": "Pepsi 500ml", "facings": 4}]}
    ]
}
DETECTED = {
    "rows": [
        {"row": 1, "products": [{"name": "Coca-Cola 500ml", "facings": 6}, {"name": "Pepsi 500ml", "facings": 2}]}
    ]
}


def fake_detect(monkeypatch):
    async def fake(image_bytes, mime_type):
        return DetectedShelf.model_validate(DETECTED)

    monkeypatch.setattr(vision, "detect_shelf", fake)


def post_audit(path, planogram=None, filename="shelf.png", content=PNG_BYTES, ctype="image/png"):
    return client.post(
        path,
        files={"file": (filename, content, ctype)},
        data={"planogram": json.dumps(PLANOGRAM) if planogram is None else planogram},
    )


# ---- /score (no image, no model) ---------------------------------------------------


def test_score_endpoint_computes_audit_from_json():
    response = client.post("/score", json={"planogram": PLANOGRAM, "detected": DETECTED})
    assert response.status_code == 200
    body = response.json()
    assert body["score"] == 80.0
    assert body["gaps"][0]["missing"] == 2


def test_score_endpoint_rejects_bad_planogram():
    response = client.post("/score", json={"planogram": {"rows": []}, "detected": DETECTED})
    assert response.status_code == 422


# ---- /audit -------------------------------------------------------------------------


def test_audit_endpoint_returns_detected_and_audit(monkeypatch):
    fake_detect(monkeypatch)
    response = post_audit("/audit")
    assert response.status_code == 200
    body = response.json()
    assert body["audit"]["score"] == 80.0
    assert body["detected"]["rows"][0]["products"][0]["facings"] == 6


def test_audit_endpoint_rejects_invalid_planogram_json(monkeypatch):
    fake_detect(monkeypatch)
    response = post_audit("/audit", planogram="{not json")
    assert response.status_code == 400
    assert "Invalid planogram" in response.json()["detail"]


# ---- website ------------------------------------------------------------------------


def test_home_page_has_form_and_sample_planogram():
    response = client.get("/")
    assert response.status_code == 200
    assert 'name="file"' in response.text
    assert "Snacks-A" in response.text  # sample planogram is pre-filled


def test_audit_ui_shows_score_and_actions(monkeypatch):
    fake_detect(monkeypatch)
    response = post_audit("/audit-ui")
    assert response.status_code == 200
    assert "80.0%" in response.text
    assert "Restock 2 x Pepsi 500ml in row 1" in response.text


def test_audit_ui_shows_error_on_the_form_for_bad_planogram(monkeypatch):
    fake_detect(monkeypatch)
    response = post_audit("/audit-ui", planogram="{oops")
    assert response.status_code == 400
    assert "Invalid planogram" in response.text
    assert "{oops" in response.text  # what you typed is kept


def test_audit_ui_shows_error_for_non_image(monkeypatch):
    fake_detect(monkeypatch)
    response = post_audit("/audit-ui", filename="notes.txt", content=b"hello", ctype="text/plain")
    assert response.status_code == 400
    assert "Only JPEG, PNG or WEBP" in response.text


def test_audit_ui_shows_error_when_model_output_is_invalid(monkeypatch):
    async def failing(image_bytes, mime_type):
        raise vision.InvalidModelOutputError("Model did not return valid shelf JSON")

    monkeypatch.setattr(vision, "detect_shelf", failing)
    response = post_audit("/audit-ui")
    assert response.status_code == 502
    assert "Model did not return valid shelf JSON" in response.text


def test_stylesheet_is_served():
    response = client.get("/static/style.css")
    assert response.status_code == 200
    assert "conic-gradient" in response.text
