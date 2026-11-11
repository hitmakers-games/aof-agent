CREATE TABLE IF NOT EXISTS members (
    user_id BIGINT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('viewer','operator','admin')),
    active INTEGER NOT NULL DEFAULT 1,
    invited_by BIGINT,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    actor_id BIGINT,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT UNIQUE,
    issue_key TEXT UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    revision TEXT NOT NULL,
    state TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'ask',
    visibility TEXT NOT NULL DEFAULT 'private',
    requester_id BIGINT,
    approved_by BIGINT,
    approved_revision TEXT,
    approval_origin TEXT,
    held INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    origin_action TEXT UNIQUE,
    assigned INTEGER NOT NULL DEFAULT 1,
    needs_approval INTEGER NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS work_queue ON work_items(state,created_at);
CREATE TABLE IF NOT EXISTS notice_batches (
    id TEXT PRIMARY KEY,
    recipient BIGINT NOT NULL,
    kind TEXT NOT NULL,
    due_at DOUBLE PRECISION NOT NULL,
    published INTEGER NOT NULL DEFAULT 0,
    last_render TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    rendered_at DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notice_items (
    batch_id TEXT NOT NULL REFERENCES notice_batches(id),
    work_id TEXT NOT NULL REFERENCES work_items(id),
    revision TEXT NOT NULL,
    PRIMARY KEY(batch_id,work_id)
);
CREATE TABLE IF NOT EXISTS callback_tokens (
    token TEXT PRIMARY KEY,
    recipient BIGINT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    message_key TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS message_handles (
    recipient BIGINT NOT NULL,
    message_key TEXT NOT NULL,
    message_id BIGINT NOT NULL,
    PRIMARY KEY(recipient,message_key)
);
CREATE TABLE IF NOT EXISTS task_drafts (
    id TEXT PRIMARY KEY,
    actor_id BIGINT NOT NULL,
    payload TEXT NOT NULL,
    revision TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'proposed',
    queue_after_register INTEGER NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL
);
