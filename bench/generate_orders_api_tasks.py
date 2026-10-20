"""Generate bench/tasks/orders-api-NNN from exact text replacements on the clean repo.

Run: python bench/generate_orders_api_tasks.py   (then: repopilot bench validate)

bug.patch = clean -> buggy diff, fix.patch = its reverse (the minimal reference fix).
"""
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
REPO = ROOT / "bench" / "repos" / "orders-api"
TASKS = ROOT / "bench" / "tasks"
GIT = ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf"]
# Held-out test names plus the test infrastructure that decides how pytest runs them.
PROTECTED = ('["tests/test_heldout_*", "conftest.py", "*/conftest.py", "pyproject.toml", '
             '"pytest.ini", "setup.cfg", "tox.ini"]')

T = {}

T["001"] = dict(
    title="Order list never shows the last partial page",
    difficulty="simple", split="dev", heldout="pagination_pages",
    expected_files=["app/pagination.py"],
    edits=[("app/pagination.py", "    return (total + page_size - 1) // page_size\n",
            "    return total // page_size\n")],
    issue="""\
        The storefront order history stops early. We have 57 orders and request
        `GET /orders?page_size=20`; the response says `"pages": 2`, so the UI only offers pages
        1 and 2 and the last 17 orders are never reachable from the pager.

        Expected `pages` to be 3 here. Probably started after last week's pagination cleanup.
        """,
    test="""\
        def test_partial_last_page_is_counted(client):
            assert client.get("/orders", params={"page_size": 20}).json()["pages"] == 3
            assert client.get("/orders", params={"page_size": 19}).json()["pages"] == 3
            assert client.get("/orders", params={"page_size": 100}).json()["pages"] == 1


        def test_last_page_is_reachable(client):
            body = client.get("/orders", params={"page": 3, "page_size": 20}).json()
            assert len(body["items"]) == 17
            assert [o["id"] for o in body["items"]] == list(range(41, 58))
        """,
)

T["002"] = dict(
    title="page_size above 100 is not capped",
    difficulty="simple", split="dev", heldout="page_size_cap",
    expected_files=["app/pagination.py"],
    edits=[("app/pagination.py", "    return min(page_size, MAX_PAGE_SIZE)\n",
            "    return page_size\n")],
    issue="""\
        The README says `page_size` is capped at 100, but `GET /orders?page_size=5000` happily
        returns everything in one response and echoes `"page_size": 5000`. One integration is
        now pulling the whole table every minute.

        Requests above the maximum should be served with `page_size` 100.
        """,
    test="""\
        from fastapi.testclient import TestClient

        from app.main import create_app
        from app.store import seeded_store


        def test_page_size_is_capped():
            client = TestClient(create_app(seeded_store(250)))
            body = client.get("/orders", params={"page_size": 5000}).json()
            assert body["page_size"] == 100
            assert len(body["items"]) == 100 and body["pages"] == 3 and body["total"] == 250


        def test_small_page_sizes_unchanged(client):
            assert client.get("/orders", params={"page_size": 7}).json()["page_size"] == 7
        """,
)

T["003"] = dict(
    title="sort=-created_at returns oldest orders first",
    difficulty="simple", split="dev", heldout="sort_direction",
    expected_files=["app/store.py"],
    edits=[("app/store.py", '        descending = sort.startswith("-")\n',
            '        descending = sort.endswith("-")\n')],
    issue="""\
        `GET /orders?sort=-created_at` should list the newest orders first (that is what the
        leading `-` means everywhere else in our APIs), but it returns them oldest first, exactly
        like `sort=created_at`. `sort=-total` has the same problem.
        """,
    test="""\
        def _first_page(client, sort):
            return client.get("/orders", params={"sort": sort, "page_size": 100}).json()["items"]


        def test_descending_created_at(client):
            stamps = [o["created_at"] for o in _first_page(client, "-created_at")]
            assert stamps == sorted(stamps, reverse=True)


        def test_descending_total(client):
            totals = [float(o["total"]) for o in _first_page(client, "-total")]
            assert totals == sorted(totals, reverse=True)


        def test_ascending_still_works(client):
            stamps = [o["created_at"] for o in _first_page(client, "created_at")]
            assert stamps == sorted(stamps)
        """,
)

