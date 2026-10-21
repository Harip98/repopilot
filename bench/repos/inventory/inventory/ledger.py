from collections import deque
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

CENT = Decimal("0.01")


class InsufficientStock(ValueError):
    pass


@dataclass
class Layer:
    """Units received together, at one cost."""

    qty: int
    unit_cost: Decimal
    received: date


class Ledger:
    def __init__(self) -> None:
        self._layers: dict[str, deque[Layer]] = {}
        self._cogs: dict[str, Decimal] = {}

    def receive(self, sku: str, qty: int, unit_cost: Decimal, at: date) -> None:
        if qty <= 0:
            raise ValueError("received quantity must be positive")
        if unit_cost < 0:
            raise ValueError("unit cost cannot be negative")
        self._layers.setdefault(sku, deque()).append(Layer(qty, Decimal(unit_cost), at))

    def ship(self, sku: str, qty: int, at: date) -> Decimal:
        """Remove `qty` units and return their cost."""
        if qty <= 0:
            raise ValueError("shipped quantity must be positive")
        available = self.on_hand(sku)
        if qty > available:
            raise InsufficientStock(f"{sku}: cannot ship {qty}, only {available} on hand")
        layers = self._layers[sku]
        remaining, cost = qty, Decimal("0")
        while remaining:
            layer = layers[0]
            take = min(layer.qty, remaining)
            cost += take * layer.unit_cost
            layer.qty -= take
            remaining -= take
            if layer.qty == 0:
                layers.popleft()
        self._cogs[sku] = self._cogs.get(sku, Decimal("0")) + cost
        return cost

    def on_hand(self, sku: str) -> int:
        return sum(layer.qty for layer in self._layers.get(sku, ()))

    def cogs(self, sku: str) -> Decimal:
        return self._cogs.get(sku, Decimal("0")).quantize(CENT)

    def valuation(self, sku: str | None = None) -> Decimal:
        """Value at cost of the units still on hand, for one SKU or for the whole warehouse."""
        skus = [sku] if sku is not None else list(self._layers)
        total = sum((layer.qty * layer.unit_cost for s in skus for layer in self._layers.get(s, ())),
                    Decimal("0"))
        return total.quantize(CENT)

    def skus(self) -> list[str]:
        return sorted(self._layers)
