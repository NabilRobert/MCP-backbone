"""Loads a small, synthetic, fully public demo dataset into the target
database -- a toy storefront (customers, products, orders, order_items).

This is hand-built synthetic data, not sourced from any real system or
real customers (per the honesty constraint: no fabricated claims of real
usage). It exists purely to give the safety layer and the Slice E
benchmark a schema with real relational complexity (joins, aggregation)
without any privacy/licensing concern."""

import argparse
import asyncio
import datetime
import random
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SCHEMA_SQL = """
drop table if exists order_items;
drop table if exists orders;
drop table if exists products;
drop table if exists customers;

create table customers (
    id serial primary key,
    name text not null,
    email text not null,
    country text not null
);

create table products (
    id serial primary key,
    name text not null,
    category text not null,
    price numeric(10, 2) not null
);

create table orders (
    id serial primary key,
    customer_id integer not null references customers(id),
    order_date date not null,
    status text not null
);

create table order_items (
    id serial primary key,
    order_id integer not null references orders(id),
    product_id integer not null references products(id),
    quantity integer not null,
    unit_price numeric(10, 2) not null
);
"""

COUNTRIES = ["Japan", "Indonesia", "Singapore", "USA", "Germany"]
CATEGORIES = ["electronics", "books", "home", "clothing"]
STATUSES = ["placed", "shipped", "delivered", "cancelled"]


def _generate_rows(
    num_customers: int, num_products: int, num_orders: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    random.seed(42)  # deterministic -- same dataset every run

    customers: list[dict[str, Any]] = [
        {"id": i, "name": f"Customer {i}", "email": f"customer{i}@example.com", "country": random.choice(COUNTRIES)}
        for i in range(1, num_customers + 1)
    ]
    products: list[dict[str, Any]] = [
        {
            "id": i,
            "name": f"Product {i}",
            "category": random.choice(CATEGORIES),
            "price": round(random.uniform(5, 500), 2),
        }
        for i in range(1, num_products + 1)
    ]
    orders: list[dict[str, Any]] = []
    order_items: list[dict[str, Any]] = []
    item_id = 1
    for order_id in range(1, num_orders + 1):
        customer_id = random.randint(1, num_customers)
        orders.append(
            {
                "id": order_id,
                "customer_id": customer_id,
                "order_date": datetime.date(2026, random.randint(1, 10), random.randint(1, 28)),
                "status": random.choice(STATUSES),
            }
        )
        for _ in range(random.randint(1, 4)):
            product = random.choice(products)
            order_items.append(
                {
                    "id": item_id,
                    "order_id": order_id,
                    "product_id": product["id"],
                    "quantity": random.randint(1, 5),
                    "unit_price": product["price"],
                }
            )
            item_id += 1

    return customers, products, orders, order_items


async def load(connection_url: str, num_customers: int, num_products: int, num_orders: int) -> None:
    customers, products, orders, order_items = _generate_rows(num_customers, num_products, num_orders)

    engine = create_async_engine(connection_url)
    try:
        async with engine.begin() as conn:
            for statement in SCHEMA_SQL.strip().split(";"):
                if statement.strip():
                    await conn.execute(text(statement))

            await conn.execute(
                text("insert into customers (id, name, email, country) "
                     "values (:id, :name, :email, :country)"),
                customers,
            )
            await conn.execute(
                text("insert into products (id, name, category, price) "
                     "values (:id, :name, :category, :price)"),
                products,
            )
            await conn.execute(
                text("insert into orders (id, customer_id, order_date, status) "
                     "values (:id, :customer_id, :order_date, :status)"),
                orders,
            )
            await conn.execute(
                text("insert into order_items (id, order_id, product_id, quantity, unit_price) "
                     "values (:id, :order_id, :product_id, :quantity, :unit_price)"),
                order_items,
            )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-url", required=True)
    parser.add_argument("--num-customers", type=int, default=50)
    parser.add_argument("--num-products", type=int, default=30)
    parser.add_argument("--num-orders", type=int, default=200)
    args = parser.parse_args()

    asyncio.run(
        load(args.connection_url, args.num_customers, args.num_products, args.num_orders)
    )
    print("Demo dataset loaded: customers, products, orders, order_items.")


if __name__ == "__main__":
    main()
