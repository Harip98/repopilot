import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from inventory.ledger import Ledger


@dataclass(frozen=True)
class Transaction:
    on: date
    sku: str
    kind: str  # "receive" or "ship"
    qty: int
    unit_cost: Decimal | None


def load_transactions(path: Path) -> list[Transaction]:
    """Read `date,sku,kind,qty,unit_cost` rows (ISO dates; unit_cost empty for shipments)."""
    with path.open(newline="", encoding="utf-8") as fh:
        return [
            Transaction(
                on=date.fromisoformat(row["date"]), sku=row["sku"], kind=row["kind"],
                qty=int(row["qty"]),
                unit_cost=Decimal(row["unit_cost"]) if row["unit_cost"] else None,
            )
            for row in csv.DictReader(fh)
        ]


def apply_transactions(ledger: Ledger, transactions: list[Transaction]) -> None:
    """Apply in date order; same-day rows keep their file order (sorted() is stable)."""
    for tx in sorted(transactions, key=lambda t: t.on):
        if tx.kind == "receive":
            ledger.receive(tx.sku, tx.qty, tx.unit_cost or Decimal("0"), tx.on)
        elif tx.kind == "ship":
            ledger.ship(tx.sku, tx.qty, tx.on)
        else:
            raise ValueError(f"unknown transaction kind {tx.kind!r}")
