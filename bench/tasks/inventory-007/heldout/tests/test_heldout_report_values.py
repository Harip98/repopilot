from datetime import date
from decimal import Decimal

from inventory.report import report


def test_each_row_has_its_own_value(ledger):
    ledger.ship("GADGET", 2, date(2026, 3, 10))
    rows = report(ledger, {}, lead_time_days=3)
    assert [r.sku for r in rows] == ["GADGET", "WIDGET"]
    assert [r.value for r in rows] == [Decimal("25.00"), Decimal("50.00")]
