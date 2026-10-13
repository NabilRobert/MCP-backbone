"""Seeds the hand-built text-to-SQL benchmark questions (Slice E) against
the toy storefront schema from load_demo_dataset.py. Idempotent: clears
existing EvalCase rows first, so re-running updates the set in place."""

import asyncio

from sqlalchemy import delete

from app.benchmark.models import EvalCase
from app.db import SessionLocal

CASES: list[dict[str, str]] = [
    {
        "question": "How many customers are there in total?",
        "expected_sql": "select count(*) from customers",
    },
    {
        "question": "List the names of all products in the 'electronics' category.",
        "expected_sql": "select name from products where category = 'electronics'",
    },
    {
        "question": "What is the total revenue across all order items?",
        "expected_sql": "select sum(quantity * unit_price) from order_items",
    },
    {
        "question": "Which country has the most customers?",
        "expected_sql": "select country from customers group by country "
        "order by count(*) desc limit 1",
    },
    {
        "question": "How many orders have the status 'cancelled'?",
        "expected_sql": "select count(*) from orders where status = 'cancelled'",
    },
    {
        "question": "What is the average price of products in the 'books' category?",
        "expected_sql": "select avg(price) from products where category = 'books'",
    },
    {
        "question": "How many distinct products have been ordered at least once?",
        "expected_sql": "select count(distinct product_id) from order_items",
    },
    {
        "question": "What is the name of the most expensive product?",
        "expected_sql": "select name from products order by price desc limit 1",
    },
    {
        "question": "For each order status, how many orders are there?",
        "expected_sql": "select status, count(*) from orders group by status",
    },
    {
        "question": "What are the ids of the top 5 customers by total amount spent, "
        "and how much did each spend?",
        "expected_sql": "select o.customer_id, sum(oi.quantity * oi.unit_price) as total "
        "from orders o join order_items oi on oi.order_id = o.id "
        "group by o.customer_id order by total desc limit 5",
    },
]


async def seed() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(EvalCase))
        for case in CASES:
            session.add(EvalCase(question=case["question"], expected_sql=case["expected_sql"]))
        await session.commit()


def main() -> None:
    asyncio.run(seed())
    print(f"Seeded {len(CASES)} eval cases.")


if __name__ == "__main__":
    main()
