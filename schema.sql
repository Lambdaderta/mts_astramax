-- Locally reconstructed from the supplied prose. Migration 1, additive only.
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','closed')),
 phase TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, opened_at TEXT NOT NULL, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), seq INTEGER NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('user','assistant')), text TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(session_id,seq)
);
CREATE TABLE IF NOT EXISTS turns (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), client_message_id TEXT NOT NULL,
 request_hash TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('processing','completed','failed')),
 user_message_id TEXT NOT NULL REFERENCES messages(id), assistant_message_id TEXT REFERENCES messages(id),
 attempt INTEGER NOT NULL DEFAULT 1, lease_until REAL, error_code TEXT, retryable INTEGER NOT NULL DEFAULT 0,
 response_json TEXT, created_at TEXT NOT NULL, UNIQUE(session_id,client_message_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_processing_turn ON turns(session_id) WHERE status='processing';
CREATE TABLE IF NOT EXISTS lead_drafts (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), revision INTEGER NOT NULL DEFAULT 0,
 full_name TEXT, phone TEXT, company TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contact_history (
 session_id TEXT NOT NULL REFERENCES sessions(id), field_name TEXT NOT NULL,
 value TEXT NOT NULL, PRIMARY KEY(session_id,field_name,value)
);
CREATE TABLE IF NOT EXISTS lead_previews (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 snapshot_last_seq INTEGER NOT NULL, draft_revision INTEGER NOT NULL, snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), preview_id TEXT NOT NULL UNIQUE REFERENCES lead_previews(id),
 idempotency_key TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('partial','complete')),
 phone TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(session_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS answer_traces (
 assistant_message_id TEXT PRIMARY KEY REFERENCES messages(id), session_id TEXT NOT NULL REFERENCES sessions(id),
 payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_opened ON sessions(opened_at,id);
CREATE INDEX IF NOT EXISTS leads_created ON leads(created_at,id);
PRAGMA user_version=1;