T["004"] = dict(
    title="Percentage discounts are taken off as dollars",
    difficulty="simple", split="dev", heldout="discount",
    expected_files=["app/pricing.py"],
    edits=[("app/pricing.py",
            '    return amount * (Decimal("100") - discount_pct) / Decimal("100")\n',
            "    return amount - discount_pct\n")],
    issue="""\
        Customers with a 10% coupon are complaining. A $200.00 order with `discount_pct: 10`
        comes out at $205.68 instead of $194.85, i.e. only $10 was taken off before tax. On a
        $5.00 order the same coupon makes the total negative.

        `discount_pct` is a percentage (0-100), applied before tax.
        """,
    test="""\
        from decimal import Decimal

        from app.models import LineItem
        from app.pricing import order_total


        def test_api_applies_percentage_discount(client):
            order = client.post("/orders", json={
                "customer": "c", "discount_pct": "10",
                "items": [{"sku": "A", "quantity": 2, "unit_price": "100.00"}],
            }).json()
            assert order["total"] == "194.85"


        def test_totals_with_discount():
            big = [LineItem(sku="A", quantity=1, unit_price=Decimal("200.00"))]
            small = [LineItem(sku="B", quantity=1, unit_price=Decimal("5.00"))]
            assert order_total(big, Decimal("10")) == Decimal("194.85")
            assert order_total(small, Decimal("10")) == Decimal("4.87")
        """,
)

T["005"] = dict(
    title="Status filter only matches lowercase values",
    difficulty="simple", split="dev", heldout="status_case",
    expected_files=["app/store.py"],
    edits=[("app/store.py", "            wanted = status.lower()\n", "            wanted = status\n")],
    issue="""\
        The admin dashboard sends `GET /orders?status=PAID` and gets `"total": 0`, while
        `status=paid` returns plenty of orders. Status values should be matched
        case-insensitively; this used to work.
        """,
    test="""\
        def _total(client, status):
            return client.get("/orders", params={"status": status}).json()["total"]


        def test_status_filter_ignores_case(client):
            lower = _total(client, "paid")
            assert lower > 0
            assert _total(client, "PAID") == lower
            assert _total(client, "Paid") == lower
        """,
)

T["006"] = dict(
    title="Shipped orders can be cancelled",
    difficulty="simple", split="dev", heldout="cancel_rules",
    expected_files=["app/store.py"],
    edits=[("app/store.py", "CANCELLABLE = {OrderStatus.PENDING, OrderStatus.PAID}\n",
            "CANCELLABLE = {OrderStatus.PENDING, OrderStatus.PAID, OrderStatus.SHIPPED}\n")],
    issue="""\
        The warehouse reports orders flipping to `cancelled` after they have already left the
        building. `POST /orders/{id}/cancel` on a shipped order returns 200. Per the README only
        orders that have not shipped can be cancelled; shipped and delivered orders should get a
        409 and stay unchanged.
        """,
    test="""\
        def _first_with_status(client, status):
            return client.get("/orders", params={"status": status}).json()["items"][0]["id"]


        def test_cannot_cancel_shipped(client):
            order_id = _first_with_status(client, "shipped")
            assert client.post(f"/orders/{order_id}/cancel").status_code == 409
            assert client.get(f"/orders/{order_id}").json()["status"] == "shipped"


        def test_cannot_cancel_delivered(client):
            order_id = _first_with_status(client, "delivered")
            assert client.post(f"/orders/{order_id}/cancel").status_code == 409


        def test_can_cancel_paid(client):
            order_id = _first_with_status(client, "paid")
            assert client.post(f"/orders/{order_id}/cancel").status_code == 200
        """,
)

