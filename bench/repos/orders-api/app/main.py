from fastapi import FastAPI, Header, HTTPException, Query, Response

from app.models import Order, OrderCreate, Page
from app.pagination import PaginationError, paginate
from app.store import InvalidTransition, OrderNotFound, OrderStore, seeded_store


def create_app(store: OrderStore | None = None) -> FastAPI:
    app = FastAPI(title="orders-api")
    orders = store if store is not None else seeded_store()

    @app.get("/orders", response_model=Page[Order])
    def list_orders(
        page: int = Query(1),
        page_size: int | None = Query(None),
        status: str | None = Query(None),
        sort: str = Query("created_at"),
    ) -> Page[Order]:
        try:
            matching = orders.list(status=status, sort=sort)
            return paginate(matching, page, page_size)
        except (PaginationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/orders/{order_id}", response_model=Order)
    def get_order(order_id: int) -> Order:
        try:
            return orders.get(order_id)
        except OrderNotFound:
            raise HTTPException(status_code=404, detail="order not found") from None

    @app.post("/orders", response_model=Order, status_code=201)
    def create_order(
        data: OrderCreate, response: Response,
        idempotency_key: str | None = Header(None),
    ) -> Order:
        order, created = orders.create(data, idempotency_key=idempotency_key)
        if not created:
            response.status_code = 200
        return order

    @app.post("/orders/{order_id}/cancel", response_model=Order)
    def cancel_order(order_id: int) -> Order:
        try:
            return orders.cancel(order_id)
        except OrderNotFound:
            raise HTTPException(status_code=404, detail="order not found") from None
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app


app = create_app()
