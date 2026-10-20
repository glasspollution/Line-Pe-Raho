"""SQLite persistence (CLAUDE.md §13), kept small: tasks and calls. Journals, recordings and summaries
live as files under data/calls/{call_id}/ (journal.jsonl, recordings/*.wav, summary.json)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, create_engine


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Task(SQLModel, table=True):
    id: str = Field(primary_key=True)
    created_at: dt.datetime = Field(default_factory=_now)
    raw_text: str
    user_first_name: str = "Aarav"
    user_language: str
    company_id: str
    profile_json: str
    plan_json: str
    plan_method: str
    status: str = "planned"  # planned | calling | done


class Call(SQLModel, table=True):
    id: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    started_at: dt.datetime = Field(default_factory=_now)
    ended_at: dt.datetime | None = None
    final_state: str | None = None
    mode: str = "sim"  # sim | live
    line_kind: str = "virtual"
    noise_profile: str = "snr10"
    hold_seconds: float | None = None
    ivr_seconds: float | None = None
    time_to_human_s: float | None = None
    recording_dir: str = ""


def make_engine(data_dir: Path) -> Engine:
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{data_dir / 'app.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def session(engine: Engine) -> Session:
    return Session(engine)
