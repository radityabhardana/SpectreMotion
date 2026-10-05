-- Phase 0 baseline: reflects the MVP schema already in use, PLUS the tables
-- later phases will need (created empty/optional so migrations stay additive).
-- NOTE: this file is immutable once shipped; never edit — add 0002+ instead.

CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    status        TEXT NOT NULL,
    stage         TEXT NOT NULL DEFAULT '',
    progress      REAL NOT NULL DEFAULT 0,
    source_type   TEXT NOT NULL DEFAULT 'file',
    source_name   TEXT NOT NULL DEFAULT '',
    source_path   TEXT NOT NULL DEFAULT '',
    source_url    TEXT NOT NULL DEFAULT '',
    campaign_id   TEXT NOT NULL DEFAULT 'none',
    template_path TEXT NOT NULL DEFAULT '',
    settings_json TEXT NOT NULL DEFAULT '{}',
    error         TEXT NOT NULL DEFAULT '',
    message       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS clips (
    id          TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    start_s     REAL NOT NULL,
    end_s       REAL NOT NULL,
    viral_score INTEGER NOT NULL DEFAULT 0,
    hook        TEXT NOT NULL DEFAULT '',
    topic       TEXT NOT NULL DEFAULT '',
    caption     TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    risk_flags  TEXT NOT NULL DEFAULT '[]',
    verdict     TEXT NOT NULL DEFAULT 'pass',
    verdict_msg TEXT NOT NULL DEFAULT '',
    output_path TEXT NOT NULL DEFAULT '',
    rendered    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_clips_job ON clips(job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

-- Phase 1 seam: lease/queue columns on jobs (nullable / defaulted so the MVP
-- code keeps working unchanged).
ALTER TABLE jobs ADD COLUMN lane TEXT NOT NULL DEFAULT 'heavy';
ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 5;
ALTER TABLE jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE jobs ADD COLUMN available_at REAL;
ALTER TABLE jobs ADD COLUMN lease_owner TEXT;
ALTER TABLE jobs ADD COLUMN lease_expires_at REAL;

CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, lane, priority, available_at);
