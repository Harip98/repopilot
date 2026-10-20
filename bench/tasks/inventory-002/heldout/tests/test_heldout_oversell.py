from datetime import date
from decimal import Decimal

import pytest

from inventory.ledger import InsufficientStock


def test_oversell_is_refused_without_side_effects(ledger):
    with pytest.raises(InsufficientStock):
        ledger.ship("WIDGET", 30, date(2026, 3, 10))
    assert ledger.on_hand("WIDGET") == 20
    assert ledger.cogs("WIDGET") == Decimal("0.00")


def test_unknown_sku_is_insufficient(ledger):
    with pytest.raises(InsufficientStock):
        ledger.ship("NOPE", 1, date(2026, 3, 10))
