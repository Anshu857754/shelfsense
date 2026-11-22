"""The stateless API from Day 1-3 keeps working (same endpoints, same response shapes)."""

import json

from app import vision
from app.main import MAX_UPLOAD_BYTES
from factories import item, reply, shelf_image

PLANOGRAM = {"rows": [{"row": 1, "expected": [{"product": "Coca-Cola 500ml", "facings": 6}, {"product": "Pepsi 500ml", "facings": 4}]}]}


def photo(name="shelf.jpg"):
    return {"file": (name, shelf_image(), "image/jpeg")}


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_describe_rejects_non_image(client):
    response = client.post("/describe", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert response.status_code == 400


def test_describe_rejects_fake_image_content(client):
    response = client.post("/describe", files={"file": ("fake.png", b"not really a png", "image/png")})
    assert response.status_code == 400


def test_describe_rejects_oversized_image(client):
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_UPLOAD_BYTES
    response = client.post("/describe", files={"file": ("big.png", big, "image/png")})
    assert response.status_code == 400 and "5 MB" in response.json()["detail"]


def test_describe_returns_model_reply(client, monkeypatch):
    async def fake_describe(image_bytes, mime_type):
        assert mime_type == "image/jpeg"
        return "Row 1: cola bottles, 2 gaps."

    monkeypatch.setattr(vision, "describe_shelf", fake_describe)
    response = client.post("/describe", files=photo())
    assert response.status_code == 200 and response.json()["description"] == "Row 1: cola bottles, 2 gaps."


def test_describe_returns_502_when_openrouter_fails(client, monkeypatch):
    async def failing(image_bytes, mime_type):
        raise vision.OpenRouterError("OpenRouter returned status 500")

    monkeypatch.setattr(vision, "describe_shelf", failing)
    assert client.post("/describe", files=photo()).status_code == 502


def test_detect_returns_the_structured_shelf(client, model):
    model.queue(reply((1, [item("Cola", 6, None, 0.9, "drinks", box={"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.2})])))
    body = client.post("/detect", files=photo()).json()
    product = body["shelf"]["rows"][0]["products"][0]
    assert product["facings"] == 6 and product["confidence"] == 0.9 and product["category"] == "drinks"
    assert product["box"]["w"] == 0.3


def test_detect_returns_502_when_the_answer_stays_invalid(client, model):
    model.queue("bad", "bad", "bad")
    assert client.post("/detect", files=photo()).status_code == 502


def test_detect_returns_500_when_the_key_is_missing(client, model):
    model.queue(vision.MissingAPIKeyError("OPENROUTER_API_KEY is not set."))
    assert client.post("/detect", files=photo()).status_code == 500


def test_score_endpoint_scores_hand_written_data(client):
    detected = {"rows": [{"row": 1, "products": [{"name": "Coca-Cola 500ml", "facings": 6}, {"name": "Pepsi 500ml", "facings": 2}]}]}
    body = client.post("/score", json={"planogram": PLANOGRAM, "detected": detected}).json()
    assert body["score"] == 80.0 and body["complete"] is True
    assert body["gaps"][0]["missing"] == 2 and body["tasks"][0]["priority"] == "high"


def test_score_with_no_detections_is_incomplete_not_zero(client):
    body = client.post("/score", json={"planogram": PLANOGRAM, "detected": {"rows": []}}).json()
    assert body["score"] is None and body["complete"] is False and body["gaps"] == []
    confirmed = client.post("/score", json={"planogram": PLANOGRAM, "detected": {"rows": []}, "confirmed_empty": True}).json()
    assert confirmed["score"] == 0 and confirmed["complete"] is True


def test_score_rejects_a_bad_planogram(client):
    assert client.post("/score", json={"planogram": {"rows": []}, "detected": {"rows": []}}).status_code == 422


def test_audit_endpoint_returns_detected_and_audit(client, model):
    model.queue(reply((1, [item("Cola bottles", 6, "Coca-Cola 500ml"), item("Pepsi cans", 2, "Pepsi 500ml")])))
    response = client.post("/audit", files=photo(), data={"planogram": json.dumps(PLANOGRAM), "category": "beverages"})
    body = response.json()
    assert response.status_code == 200 and body["audit"]["score"] == 80.0
    assert body["detected"]["rows"][0]["products"][0]["facings"] == 6
    assert "Beverages" in model.first_prompt and "Coca-Cola 500ml" in model.first_prompt


def test_audit_endpoint_rejects_bad_input(client, model):
    assert client.post("/audit", files=photo(), data={"planogram": "{nope"}).status_code == 400
    assert client.post("/audit", files=photo(), data={"planogram": json.dumps(PLANOGRAM), "category": "spaceships"}).status_code == 400
    assert model.calls == []
