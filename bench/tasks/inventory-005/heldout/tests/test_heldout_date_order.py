from decimal import Decimal

from inventory.io import apply_transactions, load_transactions
from inventory.ledger import Ledger

GROUPED = (
    "date,sku,kind,qty,unit_cost\n"
    "2026-03-05,A,ship,5,\n"
    "2026-03-01,A,receive,10,1.00\n"
    "2026-03-04,B,ship,2,\n"
    "2026-03-02,B,receive,3,4.00\n"
    "2026-03-02,B,receive,1,5.00\n"
)


def test_applied_in_date_order(tmp_path):
    path = tmp_path / "tx.csv"
    path.write_text(GROUPED, encoding="utf-8")
    ledger = Ledger()
    apply_transactions(ledger, load_transactions(path))
    assert ledger.on_hand("A") == 5 and ledger.cogs("A") == Decimal("5.00")
    assert ledger.on_hand("B") == 2 and ledger.cogs("B") == Decimal("8.00")


def test_same_day_rows_keep_file_order(tmp_path):
    path = tmp_path / "tx.csv"
    path.write_text("date,sku,kind,qty,unit_cost\n"
                    "2026-03-02,C,receive,1,1.00\n"
                    "2026-03-02,C,ship,1,\n", encoding="utf-8")
    ledger = Ledger()
    apply_transactions(ledger, load_transactions(path))
    assert ledger.on_hand("C") == 0
