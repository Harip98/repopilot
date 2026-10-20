from dataclasses import dataclass
from decimal import Decimal

from inventory.ledger import Ledger
from inventory.reorder import needs_reorder


@dataclass(frozen=True)
class ReportRow:
    sku: str
    on_hand: int
    value: Decimal
    reorder: bool


def report(ledger: Ledger, avg_daily_demand: dict[str, float], lead_time_days: int,
           safety_days: int = 0) -> list[ReportRow]:
    """One row per SKU, sorted by SKU."""
    rows = []
    for sku in ledger.skus():
        on_hand = ledger.on_hand(sku)
        rows.append(ReportRow(
            sku=sku, on_hand=on_hand, value=ledger.valuation(sku),
            reorder=needs_reorder(on_hand, avg_daily_demand.get(sku, 0.0), lead_time_days,
                                  safety_days),
        ))
    return rows


def render(rows: list[ReportRow]) -> str:
    lines = [f"{'SKU':<10}{'ON HAND':>9}{'VALUE':>12}  REORDER"]
    lines += [f"{r.sku:<10}{r.on_hand:>9}{r.value:>12}  {'yes' if r.reorder else ''}" for r in rows]
    return "\n".join(lines)