T["007"] = dict(
    title="Some order totals are one cent low",
    difficulty="complex", split="dev", heldout="money_rounding",
    expected_files=["app/pricing.py"],
    edits=[("app/pricing.py", '    return amount * (Decimal("1") + TAX_RATE)\n',
            "    return Decimal(float(amount) * (1 + float(TAX_RATE)))\n")],
    issue="""\
        Finance found orders whose total is one cent lower than our invoicing system. Two
        examples, no discount:

        - one item at $10.00: invoicing says $10.83, the API says $10.82
        - one item at $18.00: invoicing says $19.49, the API says $19.48

        Most orders match. Totals should be exact and rounded half-up to the cent.
        """,
    test="""\
        from decimal import Decimal

        from app.models import LineItem
        from app.pricing import order_total


        def _total(price):
            return order_total([LineItem(sku="X", quantity=1, unit_price=Decimal(price))], Decimal("0"))


        def test_half_cent_totals_round_up():
            assert _total("10.00") == Decimal("10.83")
            assert _total("18.00") == Decimal("19.49")
            assert _total("2.00") == Decimal("2.17")


        def test_api_total(client):
            resp = client.post("/orders", json={
                "customer": "fin", "items": [{"sku": "X", "quantity": 1, "unit_price": "10.00"}],
            })
            assert resp.json()["total"] == "10.83"
        """,
)

T["008"] = dict(
    title="Requesting a page past the end returns 500",
    difficulty="complex", split="test", heldout="out_of_range",
    expected_files=["app/pagination.py"],
    edits=[("app/pagination.py", "    start = (page - 1) * size\n",
            "    pages = page_count(len(items), size)\n"
            "    if page > pages:\n"
            "        raise IndexError(f\"page {page} is out of range\")\n"
            "    start = (page - 1) * size\n")],
    issue="""\
        Two crashes in the order list, both `500 Internal Server Error`:

        1. `GET /orders?page=99` (the pager remembers the page across filter changes)
        2. `GET /orders?status=refunded` - no order has that status, and the very first page
           already fails

        A page past the end should come back as `200` with an empty `items` list and the real
        `total` / `pages`, so the UI can show "no results" or jump back.
        """,
    test="""\
        def test_page_past_end_is_empty(client):
            resp = client.get("/orders", params={"page": 99, "page_size": 20})
            assert resp.status_code == 200
            body = resp.json()
            assert body["items"] == [] and body["total"] == 57 and body["pages"] == 3


        def test_no_matches_is_empty_first_page(client):
            resp = client.get("/orders", params={"status": "refunded"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["items"] == [] and body["total"] == 0 and body["pages"] == 0
        """,
)

T["009"] = dict(
    title="Order subtotal includes cancelled line items",
    difficulty="complex", split="test", heldout="cancelled_lines",
    expected_files=["app/store.py"],
    edits=[("app/store.py", "            subtotal=round_money(subtotal(data.items)),\n",
            "            subtotal=round_money(sum((i.unit_price * i.quantity for i in data.items), Decimal(\"0\"))),\n")],
    issue="""\
        Customers who remove gift wrap from an order still see it in the order summary. Order 1
        has a cancelled GIFT-WRAP line ($2.50), yet `GET /orders/1` shows `"subtotal": "12.49"`.
        The `total` looks right, so the two fields disagree. Cancelled lines must not count
        towards the subtotal anywhere.
        """,
    test="""\
        from decimal import Decimal


        def test_subtotal_excludes_cancelled_lines(client):
            order = client.get("/orders/1").json()
            assert order["subtotal"] == "9.99"


        def test_created_order_subtotal(client):
            order = client.post("/orders", json={"customer": "c", "items": [
                {"sku": "A", "quantity": 2, "unit_price": "10.00"},
                {"sku": "W", "quantity": 1, "unit_price": "2.50", "cancelled": True},
            ]}).json()
            assert order["subtotal"] == "20.00"
            assert Decimal(order["total"]) == Decimal("21.65")
        """,
)

