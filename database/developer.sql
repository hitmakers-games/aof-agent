CREATE TABLE IF NOT EXISTS telegram_messages (
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    sender_id BIGINT,
    sender_name TEXT NOT NULL DEFAULT '',
    sent_at DOUBLE PRECISION NOT NULL,
    text TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(chat_id,message_id)
);
CREATE INDEX IF NOT EXISTS telegram_history ON telegram_messages(chat_id,sent_at);
CREATE TABLE IF NOT EXISTS developer_effects (
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    result TEXT,
    PRIMARY KEY(request_id,operation,fingerprint)
);
