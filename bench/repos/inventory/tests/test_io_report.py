from decimal import Decimal

from inventory.io import apply_transactions, load_transactions
from inventory.ledger import Ledger
from inventory.report import render, report

CSV = """date,sku,kind,qty,unit_cost
2026-03-01,BOLT,receive,100,0.10
2026-03-03,BOLT,ship,30,
"""


def test_load_and_apply(tmp_path):
    path = tmp_path / "tx.csv"
    path.write_text(CSV, encoding="utf-8")
    ledger = Ledger()
    apply_transactions(ledger, load_transactions(path))
    assert ledger.on_hand("BOLT") == 70
    assert ledger.cogs("BOLT") == Decimal("3.00")


def test_report_single_sku(tmp_path):
    path = tmp_path / "tx.csv"
    path.write_text(CSV, encoding="utf-8")
    ledger = Ledger()
    apply_transactions(ledger, load_transactions(path))
    [row] = report(ledger, {"BOLT": 20.0}, lead_time_days=5)  # reorder point 100 >= 70
    assert (row.sku, row.on_hand, row.value, row.reorder) == ("BOLT", 70, Decimal("7.00"), True)
    assert "BOLT" in render([row])
