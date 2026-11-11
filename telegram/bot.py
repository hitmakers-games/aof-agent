"""Private Telegram transport with durable incoming messages and outgoing reports."""
import argparse
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from telegram.api import Telegram, TelegramError
from telegram.storage import connect, setting, put, queue_report
from database.errors import StorageError
from gateway.commands import dispatch
from gateway import access
from telegram.updates import handle_update
from telegram.delivery import deliver_one


STATUS = "Telegram is connected. The AI worker is not connected yet."


def engine_ready(db, now=None):
    return (setting(db, "engine_state") in ("chat", "gateway")
            and (time.time() if now is None else now) - float(setting(db, "engine_heartbeat", "0")) < 180)


def status_text(db):
    if engine_ready(db):
        return "Developer gateway connected. Use /help for tasks and stand diagnostics. Coding execution is not enabled yet."
    return STATUS


def refresh_typing(db, api, next_refresh=0, now=None):
    people = db.execute("""SELECT DISTINCT COALESCE(chat_id,?) FROM inbox
                           WHERE status IN ('queued','running','cancelling') LIMIT 5""",
                        (setting(db,'owner'),)).fetchall()
    if not people or not engine_ready(db):
        return 0
    now = time.monotonic() if now is None else now
    if now < next_refresh:
        return next_refresh
    delay = 4
    for (actor,) in people:
        if actor is None or not access.allowed(db,int(actor),'operator'):
            continue
        try:
            api.call('sendChatAction',chat_id=int(actor),action='typing',_timeout=3)
        except TelegramError as error:
            delay=max(delay,error.retry_after)
    return now+delay


def serve(state, secret):
    Path("/tmp/telegram-ready").unlink(missing_ok=True)
    api = Telegram((secret / "token").read_text().strip())
    pairing_code = (secret / "pairing-code").read_text().strip()
    expires = int((secret / "pairing-expires-at").read_text().strip())
    db = connect(state / "telegram.sqlite3")
    me = api.call("getMe")
    if not me.get("is_bot"):
        raise ValueError("Telegram account is not a bot")
    previous = setting(db, "bot_id")
    if previous is not None and previous != str(me["id"]):
        raise ValueError("This state belongs to a different bot")
    if api.call("getWebhookInfo").get("url"):
        raise ValueError("Bot already has a webhook; refusing to take it over")
    with db:
        put(db, "bot_id", me["id"])
    Path("/tmp/bot-identity.json").write_text(json.dumps({"username": me["username"]}))
    Path("/tmp/telegram-ready").touch()
    print("Telegram connected; private-chat pairing required" if not setting(db, "owner")
          else "Telegram connected; owner pairing restored", flush=True)
    notices = {}
    next_typing = 0
    while True:
        try:
            delivered = deliver_one(db, api)
            if delivered:
                time.sleep(1.1)
            next_typing = refresh_typing(db, api, next_typing)
            updates = api.call("getUpdates", offset=int(setting(db, "offset", "0")),
                               timeout=0 if delivered else 2, limit=20,
                               allowed_updates=["message", "callback_query"])
            for update in updates:
                notice = handle_update(db, update, pairing_code, expires)
                if notice and 'callback_query_id' in notice:
                    try:
                        api.call('answerCallbackQuery', **notice)
                    except TelegramError:
                        pass
                    continue
                if notice and time.monotonic() - notices.get(notice["chat_id"], -60) >= 30:
                    if len(notices) >= 1000:
                        notices.clear()
                    notices[notice["chat_id"]] = time.monotonic()
                    try:
                        api.call("sendMessage", **notice)
                    except TelegramError:
                        pass
            next_typing = refresh_typing(db, api, next_typing)
            Path("/tmp/telegram-ready").touch()
        except TelegramError as error:
            if error.code in (401, 403, 409):
                raise
            print("Telegram temporarily unavailable; retrying", flush=True)
            time.sleep(error.retry_after)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["serve", "report"])
    parser.add_argument("--state-dir", type=Path, default=Path("/state"))
    parser.add_argument("--secret-dir", type=Path, default=Path("/run/secrets/telegram"))
    options = parser.parse_args()
    try:
        options.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if options.action == "report":
            text = sys.stdin.read(128 * 1024 + 1)
            if len(text) > 128 * 1024:
                raise ValueError("Report exceeds the input limit")
            with connect(options.state_dir / "telegram.sqlite3") as db:
                if setting(db, "owner") is None:
                    raise ValueError("Telegram owner is not paired")
                queue_report(db, text)
            print("Report queued")
        else:
            with (options.state_dir / "telegram.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                serve(options.state_dir, options.secret_dir)
    except (OSError, ValueError, sqlite3.Error, StorageError, TelegramError):
        # Never include exception URLs, tokens, or message text in pod logs.
        print("Telegram service failed; check configuration and bot availability", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
