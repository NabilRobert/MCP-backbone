import uuid
from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Only used by the Slice E text-to-SQL benchmark (scripts/run_benchmark.py)
# -- the live server never reads or writes these tables.


class EvalCase(Base):
    """One hand-written benchmark question, with its known-correct
    answer attached. question is something like "How many customers are
    there?"; expected_sql is the SQL that correctly answers it, written
    by hand and trusted as the "answer key.\""""

    __tablename__ = "eval_cases"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    question: Mapped[str]
    expected_sql: Mapped[str]
    difficulty: Mapped[str] = mapped_column(default="unrated", server_default="unrated")


class BenchmarkResult(Base):
    """One recorded outcome from actually running the benchmark: which
    question (eval_case_id) was asked, what SQL the AI agent actually
    came up with (actual_sql), and whether that turned out correct."""

    __tablename__ = "benchmark_results"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    eval_case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eval_cases.id"))
    actual_sql: Mapped[str]
    correct: Mapped[bool]  # lenient score (app/benchmark/grading.py rows_equal)
    # Nullable: results from before these were recorded don't have them.
    strict_correct: Mapped[bool | None]
    model: Mapped[str | None]
    run_started_at: Mapped[datetime | None]
