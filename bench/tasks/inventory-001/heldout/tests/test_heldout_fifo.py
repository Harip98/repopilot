from datetime import date
from decimal import Decimal


def test_ship_consumes_oldest_first(ledger):
    assert ledger.ship("WIDGET", 5, date(2026, 3, 10)) == Decimal("10.00")
    assert ledger.valuation("WIDGET") == Decimal("40.00")


def test_ship_across_layers(ledger):
    ledger.ship("WIDGET", 5, date(2026, 3, 10))
    assert ledger.ship("WIDGET", 12, date(2026, 3, 11)) == Decimal("31.00")
    assert ledger.on_hand("WIDGET") == 3
    assert ledger.cogs("WIDGET") == Decimal("41.00")
