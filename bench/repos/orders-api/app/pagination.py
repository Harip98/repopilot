from collections.abc import Sequence

from app.models import Page

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class PaginationError(ValueError):
    pass


def page_count(total: int, page_size: int) -> int:
    """Number of pages needed to show `total` items, `page_size` at a time."""
    return (total + page_size - 1) // page_size


def clamp_page_size(page_size: int | None) -> int:
    if page_size is None:
        return DEFAULT_PAGE_SIZE
    if page_size < 1:
        raise PaginationError("page_size must be at least 1")
    return min(page_size, MAX_PAGE_SIZE)


def paginate[T](items: Sequence[T], page: int, page_size: int | None) -> Page[T]:
    if page < 1:
        raise PaginationError("page must be at least 1")
    size = clamp_page_size(page_size)
    start = (page - 1) * size
    return Page[T](
        items=list(items[start:start + size]),
        page=page,
        page_size=size,
        total=len(items),
        pages=page_count(len(items), size),
    )
