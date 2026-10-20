from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class OrderStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class LineItem(BaseModel):
    sku: str
    quantity: int = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    cancelled: bool = False


class OrderCreate(BaseModel):
    customer: str = Field(min_length=1)
    items: list[LineItem] = Field(min_length=1)
    discount_pct: Decimal = Field(default=Decimal("0"), ge=0, le=100)


class Order(BaseModel):
    id: int
    customer: str
    status: OrderStatus
    items: list[LineItem]
    discount_pct: Decimal
    created_at: datetime
    subtotal: Decimal
    total: Decimal


class Page[T](BaseModel):
    items: list[T]
    page: int
    page_size: int
    total: int
    pages: int
