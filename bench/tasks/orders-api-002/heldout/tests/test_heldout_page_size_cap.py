from app.pagination import MAX_PAGE_SIZE, paginate


def test_page_size_is_capped():
    page = paginate(list(range(250)), 1, 5000)
    assert page.page_size == MAX_PAGE_SIZE == 100
    assert len(page.items) == 100 and page.pages == 3


def test_api_caps_page_size(client):
    body = client.get("/orders", params={"page_size": 5000}).json()
    assert body["page_size"] == 100
