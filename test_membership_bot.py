import sqlite3
import tempfile
import unittest
from pathlib import Path

from membership_bot import Config, MembershipBot, TelegramError, is_member


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.members = {}
        self.counter = 0
        self.fail_send = False

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "getChatMember":
            return self.members[(params["chat_id"], params["user_id"])]
        if method == "createChatInviteLink":
            self.counter += 1
            return {"invite_link": f"https://t.me/+test{self.counter}"}
        if method == "sendMessage" and self.fail_send:
            raise TelegramError(method, "test failure")
        return True


class MembershipBotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(Path(self.temp.name) / "test.sqlite3")
        self.api = FakeAPI()
        self.bot = MembershipBot(
            self.api,
            Config("fake-token", -1001, -1002, Path(self.temp.name) / "test.sqlite3"),
            self.db,
        )
        self.user = {"id": 42, "first_name": "A <B>", "is_bot": False}
        self.api.members[(-1001, 42)] = {"status": "member"}
        self.api.members[(-1002, 42)] = {"status": "left"}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def join_group(self):
        self.bot.handle_update(
            {
                "chat_member": {
                    "chat": {"id": -1001},
                    "old_chat_member": {"status": "left"},
                    "new_chat_member": {"status": "member", "user": self.user},
                }
            }
        )

    def join_request(self, user_id):
        self.bot.handle_update(
            {
                "chat_join_request": {
                    "chat": {"id": -1002},
                    "from": {"id": user_id},
                    "invite_link": {"invite_link": self.bot.user(42)["invite_link"]},
                }
            }
        )

    def test_group_join_sends_one_personal_approval_link(self):
        self.join_group()
        invite_call = next(params for method, params in self.api.calls if method == "createChatInviteLink")
        self.assertTrue(invite_call["creates_join_request"])
        self.assertNotIn("member_limit", invite_call)
        messages = [params for method, params in self.api.calls if method == "sendMessage"]
        self.assertEqual(len(messages), 1)
        self.assertIn("A &lt;B&gt;", messages[0]["text"])
        self.assertIn("https://t.me/+test1", messages[0]["text"])
        self.bot.check_user(42, force=True)
        self.assertEqual(sum(method == "sendMessage" for method, _ in self.api.calls), 1)

    def test_wrong_user_cannot_claim_visible_link(self):
        self.join_group()
        self.join_request(99)
        self.assertIn(("declineChatJoinRequest", {"chat_id": -1002, "user_id": 99}), self.api.calls)
        self.assertIsNotNone(self.bot.user(42)["invite_link"])
        self.assertFalse(any(method == "approveChatJoinRequest" for method, _ in self.api.calls))

    def test_intended_user_is_approved_and_link_revoked(self):
        self.join_group()
        self.join_request(42)
        self.assertIn(("approveChatJoinRequest", {"chat_id": -1002, "user_id": 42}), self.api.calls)
        self.assertIn(
            ("revokeChatInviteLink", {"chat_id": -1002, "invite_link": "https://t.me/+test1"}),
            self.api.calls,
        )
        self.assertIsNone(self.bot.user(42)["invite_link"])

    def test_group_departure_revokes_pending_link(self):
        self.join_group()
        self.bot.handle_update(
            {
                "chat_member": {
                    "chat": {"id": -1001},
                    "old_chat_member": {"status": "member"},
                    "new_chat_member": {"status": "left", "user": self.user},
                }
            }
        )
        self.assertEqual(self.bot.user(42)["active"], 0)
        self.assertIsNone(self.bot.user(42)["invite_link"])

    def test_existing_channel_member_gets_no_prompt(self):
        self.api.members[(-1002, 42)] = {"status": "member"}
        self.join_group()
        self.assertFalse(any(method == "createChatInviteLink" for method, _ in self.api.calls))

    def test_failed_post_revokes_link_and_can_retry(self):
        self.api.fail_send = True
        with self.assertRaises(TelegramError):
            self.join_group()
        self.assertIsNone(self.bot.user(42)["invite_link"])
        self.assertEqual(self.bot.user(42)["last_prompt_at"], 0)
        self.api.fail_send = False
        self.bot.check_user(42, force=True)
        self.assertEqual(self.api.counter, 2)
        self.assertEqual(self.bot.user(42)["invite_link"], "https://t.me/+test2")

    def test_observed_existing_member_gets_checked(self):
        self.bot.handle_update({"message": {"chat": {"id": -1001}, "from": self.user}})
        self.assertEqual(self.bot.user(42)["active"], 1)
        self.assertTrue(any(method == "sendMessage" for method, _ in self.api.calls))

    def test_repeated_request_after_approval_only_revokes(self):
        self.join_group()
        self.api.members[(-1002, 42)] = {"status": "member"}
        self.join_request(42)
        self.assertFalse(any(method == "approveChatJoinRequest" for method, _ in self.api.calls))
        self.assertIsNone(self.bot.user(42)["invite_link"])

    def test_restricted_member_is_active_only_when_still_in_chat(self):
        self.assertTrue(is_member({"status": "restricted", "is_member": True}))
        self.assertFalse(is_member({"status": "restricted", "is_member": False}))


if __name__ == "__main__":
    unittest.main()
