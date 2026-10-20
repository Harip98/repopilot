import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import seeded_store


@pytest.fixture
def store():
    return seeded_store()


@pytest.fixture
def client(store):
    return TestClient(create_app(store))
