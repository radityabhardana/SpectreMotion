-- Phase 1: job events (SSE replay log) + stage checkpoints (crash resume).
-- Additive only.

CREATE TABLE IF NOT EXISTS job_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    type       TEXT NOT NULL,
    data_json  TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE (job_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, seq);

CREATE TABLE IF NOT EXISTS job_stages (
    job_id          TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    stage           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempt         INTEGER NOT NULL DEFAULT 0,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    started_at      REAL,
    finished_at     REAL,
    PRIMARY KEY (job_id, stage)
);
