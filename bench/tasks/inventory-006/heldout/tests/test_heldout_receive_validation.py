from datetime import date
from decimal import Decimal

import pytest


def test_non_positive_receipts_rejected(ledger):
    for qty in (0, -5):
        with pytest.raises(ValueError):
            ledger.receive("WIDGET", qty, Decimal("2.00"), date(2026, 3, 9))
    assert ledger.on_hand("WIDGET") == 20
