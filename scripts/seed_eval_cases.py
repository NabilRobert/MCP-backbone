"""Seeds the text-to-SQL benchmark questions (Slice E). Two sets:

- pagila (default, 30 questions): the live demo database. Hand-written,
  since no public text-to-SQL benchmark targets Pagila, and checked
  against the actual data before use: every answer is small (well under
  the 200-row result cap), every "top N" question has a clear gap at its
  cut-off (no ties that would make two answers equally right), and date
  questions say UTC, since the columns are timestamptz and the answer
  would otherwise depend on the server's time zone.
- demo (10 questions): the original toy storefront from
  scripts/load_demo_dataset.py.

Replaces the current set, and clears any results recorded against it.

    python -m scripts.seed_eval_cases [--dataset pagila|demo]
"""

import argparse
import asyncio

from sqlalchemy import delete

from app.benchmark.models import BenchmarkResult, EvalCase
from app.db import SessionLocal

# (difficulty, question, expected_sql)
PAGILA: list[tuple[str, str, str]] = [
    ("easy", "How many films are in the database?", "select count(*) from film"),
    ("easy", "How many films are rated 'R'?", "select count(*) from film where rating = 'R'"),
    ("easy", "How many actors are there?", "select count(*) from actor"),
    (
        "easy",
        "What is the average film length in minutes, rounded to 2 decimal places?",
        "select round(avg(length), 2) from film",
    ),
    (
        "easy",
        "How many films have a rental rate of 4.99?",
        "select count(*) from film where rental_rate = 4.99",
    ),
    ("easy", "What is the length in minutes of the longest film?", "select max(length) from film"),
    ("easy", "How many countries are there?", "select count(*) from country"),
    ("easy", "What is the total of all payment amounts?", "select sum(amount) from payment"),
    (
        "easy",
        "How many films have a replacement cost greater than 25?",
        "select count(*) from film where replacement_cost > 25",
    ),
    (
        "easy",
        "How many rentals have not been returned yet?",
        "select count(*) from rental where return_date is null",
    ),
    (
        "medium",
        "How many films are there for each rating?",
        "select rating, count(*) from film group by rating",
    ),
    (
        "medium",
        "How many films are there in each language? Give the language name and the count.",
        (
            "select l.name, count(*) from film f join language l on l.language_id = f.language_id "
            "group by l.name"
        ),
    ),
    (
        "medium",
        "How many films are in the 'Action' category?",
        (
            "select count(*) from film_category fc join category c on c.category_id = fc.category_id "
            "where c.name = 'Action'"
        ),
    ),
    (
        "medium",
        "How many rentals were made in 2025 (UTC)?",
        (
            "select count(*) from rental where (rental_date at time zone 'UTC') >= '2025-01-01' "
            "and (rental_date at time zone 'UTC') < '2026-01-01'"
        ),
    ),
    (
        "medium",
        "What is the average rental rate for each rating, rounded to 2 decimal places?",
        "select rating, round(avg(rental_rate), 2) from film group by rating",
    ),
    (
        "medium",
        "How many films has the actor with actor_id 1 appeared in?",
        "select count(*) from film_actor where actor_id = 1",
    ),
    (
        "medium",
        "What is the total payment amount for each year (UTC)? Give the year and the total.",
        (
            "select extract(year from payment_date at time zone 'UTC')::int, sum(amount) "
            "from payment group by 1"
        ),
    ),
    (
        "medium",
        "How many cities are in the country 'Canada'?",
        (
            "select count(*) from city ci join country co on co.country_id = ci.country_id "
            "where co.country = 'Canada'"
        ),
    ),
    (
        "medium",
        "Which film category has the fewest films, and how many does it have?",
        (
            "select c.name, count(*) from film_category fc join category c "
            "on c.category_id = fc.category_id group by c.name order by count(*) asc limit 1"
        ),
    ),
    (
        "medium",
        "How many distinct customers have rented at least one film?",
        "select count(distinct customer_id) from rental",
    ),
    (
        "hard",
        "Who are the top 5 customers by total payment amount? Give customer_id and the total.",
        (
            "select customer_id, sum(amount) as total from payment group by customer_id "
            "order by total desc limit 5"
        ),
    ),
    (
        "hard",
        "What are the 3 most rented films? Give the title and the number of rentals.",
        (
            "select f.title, count(*) as n from rental r "
            "join inventory i on i.inventory_id = r.inventory_id "
            "join film f on f.film_id = i.film_id group by f.film_id, f.title "
            "order by n desc limit 3"
        ),
    ),
    (
        "hard",
        "Which film category has generated the most payment revenue, and how much?",
        (
            "select c.name, sum(p.amount) as revenue from payment p "
            "join rental r on r.rental_id = p.rental_id "
            "join inventory i on i.inventory_id = r.inventory_id "
            "join film_category fc on fc.film_id = i.film_id "
            "join category c on c.category_id = fc.category_id "
            "group by c.name order by revenue desc limit 1"
        ),
    ),
    (
        "hard",
        (
            "What is the average number of rentals per customer, counting only customers who "
            "have rented at least once, rounded to 2 decimal places?"
        ),
        "select round(count(*)::numeric / count(distinct customer_id), 2) from rental",
    ),
    (
        "hard",
        "How many films have never been rented?",
        (
            "select count(*) from film f where not exists (select 1 from inventory i "
            "join rental r on r.inventory_id = i.inventory_id where i.film_id = f.film_id)"
        ),
    ),
    (
        "hard",
        "In which month (as YYYY-MM, UTC) were the most rentals made, and how many?",
        (
            "select to_char(rental_date at time zone 'UTC', 'YYYY-MM') as m, count(*) as n "
            "from rental group by m order by n desc limit 1"
        ),
    ),
    (
        "hard",
        "How many customers have made more than 60 rentals?",
        (
            "select count(*) from (select customer_id from rental group by customer_id "
            "having count(*) > 60) t"
        ),
    ),
    (
        "hard",
        "How many films are longer than the average film length?",
        "select count(*) from film where length > (select avg(length) from film)",
    ),
    (
        "hard",
        (
            "Which actor has appeared in the most films? Give first name, last name and the "
            "number of films."
        ),
        (
            "select a.first_name, a.last_name, count(*) as n from actor a "
            "join film_actor fa on fa.actor_id = a.actor_id "
            "group by a.actor_id, a.first_name, a.last_name order by n desc limit 1"
        ),
    ),
    (
        "hard",
        "Which customer has spent the most in total? Give first name, last name and the total.",
        (
            "select c.first_name, c.last_name, sum(p.amount) as total from customer c "
            "join payment p on p.customer_id = c.customer_id "
            "group by c.customer_id, c.first_name, c.last_name order by total desc limit 1"
        ),
    ),
]

