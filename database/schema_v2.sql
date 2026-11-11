ALTER TABLE inbox ADD COLUMN IF NOT EXISTS chat_id BIGINT;
ALTER TABLE inbox ADD COLUMN IF NOT EXISTS requested_by BIGINT;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS chat_id BIGINT;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS required_role TEXT NOT NULL DEFAULT 'operator';
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS event_key TEXT;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS reply_markup TEXT;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS message_key TEXT;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS delivered_message_id BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS outbox_event ON outbox(event_key);
UPDATE inbox SET chat_id=(SELECT value::bigint FROM settings WHERE key='owner'),
                 requested_by=(SELECT value::bigint FROM settings WHERE key='owner')
 WHERE chat_id IS NULL;
UPDATE outbox SET chat_id=(SELECT value::bigint FROM settings WHERE key='owner') WHERE chat_id IS NULL;
INSERT INTO schema_migrations(version) VALUES (2) ON CONFLICT DO NOTHING;
