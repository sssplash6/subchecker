#!/usr/bin/env python3
"""Keep observed group members invited to a private Telegram channel."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger("membership_bot")
ACTIVE_STATUSES = {"creator", "administrator", "member"}
ALLOWED_UPDATES = ["message", "chat_member", "chat_join_request"]
INVITE_LIFETIME_SECONDS = 24 * 60 * 60
REMINDER_INTERVAL_SECONDS = 24 * 60 * 60
CHECK_INTERVAL_SECONDS = 60 * 60
SCAN_INTERVAL_SECONDS = 6 * 60 * 60


class TelegramError(RuntimeError):
    def __init__(self, method: str, description: str, retry_after: int | None = None):
        super().__init__(f"{method}: {description}")
        self.retry_after = retry_after


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, **params: Any) -> Any:
        body = json.dumps(params).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + method,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except (ValueError, OSError):
                raise TelegramError(method, f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TelegramError(method, str(exc)) from exc
        if not payload.get("ok"):
            retry_after = payload.get("parameters", {}).get("retry_after")
            raise TelegramError(method, payload.get("description", "unknown error"), retry_after)
        return payload["result"]


@dataclass(frozen=True)
class Config:
    token: str
    group_id: int
    channel_id: int
    database: Path

    @classmethod
    def from_env(cls) -> Config:
        missing = [key for key in ("BOT_TOKEN", "GROUP_ID", "CHANNEL_ID") if not os.getenv(key)]
        if missing:
            raise ValueError("Missing environment variables: " + ", ".join(missing))
        group_id = int(os.environ["GROUP_ID"])
        channel_id = int(os.environ["CHANNEL_ID"])
        if group_id == channel_id:
            raise ValueError("GROUP_ID and CHANNEL_ID must differ")
        return cls(
            os.environ["BOT_TOKEN"],
            group_id,
            channel_id,
            Path(os.getenv("BOT_DB", "membership.sqlite3")),
        )


def is_member(member: dict[str, Any]) -> bool:
    status = member.get("status")
    return status in ACTIVE_STATUSES or (status == "restricted" and member.get("is_member") is True)


class MembershipBot:
    def __init__(self, api: TelegramAPI, config: Config, db: sqlite3.Connection):
        self.api = api
        self.config = config
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                last_checked_at INTEGER NOT NULL DEFAULT 0,
                last_prompt_at INTEGER NOT NULL DEFAULT 0,
                invite_link TEXT,
                invite_expires_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            """
        )

    def validate_access(self) -> None:
        bot_id = self.api.call("getMe")["id"]
        group = self.api.call("getChat", chat_id=self.config.group_id)
        channel = self.api.call("getChat", chat_id=self.config.channel_id)
        if group["type"] not in {"group", "supergroup"} or channel["type"] != "channel":
            raise ValueError("GROUP_ID must name a group and CHANNEL_ID a channel")
        for label, chat_id in (("group", self.config.group_id), ("channel", self.config.channel_id)):
            member = self.api.call("getChatMember", chat_id=chat_id, user_id=bot_id)
            if member["status"] not in {"administrator", "creator"}:
                raise ValueError(f"Bot must be an administrator in the {label}")
            if label == "channel" and not member.get("can_invite_users"):
                raise ValueError("Bot needs the channel's Invite Users administrator right")

    def state(self, key: str) -> int:
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else 0

    def set_state(self, key: str, value: int) -> None:
        self.db.execute(
            "INSERT INTO state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.commit()

    def user(self, user_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

    def remember(self, user: dict[str, Any], active: bool = True) -> None:
        self.db.execute(
            "INSERT INTO users(user_id, first_name, active) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET first_name = excluded.first_name, "
            "active = excluded.active",
            (user["id"], user.get("first_name") or "Member", int(active)),
        )
        self.db.commit()

    def clear_invite(self, user_id: int) -> None:
        self.db.execute(
            "UPDATE users SET invite_link = NULL, invite_expires_at = 0 WHERE user_id = ?",
            (user_id,),
        )
        self.db.commit()

    def revoke_invite(self, user_id: int) -> None:
        row = self.user(user_id)
        if row and row["invite_link"]:
            self.api.call(
                "revokeChatInviteLink",
                chat_id=self.config.channel_id,
                invite_link=row["invite_link"],
            )
            self.clear_invite(user_id)

    def check_user(self, user_id: int, force: bool = False) -> None:
        row = self.user(user_id)
        if not row or not row["active"]:
            return
        now = int(time.time())
        if not force and now - row["last_checked_at"] < CHECK_INTERVAL_SECONDS:
            return
        group_member = self.api.call("getChatMember", chat_id=self.config.group_id, user_id=user_id)
        if not is_member(group_member):
            self.db.execute("UPDATE users SET active = 0 WHERE user_id = ?", (user_id,))
            self.db.commit()
            self.revoke_invite(user_id)
            return
        channel_member = self.api.call("getChatMember", chat_id=self.config.channel_id, user_id=user_id)
        self.db.execute(
            "UPDATE users SET last_checked_at = ? WHERE user_id = ?", (now, user_id)
        )
        self.db.commit()
        if is_member(channel_member):
            self.revoke_invite(user_id)
            return
        row = self.user(user_id)
        if row["invite_link"] and row["invite_expires_at"] > now:
            return
        if now - row["last_prompt_at"] < REMINDER_INTERVAL_SECONDS:
            return
        self.revoke_invite(user_id)
        invite = self.api.call(
            "createChatInviteLink",
            chat_id=self.config.channel_id,
            name=f"employee-{user_id}"[:32],
            expire_date=now + INVITE_LIFETIME_SECONDS,
            creates_join_request=True,
        )
        link = invite["invite_link"]
        self.db.execute(
            "UPDATE users SET invite_link = ?, invite_expires_at = ?, last_prompt_at = ? "
            "WHERE user_id = ?",
            (link, now + INVITE_LIFETIME_SECONDS, now, user_id),
        )
        self.db.commit()
        name = html.escape(row["first_name"])
        try:
            self.api.call(
                "sendMessage",
                chat_id=self.config.group_id,
                text=f'<a href="tg://user?id={user_id}">{name}</a>, please join our channel: '
                f'<a href="{html.escape(link, quote=True)}">Join channel</a>. '
                "This link is for you and expires in 24 hours.",
                parse_mode="HTML",
            )
        except TelegramError:
            self.revoke_invite(user_id)
            self.db.execute(
                "UPDATE users SET last_prompt_at = 0 WHERE user_id = ?", (user_id,)
            )
            self.db.commit()
            raise
        LOG.info("Sent channel invitation to group for user %s", user_id)

    def handle_join_request(self, request: dict[str, Any]) -> None:
        if request["chat"]["id"] != self.config.channel_id:
            return
        invite = request.get("invite_link") or {}
        link = invite.get("invite_link")
        if not link:
            return
        row = self.db.execute(
            "SELECT * FROM users WHERE invite_link = ?", (link,)
        ).fetchone()
        if not row:
            return
        requester_id = request["from"]["id"]
        if requester_id != row["user_id"] or not row["active"]:
            self.api.call(
                "declineChatJoinRequest",
                chat_id=self.config.channel_id,
                user_id=requester_id,
            )
            return
        channel_member = self.api.call(
            "getChatMember", chat_id=self.config.channel_id, user_id=requester_id
        )
        if is_member(channel_member):
            self.revoke_invite(requester_id)
            return
        group_member = self.api.call(
            "getChatMember", chat_id=self.config.group_id, user_id=requester_id
        )
        if not is_member(group_member):
            self.api.call(
                "declineChatJoinRequest",
                chat_id=self.config.channel_id,
                user_id=requester_id,
            )
            self.db.execute("UPDATE users SET active = 0 WHERE user_id = ?", (requester_id,))
            self.db.commit()
            self.revoke_invite(requester_id)
            return
        self.api.call(
            "approveChatJoinRequest",
            chat_id=self.config.channel_id,
            user_id=requester_id,
        )
        self.revoke_invite(requester_id)
        LOG.info("Approved channel join request for user %s", requester_id)

    def handle_update(self, update: dict[str, Any]) -> None:
        if "chat_join_request" in update:
            self.handle_join_request(update["chat_join_request"])
            return
        if "chat_member" in update:
            change = update["chat_member"]
            chat_id = change["chat"]["id"]
            user = change["new_chat_member"]["user"]
            if user.get("is_bot"):
                return
            new_active = is_member(change["new_chat_member"])
            old_active = is_member(change["old_chat_member"])
            if chat_id == self.config.group_id:
                if new_active and not old_active:
                    self.remember(user)
                    self.check_user(user["id"], force=True)
                elif old_active and not new_active and self.user(user["id"]):
                    self.db.execute("UPDATE users SET active = 0 WHERE user_id = ?", (user["id"],))
                    self.db.commit()
                    self.revoke_invite(user["id"])
            elif chat_id == self.config.channel_id and self.user(user["id"]):
                if new_active:
                    self.revoke_invite(user["id"])
                elif old_active and not new_active:
                    self.check_user(user["id"], force=True)
            return
        message = update.get("message") or {}
        if message.get("chat", {}).get("id") != self.config.group_id:
            return
        for joined_user in message.get("new_chat_members", []):
            if not joined_user.get("is_bot"):
                self.remember(joined_user)
                self.check_user(joined_user["id"], force=True)
        sender = message.get("from")
        if sender and not sender.get("is_bot"):
            self.remember(sender)
            self.check_user(sender["id"])

    def scan(self) -> None:
        user_ids = [
            row["user_id"]
            for row in self.db.execute("SELECT user_id FROM users WHERE active = 1")
        ]
        for user_id in user_ids:
            try:
                self.check_user(user_id, force=True)
            except TelegramError:
                LOG.exception("Could not check user %s", user_id)
        self.set_state("last_scan_at", int(time.time()))

    def run(self) -> None:
        while True:
            try:
                if time.time() - self.state("last_scan_at") >= SCAN_INTERVAL_SECONDS:
                    self.scan()
                offset = self.state("next_update_id")
                updates = self.api.call(
                    "getUpdates",
                    offset=offset,
                    timeout=25,
                    allowed_updates=ALLOWED_UPDATES,
                )
                for update in updates:
                    self.handle_update(update)
                    self.set_state("next_update_id", update["update_id"] + 1)
            except (TelegramError, urllib.error.URLError) as exc:
                LOG.error("Telegram API error: %s", exc)
                time.sleep(min(getattr(exc, "retry_after", None) or 5, 60))


def discover(api: TelegramAPI) -> None:
    """Print chat IDs observed in a short polling session during setup."""
    print("Watching for group messages and channel posts. Press Ctrl+C to stop.", flush=True)
    offset = None
    while True:
        updates = api.call(
            "getUpdates",
            offset=offset,
            timeout=25,
            allowed_updates=["message", "channel_post", "chat_member"],
        )
        for update in updates:
            offset = update["update_id"] + 1
            item = update.get("message") or update.get("channel_post") or update.get("chat_member")
            if item:
                chat = item["chat"]
                print(f'{chat.get("type")}: {chat["id"]} ({chat.get("title", "")})', flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discover", action="store_true", help="show chat IDs during setup")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("BOT_TOKEN")
    if not token:
        parser.error("BOT_TOKEN is required")
    api = TelegramAPI(token)
    try:
        if args.discover:
            discover(api)
        else:
            config = Config.from_env()
            config.database.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(config.database) as db:
                bot = MembershipBot(api, config, db)
                bot.validate_access()
                LOG.info("Starting membership checks for group %s and channel %s", config.group_id, config.channel_id)
                bot.run()
    except (ValueError, TelegramError) as exc:
        LOG.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
