"""SQLModel tables and the engine factory (PostgreSQL).

Tables:

- ``cards`` — the content you author. ``question``/``answer`` are ProseMirror
  documents (JSONB) — the portable rich-text format the TipTap editor produces and
  every future client (web, extension, React Native) can render.
- ``progress`` — FSRS scheduling state, one row per card (keyed by ``cards.id``).
  ``card_json`` holds the serialised ``fsrs.Card``; ``due``/``state`` are denormalised
  for the picker; ``reps``/``rating_sum``/``last_rating`` back the stats aggregates.
- ``reviews`` — the raw append-only grade log.
- ``fsrs_params`` — trained FSRS weight sets; the newest is loaded at startup.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel


def _empty_doc() -> dict:
    """An empty ProseMirror document."""
    return {"type": "doc", "content": []}


class Card(SQLModel, table=True):
    __tablename__ = "cards"

    id: str = Field(primary_key=True)
    # ProseMirror/TipTap documents — the reusable rich-text content model.
    question: dict = Field(default_factory=_empty_doc, sa_column=Column(JSONB, nullable=False))
    answer: dict = Field(default_factory=_empty_doc, sa_column=Column(JSONB, nullable=False))
    theme: str = Field(default="", index=True)
    subtheme: str = ""
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    position: float = Field(default=0.0, index=True)  # manual ordering
    created_at: float = 0.0
    updated_at: float = Field(default=0.0, index=True)


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