T["010"] = dict(
    title="Retried POST /orders with the same Idempotency-Key creates a duplicate",
    difficulty="complex", split="test", heldout="idempotency",
    expected_files=["app/store.py"],
    edits=[("app/store.py",
            "            if idempotency_key is not None:\n"
            "                self._idempotency[idempotency_key] = order.id\n", "")],
    issue="""\
        The mobile app retries `POST /orders` with the same `Idempotency-Key` header when a
        request times out. Since the last release some customers end up with two identical
        orders. A repeated key should return the original order (status 200) and must not create
        a new one.
        """,
    test="""\
        PAYLOAD = {"customer": "mobile", "items": [{"sku": "A", "quantity": 1, "unit_price": "3.00"}]}


        def test_same_key_returns_original(client):
            first = client.post("/orders", json=PAYLOAD, headers={"Idempotency-Key": "k-1"})
            second = client.post("/orders", json=PAYLOAD, headers={"Idempotency-Key": "k-1"})
            assert first.status_code == 201 and second.status_code == 200
            assert second.json()["id"] == first.json()["id"]


        def test_no_duplicate_order(client):
            before = client.get("/orders").json()["total"]
            for _ in range(3):
                client.post("/orders", json=PAYLOAD, headers={"Idempotency-Key": "k-2"})
            assert client.get("/orders").json()["total"] == before + 1


        def test_different_keys_create_orders(client):
            a = client.post("/orders", json=PAYLOAD, headers={"Idempotency-Key": "a"}).json()
            b = client.post("/orders", json=PAYLOAD, headers={"Idempotency-Key": "b"}).json()
            assert a["id"] != b["id"]
        """,
)


def diff_for(edits) -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        shutil.copytree(REPO, ws, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        subprocess.run([*GIT, "init", "-q"], cwd=ws, check=True)
        subprocess.run([*GIT, "add", "-A"], cwd=ws, check=True)
        subprocess.run([*GIT, "-c", "user.name=g", "-c", "user.email=g@x", "commit", "-qm", "c"],
                       cwd=ws, check=True)
        for rel, old, new in edits:
            path = ws / rel
            text = path.read_text(encoding="utf-8")
            assert text.count(old) == 1, (rel, old)
            path.write_bytes(text.replace(old, new).encode())
        bug = subprocess.run([*GIT, "diff"], cwd=ws, check=True, capture_output=True).stdout
        fix = subprocess.run([*GIT, "diff", "-R"], cwd=ws, check=True, capture_output=True).stdout
        return bug.decode(), fix.decode()


def yaml_block(text: str) -> str:
    return "\n".join("  " + line if line else "" for line in textwrap.dedent(text).rstrip().splitlines())


for num, spec in T.items():
    task_id = f"orders-api-{num}"
    d = TASKS / task_id
    if d.exists():
        shutil.rmtree(d)
    (d / "heldout" / "tests").mkdir(parents=True)
    bug, fix = diff_for(spec["edits"])
    (d / "bug.patch").write_bytes(bug.encode())
    (d / "fix.patch").write_bytes(fix.encode())
    heldout_name = f"tests/test_heldout_{spec['heldout']}.py"
    (d / "heldout" / heldout_name).write_bytes(textwrap.dedent(spec["test"]).encode())
    # Scoring overwrites the test fixtures with pristine copies, so a conftest hook the agent
    # adds cannot force held-out tests to pass.
    shutil.copyfile(REPO / "tests" / "conftest.py", d / "heldout" / "tests" / "conftest.py")
    expected = ", ".join(spec["expected_files"])
    yaml = (
        f"id: {task_id}\n"
        f"repo: orders-api\n"
        f"title: \"{spec['title']}\"\n"
        f"issue: |\n{yaml_block(spec['issue'])}\n"
        f"difficulty: {spec['difficulty']}\n"
        f"split: {spec['split']}\n"
        f"test_command: [pytest, -q]\n"
        f"heldout_command: [pytest, -q, {heldout_name}]\n"
        f"protected_paths: {PROTECTED}\n"
        f"expected_files: [{expected}]\n"
    )
    (d / "task.yaml").write_bytes(yaml.encode())
    print(task_id, len(bug), "bytes")
