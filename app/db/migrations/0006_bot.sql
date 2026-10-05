-- Phase 8 (bot): Telegram bot state that must survive ephemeral Actions runs.
-- Additive.

-- Maps Telegram user id -> internal user (for access control + ownership).
CREATE TABLE IF NOT EXISTS telegram_users (
    tg_user_id  INTEGER PRIMARY KEY,
    username    TEXT NOT NULL DEFAULT '',
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

-- Pending "awaiting" prompts per chat (e.g. waiting for campaign choice).
CREATE TABLE IF NOT EXISTS bot_sessions (
    chat_id     INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL DEFAULT 0,
    state_json  TEXT NOT NULL DEFAULT '{}',
    updated_at  REAL NOT NULL
);

-- Telegram getUpdates offset, so polling resumes correctly across runs.
CREATE TABLE IF NOT EXISTS bot_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
