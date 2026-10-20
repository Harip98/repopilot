from datetime import date
from decimal import Decimal

import pytest

from inventory.ledger import Ledger


def test_on_hand(ledger):
    assert ledger.on_hand("WIDGET") == 20 and ledger.on_hand("GADGET") == 4
    assert ledger.on_hand("NOPE") == 0


def test_ship_from_single_layer(ledger):
    assert ledger.ship("GADGET", 3, date(2026, 3, 10)) == Decimal("37.50")
    assert ledger.on_hand("GADGET") == 1


def test_valuation_whole_warehouse(ledger):
    assert ledger.valuation() == Decimal("100.00")


def test_rejects_non_positive_shipment():
    with pytest.raises(ValueError):
        Ledger().ship("X", 0, date(2026, 1, 1))
