-- Phase 3: clip catalog enrichment + per-user hook template overrides.
-- Additive only.

-- Clip status lets the catalog distinguish candidate vs rendered vs rejected,
-- and rank gives a stable catalog order independent of insertion.
ALTER TABLE clips ADD COLUMN status TEXT NOT NULL DEFAULT 'candidate';
ALTER TABLE clips ADD COLUMN rank INTEGER NOT NULL DEFAULT 0;
ALTER TABLE clips ADD COLUMN thumbnail_path TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS template_overrides (
    id          TEXT PRIMARY KEY,
    preset_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    props_json  TEXT NOT NULL DEFAULT '{}',
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    UNIQUE (name)
);
