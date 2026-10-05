-- Phase 1 fix: the queue state machine (complete/fail/cancel) records
-- `finished_at`, but 0001 never created it. Additive column.

ALTER TABLE jobs ADD COLUMN finished_at REAL;
