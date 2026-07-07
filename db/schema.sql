-- Personal AI Assistant System — SQLite Schema
-- Apply with: sqlite3 db/assistant.db < db/schema.sql

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type      TEXT    NOT NULL,          -- 'email' | 'voice' | 'file' | 'webhook' | 'chat'
    source_ref       TEXT    NOT NULL,          -- external ID from the source system
    dedupe_key       TEXT    NOT NULL UNIQUE,   -- SHA256(source_ref + "|" + title); prevents double-processing
    status           TEXT    NOT NULL DEFAULT 'pending',
                                                -- 'pending' | 'approved' | 'rejected' | 'done' | 'error' | 'dead_letter'
    intent           TEXT,                      -- classified intent set by the triage agent
    payload_json     TEXT    NOT NULL,          -- raw intake payload serialised as JSON text
    result_json      TEXT,                      -- output from the processing node, if any
    approval_required INTEGER NOT NULL DEFAULT 0,  -- 0 = auto-proceed, 1 = human gate required
    attempts         INTEGER NOT NULL DEFAULT 0,   -- execution attempts so far
    next_retry_at    TEXT,                      -- ISO timestamp; NULL = runnable now
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS approvals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    requested_action TEXT    NOT NULL,   -- human-readable description of the proposed action
    reviewer         TEXT,               -- identity of the reviewer (email or username)
    decision         TEXT,               -- 'approved' | 'rejected' | NULL while pending
    reason           TEXT,               -- optional free-text reason from reviewer
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at       TEXT                -- NULL until a decision is recorded
);

-- Keep updated_at current whenever a jobs row changes
CREATE TRIGGER IF NOT EXISTS jobs_updated_at
    AFTER UPDATE ON jobs
    FOR EACH ROW
BEGIN
    UPDATE jobs
    SET    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE  id = OLD.id;
END;

CREATE INDEX IF NOT EXISTS idx_jobs_status        ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe        ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_approvals_job_id   ON approvals(job_id);
CREATE INDEX IF NOT EXISTS idx_approvals_decision ON approvals(decision);
