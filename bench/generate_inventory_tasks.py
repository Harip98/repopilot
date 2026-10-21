"""Generate bench/tasks/inventory-NNN. Run: python bench/generate_inventory_tasks.py

Each task is an exact text replacement on the clean repo; see taskgen.py.
"""
from taskgen import write_tasks

T = {}

T["001"] = dict(
    title="Shipments are costed LIFO instead of FIFO",
    split="dev", heldout="fifo",
    expected_files=["inventory/ledger.py"],
    edits=[("inventory/ledger.py", "            layer = layers[0]\n", "            layer = layers[-1]\n"),
           ("inventory/ledger.py", "                layers.popleft()\n", "                layers.pop()\n")],
    issue="""\
        Finance says cost of goods sold is too high. Example: receive 10 widgets at $2.00 on
        1 March and 10 more at $3.00 on 5 March, then ship 5. `ship()` returns $15.00, but our
        costing policy is FIFO, so those 5 units cost $10.00 and the stock left is worth $40.00.
        """,
    test="""\
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
        """,
)

T["002"] = dict(
    title="Overselling crashes and wipes stock",
    split="dev", heldout="oversell",
    expected_files=["inventory/ledger.py"],
    edits=[("inventory/ledger.py",
            "        available = self.on_hand(sku)\n"
            "        if qty > available:\n"
            '            raise InsufficientStock(f"{sku}: cannot ship {qty}, only {available} on hand")\n',
            "")],
    issue="""\
        When an order asks for more than we have, `ship()` blows up with `IndexError: deque
        index out of range`, and afterwards the SKU shows 0 on hand even though nothing left
        the building. It should raise `InsufficientStock` and leave the stock exactly as it was.
        """,
    test="""\
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
        """,
)

T["003"] = dict(
    title="Reorder point rounds down",
    split="dev", heldout="reorder_rounding",
    expected_files=["inventory/reorder.py"],
    edits=[("inventory/reorder.py",
            "    return math.ceil(avg_daily_demand * (lead_time_days + safety_days))\n",
            "    return int(avg_daily_demand * (lead_time_days + safety_days))\n")],
    issue="""\
        We ran out of brackets again. Average demand is 2.5 a day with a 3-day lead time, so we
        need 7.5 units to cover the lead time, but `reorder_point(2.5, 3)` says 7. Partial
        units have to round up (8), otherwise we always reorder a little too late.
        """,
    test="""\
        from inventory.reorder import reorder_point


        def test_rounds_up():
            assert reorder_point(2.5, 3) == 8
            assert reorder_point(0.4, 7) == 3
            assert reorder_point(1.25, 2, safety_days=1) == 4


        def test_whole_numbers_unchanged():
            assert reorder_point(2, 3) == 6
        """,
)

T["004"] = dict(
    title="No reorder suggested exactly at the reorder point",
    split="dev", heldout="reorder_boundary",
    expected_files=["inventory/reorder.py"],
    edits=[("inventory/reorder.py",
            "    return on_hand <= reorder_point(avg_daily_demand, lead_time_days, safety_days)\n",
            "    return on_hand < reorder_point(avg_daily_demand, lead_time_days, safety_days)\n")],
    issue="""\
        With 6 units on hand, demand of 2 a day and a 3-day lead time, the reorder point is 6,
        yet `needs_reorder(6, 2, 3)` is False. The README says to reorder when stock is *at or
        below* the reorder point.
        """,
    test="""\
        from inventory.reorder import needs_reorder


        def test_reorder_at_the_point():
            assert needs_reorder(6, 2, 3)
            assert needs_reorder(10, 2, 3, safety_days=2)


        def test_above_the_point():
            assert not needs_reorder(7, 2, 3)
        """,
)

T["005"] = dict(
    title="Importing an export grouped by SKU fails",
    split="test", heldout="date_order",
    expected_files=["inventory/io.py"],
    edits=[("inventory/io.py", "    for tx in sorted(transactions, key=lambda t: t.on):\n",
            "    for tx in transactions:\n")],
    issue="""\
        The nightly import from the warehouse system fails with `InsufficientStock` on files
        like this one (the export is grouped by SKU, not sorted by date):

            date,sku,kind,qty,unit_cost
            2026-03-05,A,ship,5,
            2026-03-01,A,receive,10,1.00

        The shipment happened after the receipt, so this file is valid. Transactions must be
        applied in date order; rows from the same day keep their order in the file.
        """,
    test="""\
        from decimal import Decimal

        from inventory.io import apply_transactions, load_transactions
        from inventory.ledger import Ledger

        GROUPED = (
            "date,sku,kind,qty,unit_cost\\n"
            "2026-03-05,A,ship,5,\\n"
            "2026-03-01,A,receive,10,1.00\\n"
            "2026-03-04,B,ship,2,\\n"
            "2026-03-02,B,receive,3,4.00\\n"
            "2026-03-02,B,receive,1,5.00\\n"
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
            path.write_text("date,sku,kind,qty,unit_cost\\n"
                            "2026-03-02,C,receive,1,1.00\\n"
                            "2026-03-02,C,ship,1,\\n", encoding="utf-8")
            ledger = Ledger()
            apply_transactions(ledger, load_transactions(path))
            assert ledger.on_hand("C") == 0
        """,
)

T["006"] = dict(
    title="Negative receipts are accepted",
    split="test", heldout="receive_validation",
    expected_files=["inventory/ledger.py"],
    edits=[("inventory/ledger.py",
            "        if qty <= 0:\n"
            '            raise ValueError("received quantity must be positive")\n', "")],
    issue="""\
        A typo in the receiving screen booked a receipt of -5 widgets, and the system took it:
        stock on hand silently dropped by 5. Receipts of zero or negative quantity must be
        rejected with a ValueError and must not change stock.
        """,
    test="""\
        from datetime import date
        from decimal import Decimal

        import pytest


        def test_non_positive_receipts_rejected(ledger):
            for qty in (0, -5):
                with pytest.raises(ValueError):
                    ledger.receive("WIDGET", qty, Decimal("2.00"), date(2026, 3, 9))
            assert ledger.on_hand("WIDGET") == 20
        """,
)

T["007"] = dict(
    title="Stock report shows the warehouse total on every row",
    split="test", heldout="report_values",
    expected_files=["inventory/report.py"],
    edits=[("inventory/report.py", "            sku=sku, on_hand=on_hand, value=ledger.valuation(sku),\n",
            "            sku=sku, on_hand=on_hand, value=ledger.valuation(),\n")],
    issue="""\
        In the weekly stock report every SKU shows the same VALUE, and it is the value of the
        whole warehouse. Each row should show the value at cost of that SKU's units only.
        """,
    test="""\
        from datetime import date
        from decimal import Decimal

        from inventory.report import report


        def test_each_row_has_its_own_value(ledger):
            ledger.ship("GADGET", 2, date(2026, 3, 10))
            rows = report(ledger, {}, lead_time_days=3)
            assert [r.sku for r in rows] == ["GADGET", "WIDGET"]
            assert [r.value for r in rows] == [Decimal("25.00"), Decimal("50.00")]
        """,
)

write_tasks("inventory", T)
