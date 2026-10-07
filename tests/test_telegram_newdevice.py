"""Tests for the Telegram side of new-device naming (buttons, /unnamed, name replies).

Fake update/query/context objects are used, so nothing is sent to Telegram.
Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_db  # noqa: E402
import bt_newdevice as nd  # noqa: E402
import bt_telegram  # noqa: E402

MAC = "8E:44:51:E7:87:9D"


class Replies:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.sent: list[tuple[str, dict]] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.sent.append((text, kwargs))


def message_update(text: str, replies: Replies | None = None):
    replies = replies or Replies(text)
    replies.text = text
    return SimpleNamespace(message=replies, effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock())), replies


def callback_update(data: str):
    query = SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=42),
        message=SimpleNamespace(chat=SimpleNamespace(id=42), message_id=7),
        answer=mock.AsyncMock(), edit_message_text=mock.AsyncMock(),
    )
    return SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=42)), query


class Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bt_radar.db"
        bt_db.init_db(self.db)
        self.conn = bt_db.get_connection(self.db)
        self.addCleanup(self.conn.close)
        self.context = SimpleNamespace(bot_data={}, args=[])
        for target, value in (("_get_db_path", lambda: self.db), ("_is_authorized", lambda _id: True)):
            patcher = mock.patch.object(bt_telegram, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def add(self, mac: str = MAC, host: str = "iPhone.lan", **cols) -> None:
        bt_db.upsert_device(self.conn, mac, advertised_name=host, scan_type="WiFi", state="DETECTED",
                            ip_address="192.168.1.158")
        for c, v in cols.items():
            self.conn.execute(f"UPDATE devices SET {c} = ? WHERE mac_address = ?", (v, mac))
        self.conn.commit()

    def device(self, mac: str = MAC) -> dict:
        return bt_db.get_device(self.conn, mac)


class UnnamedCommandTests(Case):
    async def test_nothing_to_name(self) -> None:
        update, replies = message_update("/unnamed")
        await bt_telegram._cmd_unnamed(update, self.context)
        self.assertEqual(len(replies.sent), 1)
        self.assertIn("has a name", replies.sent[0][0])

    async def test_lists_each_unnamed_device_with_buttons(self) -> None:
        self.add(MAC)
        self.add("AA:BB:CC:00:00:02", "Mac.lan")
        self.add("AA:BB:CC:00:00:03", "Dryer.lan", friendly_name="Dryer")
        update, replies = message_update("/unnamed")
        await bt_telegram._cmd_unnamed(update, self.context)
        self.assertIn("2 connected device(s) without a name", replies.sent[0][0])
        self.assertEqual(len(replies.sent), 3)  # header + one per device
        for text, kwargs in replies.sent[1:]:
            self.assertIn("New device on the WiFi", text)
            self.assertEqual(kwargs["parse_mode"], "HTML")
            self.assertIsNotNone(kwargs["reply_markup"])

    async def test_caps_the_list(self) -> None:
        for i in range(12):
            self.add(f"AA:BB:CC:00:01:{i:02X}")
        update, replies = message_update("/unnamed")
        await bt_telegram._cmd_unnamed(update, self.context)
        self.assertIn("8+", replies.sent[0][0])
        self.assertEqual(len(replies.sent), 9)

    async def test_ignores_unauthorised_chats(self) -> None:
        self.add()
        update, replies = message_update("/unnamed")
        with mock.patch.object(bt_telegram, "_is_authorized", lambda _id: False):
            await bt_telegram._cmd_unnamed(update, self.context)
        self.assertEqual(replies.sent, [])


class ButtonTests(Case):
    async def test_role_button_asks_for_a_name_and_changes_nothing_yet(self) -> None:
        self.add()
        update, query = callback_update(nd.callback_data("phone", MAC))
        await bt_telegram._on_newdevice_callback(update, self.context)
        prompt = query.edit_message_text.await_args.args[0]
        self.assertIn("Reply with a name", prompt)
        self.assertIsNone(self.device()["friendly_name"])
        self.assertEqual(self.context.bot_data["nd_pending"]["42"]["mac"], MAC)

    async def test_ignore_hides_immediately(self) -> None:
        self.add()
        update, query = callback_update(nd.callback_data("ignore", MAC))
        await bt_telegram._on_newdevice_callback(update, self.context)
        self.assertEqual(self.device()["is_hidden"], 1)
        self.assertIn("Ignored", query.edit_message_text.await_args.args[0])
        self.assertNotIn("42", self.context.bot_data.get("nd_pending", {}))

    async def test_already_named_device_is_left_alone(self) -> None:
        self.add(friendly_name="Richard's iPhone (WiFi)")
        update, query = callback_update(nd.callback_data("laptop", MAC))
        await bt_telegram._on_newdevice_callback(update, self.context)
        self.assertIn("Already named", query.edit_message_text.await_args.args[0])
        self.assertEqual(self.device()["friendly_name"], "Richard's iPhone (WiFi)")
        self.assertNotIn("42", self.context.bot_data.get("nd_pending", {}))

    async def test_device_deleted_since_the_alert(self) -> None:
        update, query = callback_update(nd.callback_data("phone", MAC))
        await bt_telegram._on_newdevice_callback(update, self.context)
        self.assertIn("no longer in the list", query.edit_message_text.await_args.args[0])

    async def test_garbage_payload_does_nothing(self) -> None:
        self.add()
        update, query = callback_update("nd:phone:not-a-mac")
        await bt_telegram._on_newdevice_callback(update, self.context)
        query.edit_message_text.assert_not_awaited()
        self.assertIsNone(self.device()["friendly_name"])

    async def test_unauthorised_tap_is_ignored(self) -> None:
        self.add()
        update, query = callback_update(nd.callback_data("ignore", MAC))
        with mock.patch.object(bt_telegram, "_is_authorized", lambda _id: False):
            await bt_telegram._on_newdevice_callback(update, self.context)
        query.edit_message_text.assert_not_awaited()
        self.assertEqual(self.device()["is_hidden"], 0)


class NamingReplyTests(Case):
    async def tap(self, action: str = "phone") -> None:
        update, _ = callback_update(nd.callback_data(action, MAC))
        await bt_telegram._on_newdevice_callback(update, self.context)

    async def reply(self, text: str):
        update, replies = message_update(text)
        consumed = await bt_telegram._maybe_handle_naming(update, self.context)
        return consumed, replies

    async def test_the_next_message_becomes_the_name(self) -> None:
        self.add()
        await self.tap("phone")
        consumed, replies = await self.reply("Mathilde's iPhone")
        self.assertTrue(consumed)
        d = self.device()
        self.assertEqual((d["friendly_name"], d["device_type"], d["role"], d["is_watchlisted"], d["is_notify"]),
                         ("Mathilde's iPhone", "Phone", "phone", 1, 1))
        self.assertIn("Saved as", replies.sent[0][0])
        self.assertIn("notifications on", replies.sent[0][0])
        consumed_again, _ = await self.reply("what's the weather")
        self.assertFalse(consumed_again)  # back to normal chat

    async def test_laptop_reply_is_named_but_not_watched(self) -> None:
        self.add()
        await self.tap("laptop")
        await self.reply("Laura's MacBook Pro")
        d = self.device()
        self.assertEqual((d["friendly_name"], d["is_watchlisted"], d["is_notify"]), ("Laura's MacBook Pro", 0, 0))

    async def test_html_in_a_name_is_escaped_in_the_confirmation(self) -> None:
        self.add()
        await self.tap("home")
        _, replies = await self.reply("<b>x</b>")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", replies.sent[0][0])

    async def test_cancel(self) -> None:
        self.add()
        await self.tap()
        consumed, replies = await self.reply("cancel")
        self.assertTrue(consumed)
        self.assertIn("Cancelled", replies.sent[0][0])
        self.assertIsNone(self.device()["friendly_name"])
        self.assertFalse((await self.reply("hello"))[0])

    async def test_overlong_name_is_refused_and_still_waiting(self) -> None:
        self.add()
        await self.tap()
        consumed, replies = await self.reply("x" * 61)
        self.assertTrue(consumed)
        self.assertIn("too long", replies.sent[0][0])
        self.assertIsNone(self.device()["friendly_name"])
        consumed, _ = await self.reply("Short name")
        self.assertTrue(consumed)
        self.assertEqual(self.device()["friendly_name"], "Short name")

    async def test_expired_request_is_not_consumed(self) -> None:
        self.add()
        await self.tap()
        self.context.bot_data["nd_pending"]["42"]["at"] = time.time() - 901
        consumed, replies = await self.reply("hello there")
        self.assertFalse(consumed)
        self.assertEqual(replies.sent, [])
        self.assertIsNone(self.device()["friendly_name"])

    async def test_named_elsewhere_in_the_meantime(self) -> None:
        self.add()
        await self.tap()
        bt_db.update_device(self.conn, MAC, friendly_name="Dashboard name")
        _, replies = await self.reply("Telegram name")
        self.assertIn("already has a name", replies.sent[0][0])
        self.assertEqual(self.device()["friendly_name"], "Dashboard name")

    async def test_no_pending_request_means_normal_chat(self) -> None:
        self.assertFalse((await self.reply("hello"))[0])

    async def test_handle_message_routes_the_name_before_chat_or_presence(self) -> None:
        self.add()
        await self.tap()
        update, replies = message_update("Is Richard home")  # would normally be a presence query
        with mock.patch.object(bt_telegram, "answer_presence", mock.AsyncMock()) as presence, \
             mock.patch.object(bt_telegram.bt_search, "chat_with_search_async", mock.AsyncMock()) as chat:
            await bt_telegram._handle_message(update, self.context)
        presence.assert_not_awaited()
        chat.assert_not_awaited()
        self.assertEqual(self.device()["friendly_name"], "Is Richard home")


if __name__ == "__main__":
    unittest.main()
