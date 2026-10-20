import pytest

from app.pagination import DEFAULT_PAGE_SIZE, PaginationError, paginate


def test_first_page():
    page = paginate(list(range(50)), 1, 10)
    assert page.items == list(range(10))
    assert (page.page, page.page_size, page.total, page.pages) == (1, 10, 50, 5)


def test_default_page_size():
    assert paginate(list(range(5)), 1, None).page_size == DEFAULT_PAGE_SIZE


def test_rejects_page_zero():
    with pytest.raises(PaginationError):
        paginate([1, 2, 3], 0, 10)


def test_rejects_non_positive_page_size():
    with pytest.raises(PaginationError):
        paginate([1, 2, 3], 1, 0)
