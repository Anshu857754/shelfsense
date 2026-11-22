import pytest
from fastapi.testclient import TestClient

from app import db, vision
from app.main import app
from factories import FakeModel


@pytest.fixture(autouse=True)
def isolated_database(tmp_path):
    """Every test gets its own empty database and photo folder. Real data is never touched."""
    db.configure(tmp_path / "data")
    yield


@pytest.fixture
def model(monkeypatch) -> FakeModel:
    """Replace the network call to the AI with a scripted fake."""
    fake = FakeModel()
    monkeypatch.setattr(vision, "_call_openrouter", fake)
    return fake


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
