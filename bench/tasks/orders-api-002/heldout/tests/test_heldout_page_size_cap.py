from fastapi.testclient import TestClient

from app.main import create_app
from app.store import seeded_store


def test_page_size_is_capped():
    client = TestClient(create_app(seeded_store(250)))
    body = client.get("/orders", params={"page_size": 5000}).json()
    assert body["page_size"] == 100
    assert len(body["items"]) == 100 and body["pages"] == 3 and body["total"] == 250


def test_small_page_sizes_unchanged(client):
    assert client.get("/orders", params={"page_size": 7}).json()["page_size"] == 7
