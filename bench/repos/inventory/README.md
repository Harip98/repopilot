# inventory

Warehouse stock ledger.

- **Costing is FIFO**: shipments consume the oldest received units first, and the cost of goods
  shipped (COGS) is the sum of those units' purchase costs.
- **Stock never goes negative**: shipping more than is on hand raises `InsufficientStock` and
  changes nothing. Received quantities must be positive.
- **Reorder point** = average daily demand × (lead time + safety days), rounded **up** to whole
  units. Reorder when stock on hand is at or below it.
- **Transaction files** (`date,sku,kind,qty,unit_cost`, ISO dates) are applied in date order;
  rows for the same day keep their file order. Exports are grouped by SKU, not sorted by date.
- `report()` gives one row per SKU, sorted by SKU: units on hand, the value of those units at
  cost, and whether to reorder.
