"""SQLModel tables and the engine factory (PostgreSQL).

Three tables:

- ``progress`` — one row per card. ``card_json`` holds the serialised ``fsrs.Card``
  (the per-card FSRS state); ``due``/``state`` are denormalised for the picker;
  ``reps``/``rating_sum``/``last_rating`` back the stats aggregates.
- ``reviews`` — the raw append-only log, one row per grade.
- ``fsrs_params`` — trained FSRS weight sets. The newest row is loaded at startup,
  so optimised weights survive restarts and server moves.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel


class Progress(SQLModel, table=True):
    __tablename__ = "progress"

    question_id: str = Field(primary_key=True)
    card_json: dict = Field(sa_column=Column(JSONB, nullable=False))
    due: float = Field(index=True)
    state: int
    reps: int = 0
    rating_sum: int = 0
    last_rating: int = 0
    last_review: float = 0.0
    theme: str = ""
    subtheme: str = ""
    section: str = ""
    question_text: str = ""


class Review(SQLModel, table=True):
    __tablename__ = "reviews"

    id: int | None = Field(default=None, primary_key=True)
    question_id: str = Field(index=True)
    rating: int
    reviewed_at: float


class FsrsParams(SQLModel, table=True):
    __tablename__ = "fsrs_params"

    id: int | None = Field(default=None, primary_key=True)
    weights: list[float] = Field(sa_column=Column(JSONB, nullable=False))
    desired_retention: float
    review_count: int
    trained_at: float


def normalise_dsn(database_url: str) -> str:
    """Force the psycopg (v3) driver — SQLAlchemy defaults ``postgresql://`` to psycopg2."""
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if database_url.startswith(prefix):
            return database_url
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url[len("postgres://"):]
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://"):]
    return database_url


def make_engine(database_url: str):
    from sqlmodel import create_engine

    engine = create_engine(normalise_dsn(database_url), pool_pre_ping=True)
    SQLModel.metadata.create_all(engine)
    return engine
