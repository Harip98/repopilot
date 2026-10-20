from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Lock

from app.models import LineItem, Order, OrderCreate, OrderStatus
from app.pricing import order_total, round_money, subtotal

CANCELLABLE = {OrderStatus.PENDING, OrderStatus.PAID}
SORT_KEYS = {"created_at", "total"}


class OrderNotFound(LookupError):
    pass


class InvalidTransition(ValueError):
    pass


class OrderStore:
    def __init__(self) -> None:
        self._orders: dict[int, Order] = {}
        self._idempotency: dict[str, int] = {}
        self._next_id = 1
        self._lock = Lock()

    def _build(self, data: OrderCreate, status: OrderStatus, created_at: datetime) -> Order:
        order = Order(
            id=self._next_id, customer=data.customer, status=status, items=data.items,
            discount_pct=data.discount_pct, created_at=created_at,
            subtotal=round_money(subtotal(data.items)),
            total=order_total(data.items, data.discount_pct),
        )
        self._orders[order.id] = order
        self._next_id += 1
        return order

    def create(self, data: OrderCreate, idempotency_key: str | None = None,
               now: datetime | None = None) -> tuple[Order, bool]:
        """Create an order. Returns (order, created); a repeated key returns the first order."""
        with self._lock:
            if idempotency_key is not None and idempotency_key in self._idempotency:
                return self._orders[self._idempotency[idempotency_key]], False
            order = self._build(data, OrderStatus.PENDING, now or datetime.now(UTC))
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = order.id
            return order, True

    def get(self, order_id: int) -> Order:
        try:
            return self._orders[order_id]
        except KeyError:
            raise OrderNotFound(order_id) from None

    def list(self, status: str | None = None, sort: str = "created_at") -> list[Order]:
        orders = list(self._orders.values())
        if status:
            wanted = status.lower()
            orders = [o for o in orders if o.status.value == wanted]
        descending = sort.startswith("-")
        key = sort.lstrip("-")
        if key not in SORT_KEYS:
            raise ValueError(f"cannot sort by {sort!r}")
        return sorted(orders, key=lambda o: (getattr(o, key), o.id), reverse=descending)

    def cancel(self, order_id: int) -> Order:
        with self._lock:
            order = self.get(order_id)
            if order.status not in CANCELLABLE:
                raise InvalidTransition(f"order {order_id} is {order.status.value} and cannot be cancelled")
            cancelled = order.model_copy(update={"status": OrderStatus.CANCELLED})
            self._orders[order_id] = cancelled
            return cancelled


def seeded_store(count: int = 57) -> OrderStore:
    """A deterministic store: `count` orders across every status, one hour apart."""
    store = OrderStore()
    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    statuses = list(OrderStatus)
    for i in range(count):
        items = [
            LineItem(sku=f"SKU-{i % 7}", quantity=1 + i % 3, unit_price=Decimal("9.99") + i),
            LineItem(sku="GIFT-WRAP", quantity=1, unit_price=Decimal("2.50"), cancelled=i % 5 == 0),
        ]
        data = OrderCreate(customer=f"customer-{i % 11}", items=items,
                           discount_pct=Decimal("10") if i % 4 == 0 else Decimal("0"))
        store._build(data, statuses[i % len(statuses)], start + timedelta(hours=i))
    return store
