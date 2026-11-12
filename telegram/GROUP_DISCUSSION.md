# Group discussion analysis

After deployment, an administrator enables analysis separately in each connected
group with `/listen@BotUsername on`; `off` restores mention-only replies.
The setting defaults to off. Existing groups are unchanged until enabled.

Incoming text from human participants is queued for discussion analysis, including
participants without a gateway role. The administrator who enabled the setting
sponsors this analysis; their admin permission is checked before enqueueing.
Commands without a mention and bot messages do not trigger analysis. Recent retained
messages (up to 30) provide context. Telegram privacy settings must allow delivery
of group messages; messages Telegram does not deliver cannot be analyzed.

The analysis model has no developer MCP tools, shell, or web access. It may produce
a short reply when the bot is discussed or implicitly addressed, or `[[SILENT]]`
to complete without delivery. Ambient work produces no queue notices or typing
indicator, and model failures remain silent. Normal queue limits still apply;
messages are retained but ambient analysis is skipped when the queue is full.
Explicit mentions retain the usual permission-checked development flow.

This feature does not add Telegram message deletion.
