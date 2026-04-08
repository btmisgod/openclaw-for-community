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
  project_id TEXT,
  cycle_no INTEGER,
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

ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS project_id TEXT;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS cycle_no INTEGER;
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  project_id TEXT,
  cycle_no INTEGER,
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

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project_id TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS cycle_no INTEGER;
CREATE INDEX IF NOT EXISTS idx_tasks_run_phase ON tasks(run_id, phase, status);
CREATE INDEX IF NOT EXISTS idx_tasks_project_cycle ON tasks(project_id, cycle_no, phase, status);

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

CREATE TABLE IF NOT EXISTS material_review_items (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  review_task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  section TEXT NOT NULL,
  material_id BIGINT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
  verdict TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_material_review_items_run_section
ON material_review_items(run_id, section, review_task_id);

CREATE TABLE IF NOT EXISTS discussions (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL,
  comment_text TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS draft_reviews (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL,
  section_scope TEXT NOT NULL,
  review_text TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS outputs (
  run_id TEXT PRIMARY KEY REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  project_id TEXT,
  cycle_no INTEGER,
  draft_markdown TEXT,
  revision_plan TEXT,
  final_markdown TEXT,
  final_json JSONB,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE outputs ADD COLUMN IF NOT EXISTS project_id TEXT;
ALTER TABLE outputs ADD COLUMN IF NOT EXISTS cycle_no INTEGER;

CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  status TEXT NOT NULL,
  current_cycle_no INTEGER NOT NULL DEFAULT 0,
  max_cycles INTEGER NOT NULL DEFAULT 10,
  max_consecutive_failures INTEGER NOT NULL DEFAULT 2,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  discussion_seconds INTEGER NOT NULL DEFAULT 45,
  retrospective_seconds INTEGER NOT NULL DEFAULT 600,
  next_cycle_delay_seconds INTEGER NOT NULL DEFAULT 300,
  latest_run_id TEXT,
  next_cycle_at TIMESTAMPTZ,
  paused_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  notes JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS project_cycles (
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  cycle_no INTEGER NOT NULL,
  run_id TEXT REFERENCES workflow_runs(run_id) ON DELETE SET NULL,
  status TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ,
  retrospective_started_at TIMESTAMPTZ,
  retrospective_completed_at TIMESTAMPTZ,
  next_cycle_at TIMESTAMPTZ,
  retrospective_summary TEXT,
  optimization_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (project_id, cycle_no)
);

CREATE TABLE IF NOT EXISTS retrospectives (
  id BIGSERIAL PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  cycle_no INTEGER NOT NULL,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  topic_id TEXT,
  agent_id TEXT NOT NULL,
  message_id TEXT,
  reply_to_message_id TEXT,
  from_agent TEXT,
  to_agent TEXT,
  target_type TEXT,
  topic TEXT,
  intent TEXT,
  round_no INTEGER NOT NULL DEFAULT 1,
  body TEXT,
  comment_text TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE retrospectives ADD COLUMN IF NOT EXISTS message_id TEXT;
ALTER TABLE retrospectives ADD COLUMN IF NOT EXISTS reply_to_message_id TEXT;
ALTER TABLE retrospectives ADD COLUMN IF NOT EXISTS topic_id TEXT;
ALTER TABLE retrospectives ADD COLUMN IF NOT EXISTS from_agent TEXT;
ALTER TABLE retrospectives ADD COLUMN IF NOT EXISTS to_agent TEXT;
ALTER TABLE retrospectives ADD COLUMN IF NOT EXISTS target_type TEXT;
ALTER TABLE retrospectives ADD COLUMN IF NOT EXISTS topic TEXT;
ALTER TABLE retrospectives ADD COLUMN IF NOT EXISTS intent TEXT;
ALTER TABLE retrospectives ADD COLUMN IF NOT EXISTS round_no INTEGER NOT NULL DEFAULT 1;
ALTER TABLE retrospectives ADD COLUMN IF NOT EXISTS body TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_retrospectives_message_id ON retrospectives(message_id);

CREATE TABLE IF NOT EXISTS agent_optimizations (
  id BIGSERIAL PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  cycle_no INTEGER NOT NULL,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL,
  summary_text TEXT NOT NULL,
  optimization_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_agent_memory (
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL,
  current_memory JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (project_id, agent_id)
);

CREATE TABLE IF NOT EXISTS product_reports (
  id BIGSERIAL PRIMARY KEY,
  project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE,
  cycle_no INTEGER,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  agent_id TEXT,
  report_type TEXT NOT NULL,
  title TEXT NOT NULL,
  summary_text TEXT NOT NULL,
  report_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_product_reports_run_type ON product_reports(run_id, report_type, agent_id);

CREATE TABLE IF NOT EXISTS optimization_logs (
  id BIGSERIAL PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  cycle_no INTEGER NOT NULL,
  run_id TEXT REFERENCES workflow_runs(run_id) ON DELETE SET NULL,
  agent_id TEXT,
  source_type TEXT NOT NULL,
  source TEXT NOT NULL,
  author TEXT NOT NULL,
  category TEXT NOT NULL,
  effective_from_cycle INTEGER NOT NULL,
  expires_after_cycle INTEGER,
  body TEXT NOT NULL,
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_optimization_logs_project_cycle
ON optimization_logs(project_id, effective_from_cycle, agent_id, source_type);

CREATE TABLE IF NOT EXISTS cycle_task_plans (
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  cycle_no INTEGER NOT NULL,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  created_by TEXT NOT NULL,
  summary_text TEXT NOT NULL,
  plan_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (project_id, cycle_no)
);

CREATE INDEX IF NOT EXISTS idx_cycle_task_plans_run
ON cycle_task_plans(run_id, project_id, cycle_no);

CREATE TABLE IF NOT EXISTS agent_acks (
  ack_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE,
  cycle_no INTEGER,
  phase_name TEXT NOT NULL,
  section TEXT NOT NULL DEFAULT '全局',
  agent_id TEXT NOT NULL,
  ack_status TEXT NOT NULL,
  understood_goal TEXT NOT NULL DEFAULT '',
  known_dependencies JSONB NOT NULL DEFAULT '[]'::jsonb,
  risk_note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_acks_run_phase_agent
ON agent_acks(run_id, phase_name, section, agent_id, created_at);

CREATE TABLE IF NOT EXISTS manager_control_events (
  event_id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE,
  cycle_no INTEGER,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  stage_name TEXT NOT NULL,
  section TEXT NOT NULL DEFAULT '全局',
  signal_type TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_manager_control_events_run_stage
ON manager_control_events(run_id, stage_name, section, created_at);

CREATE TABLE IF NOT EXISTS draft_versions (
  draft_version_id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  project_id TEXT,
  cycle_no INTEGER,
  version_no INTEGER NOT NULL,
  stage TEXT NOT NULL,
  created_by TEXT NOT NULL,
  markdown_text TEXT NOT NULL,
  report_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(run_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_draft_versions_run_stage
ON draft_versions(run_id, version_no, stage);

CREATE TABLE IF NOT EXISTS proofread_issues (
  issue_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  project_id TEXT,
  cycle_no INTEGER,
  section TEXT NOT NULL,
  item_ref TEXT NOT NULL,
  severity TEXT NOT NULL,
  issue_type TEXT NOT NULL,
  description TEXT NOT NULL,
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  reported_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  resolution_note TEXT,
  opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  closed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_proofread_issues_run_status
ON proofread_issues(run_id, status, severity, section);

CREATE TABLE IF NOT EXISTS proofread_decisions (
  decision_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  issue_id TEXT NOT NULL REFERENCES proofread_issues(issue_id) ON DELETE CASCADE,
  decided_by TEXT NOT NULL,
  decision_type TEXT NOT NULL,
  rationale TEXT NOT NULL,
  decision_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_proofread_decisions_run_issue
ON proofread_decisions(run_id, issue_id);

CREATE TABLE IF NOT EXISTS revision_patches (
  patch_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  decision_id TEXT REFERENCES proofread_decisions(decision_id) ON DELETE SET NULL,
  issue_id TEXT REFERENCES proofread_issues(issue_id) ON DELETE SET NULL,
  target_section TEXT NOT NULL,
  patch_instruction TEXT NOT NULL,
  patch_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  applied_by TEXT,
  source_task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  applied_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_revision_patches_run
ON revision_patches(run_id, target_section, applied_at);

CREATE TABLE IF NOT EXISTS final_reports (
  final_report_id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL UNIQUE REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  project_id TEXT,
  cycle_no INTEGER,
  source_draft_version_id BIGINT REFERENCES draft_versions(draft_version_id) ON DELETE SET NULL,
  markdown_text TEXT NOT NULL,
  report_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  published_by TEXT NOT NULL,
  published_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS retro_topics (
  topic_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  project_id TEXT,
  cycle_no INTEGER,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  opened_by TEXT NOT NULL,
  opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  closed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_retro_topics_run_status
ON retro_topics(run_id, status, opened_at);

CREATE TABLE IF NOT EXISTS retro_decisions (
  decision_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  topic_id TEXT NOT NULL REFERENCES retro_topics(topic_id) ON DELETE CASCADE,
  summary TEXT NOT NULL,
  owner_agent TEXT NOT NULL,
  action_rule_ref TEXT,
  decision_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_retro_decisions_run_topic
ON retro_decisions(run_id, topic_id);

CREATE TABLE IF NOT EXISTS optimization_rules (
  rule_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  run_id TEXT REFERENCES workflow_runs(run_id) ON DELETE SET NULL,
  cycle_no INTEGER NOT NULL,
  source TEXT NOT NULL,
  owner_scope TEXT NOT NULL,
  target_agent TEXT,
  target_section TEXT,
  effective_from_cycle INTEGER NOT NULL,
  expires_after_cycle INTEGER,
  rule_type TEXT NOT NULL,
  rule_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  rationale TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_optimization_rules_project_cycle
ON optimization_rules(project_id, effective_from_cycle, target_agent, rule_type, status);

CREATE TABLE IF NOT EXISTS llm_jobs (
  job_id TEXT PRIMARY KEY,
  job_key TEXT UNIQUE,
  node_type TEXT NOT NULL,
  project_id TEXT,
  run_id TEXT,
  cycle_no INTEGER,
  task_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  generation_mode TEXT,
  generation_error TEXT,
  queue_delay_ms BIGINT,
  model_latency_ms BIGINT,
  timeout_ms INTEGER NOT NULL,
  prompt_size INTEGER NOT NULL DEFAULT 0,
  input_size INTEGER NOT NULL DEFAULT 0,
  evidence_object_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 1,
  backoff_ms INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  provider_model TEXT,
  prompt_system TEXT NOT NULL,
  prompt_user TEXT NOT NULL,
  fallback_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_llm_jobs_status_next_attempt
ON llm_jobs(status, next_attempt_at, created_at);

CREATE INDEX IF NOT EXISTS idx_llm_jobs_run_node
ON llm_jobs(run_id, node_type, status);

CREATE INDEX IF NOT EXISTS idx_cycles_project_status ON project_cycles(project_id, status, cycle_no);
CREATE INDEX IF NOT EXISTS idx_retrospectives_run_cycle ON retrospectives(run_id, cycle_no, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_optimizations_project_cycle ON agent_optimizations(project_id, cycle_no, agent_id);
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
