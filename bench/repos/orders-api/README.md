# orders-api

Small orders service used by the storefront.

| Route | Purpose |
|---|---|
| `GET /orders` | List orders. Query: `page` (1-based), `page_size` (max 100), `status`, `sort` (`created_at`, `-created_at`, `total`, `-total`) |
| `GET /orders/{id}` | One order |
| `POST /orders` | Create an order. Send an `Idempotency-Key` header to make retries safe |
| `POST /orders/{id}/cancel` | Cancel an order that has not shipped |

Money is handled with `Decimal` and rounded half-up to cents. Tax is applied after discounts.

```
pip install -e ".[test]"
pytest
```
