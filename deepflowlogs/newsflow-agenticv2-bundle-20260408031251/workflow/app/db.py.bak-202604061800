from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterable

import psycopg

from .config import load_settings


SETTINGS = load_settings()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workflow_runs (
  workflow_id TEXT NOT NULL,
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  discussion_seconds INTEGER NOT NULL,
  forced_reject_done BOOLEAN NOT NULL DEFAULT FALSE,
  current_phase TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ,
  report_markdown_path TEXT,
  report_json_path TEXT,
  notes JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  parent_task_id TEXT,
  agent_id TEXT NOT NULL,
  agent_role TEXT NOT NULL,
  section TEXT NOT NULL,
  phase TEXT NOT NULL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tasks_run_phase ON tasks(run_id, phase, status);

CREATE TABLE IF NOT EXISTS materials (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  section TEXT NOT NULL,
  source_agent TEXT NOT NULL,
  title TEXT NOT NULL,
  source_media TEXT NOT NULL,
  published_at TIMESTAMPTZ NOT NULL,
  link TEXT NOT NULL,
  images JSONB NOT NULL DEFAULT '[]'::jsonb,
  summary_zh TEXT,
  brief_zh TEXT,
  is_primary_candidate BOOLEAN NOT NULL DEFAULT FALSE,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_materials_run_section ON materials(run_id, section);

CREATE TABLE IF NOT EXISTS reviews (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  section TEXT NOT NULL,
  review_task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  reviewer_agent TEXT NOT NULL,
  approved BOOLEAN NOT NULL,
  reason TEXT NOT NULL,
  selected_material_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS discussions (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL,
  comment_text TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS outputs (
  run_id TEXT PRIMARY KEY REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  draft_markdown TEXT,
  revision_plan TEXT,
  final_markdown TEXT,
  final_json JSONB,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


@contextmanager
def get_conn(autocommit: bool = False):
    conn = psycopg.connect(SETTINGS.postgres_dsn, autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()


def init_schema() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()


def fetch_one(query: str, params: Iterable[Any] | None = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or [])
            return cur.fetchone()


def fetch_all(query: str, params: Iterable[Any] | None = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or [])
            return cur.fetchall()


def execute(query: str, params: Iterable[Any] | None = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or [])
        conn.commit()


def execute_returning(query: str, params: Iterable[Any] | None = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or [])
            row = cur.fetchone()
        conn.commit()
        return row


def jdump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)