DEMO: list[tuple[str, str, str]] = [
    ("unrated", "How many customers are there in total?", "select count(*) from customers"),
    (
        "unrated",
        "List the names of all products in the 'electronics' category.",
        "select name from products where category = 'electronics'",
    ),
    (
        "unrated",
        "What is the total revenue across all order items?",
        "select sum(quantity * unit_price) from order_items",
    ),
    (
        "unrated",
        "Which country has the most customers?",
        "select country from customers group by country order by count(*) desc limit 1",
    ),
    (
        "unrated",
        "How many orders have the status 'cancelled'?",
        "select count(*) from orders where status = 'cancelled'",
    ),
    (
        "unrated",
        "What is the average price of products in the 'books' category?",
        "select avg(price) from products where category = 'books'",
    ),
    (
        "unrated",
        "How many distinct products have been ordered at least once?",
        "select count(distinct product_id) from order_items",
    ),
    (
        "unrated",
        "What is the name of the most expensive product?",
        "select name from products order by price desc limit 1",
    ),
    (
        "unrated",
        "For each order status, how many orders are there?",
        "select status, count(*) from orders group by status",
    ),
    (
        "unrated",
        (
            "What are the ids of the top 5 customers by total amount spent, and how much did each "
            "spend?"
        ),
        (
            "select o.customer_id, sum(oi.quantity * oi.unit_price) as total from orders o "
            "join order_items oi on oi.order_id = o.id group by o.customer_id "
            "order by total desc limit 5"
        ),
    ),
]

DATASETS = {"pagila": PAGILA, "demo": DEMO}


async def seed(dataset: str) -> int:
    cases = DATASETS[dataset]
    async with SessionLocal() as session:
        await session.execute(delete(BenchmarkResult))
        await session.execute(delete(EvalCase))
        for difficulty, question, expected_sql in cases:
            session.add(
                EvalCase(question=question, expected_sql=expected_sql, difficulty=difficulty)
            )
        await session.commit()
    return len(cases)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=sorted(DATASETS), default="pagila")
    args = parser.parse_args()
    print(f"Seeded {asyncio.run(seed(args.dataset))} {args.dataset} eval cases.")


if __name__ == "__main__":
    main()
