from datetime import date
from decimal import Decimal

import pytest

from inventory.ledger import Ledger


@pytest.fixture
def ledger() -> Ledger:
    """Widgets received twice at different costs; gadgets once."""
    ledger = Ledger()
    ledger.receive("WIDGET", 10, Decimal("2.00"), date(2026, 3, 1))
    ledger.receive("WIDGET", 10, Decimal("3.00"), date(2026, 3, 5))
    ledger.receive("GADGET", 4, Decimal("12.50"), date(2026, 3, 2))
    return ledger
