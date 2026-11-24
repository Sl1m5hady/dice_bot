from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from telebot.apihelper import ApiTelegramException
from telebot.types import Message

from chat_store import ChatStore
from main import create_bot
from telegram_bridge import deliver_once, run_bridge
from web_app import create_app


GROUP = -100


def incoming(number, text, *, group=GROUP, is_bot=False):
    return Message.de_json({
        "message_id": number,
        "chat": {"id": group, "type": "private" if group > 0 else "supergroup"},
        "from": {"id": 2, "is_bot": is_bot, "first_name": "Бран"},
        "date": 1,
        "text": text,
    })


class TelegramBridgeTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "chat.sqlite3"
        self.store = ChatStore(self.path, default_target=GROUP)
        self.app = create_app({
            "WEB_ACCESS_CODE": "shared-code", "SECRET_KEY": "test-session-secret",
            "DATABASE": self.path, "GROUP_ID": GROUP,
        })
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.post("/api/session", json={"code": "shared-code"})
        self.bot = create_bot("123456:test_only", threaded=False, chat_store=self.store)

    def post(self, text, target=GROUP):
        return self.client.post(
            "/api/chat", json={"name": "Лира", "character_id": str(uuid4()), "text": text, "target_id": str(target)}
        )

    def test_web_text_uses_character_attribution_and_no_echo(self):
        self.assertEqual(self.post("Привет!").status_code, 201)
        with patch.object(self.bot, "send_message", return_value=SimpleNamespace(message_id=90)) as send:
            self.assertTrue(deliver_once(self.bot, self.store))
            self.assertFalse(deliver_once(self.bot, self.store))
        send.assert_called_once_with(GROUP, "Лира · веб\nПривет!", timeout=20)
        self.bot.process_new_messages([incoming(90, "Лира · веб\nПривет!", is_bot=True)])
        self.assertIsNone(self.store.add_telegram(GROUP, 90, "telegram:2", "Бран", "Повтор отправленного сообщения"))
        entry, = self.client.get("/api/chat").json()["messages"]
        self.assertEqual((entry["text"], entry["delivery"], entry["source"]), ("Привет!", "sent", "web"))

    def test_each_roll_is_calculated_once_including_replayed_updates(self):
        telegram_roll = incoming(41, "d20kh+5")
        with (
            patch("service.random.randint", side_effect=[1, 20, 3, 4]) as randint,
            patch.object(self.bot, "send_message", return_value=SimpleNamespace(message_id=99)) as send,
        ):
            self.bot.process_new_messages([incoming(40, "А теперь d6!")])
            self.bot.process_new_messages([telegram_roll])
            self.bot.process_new_messages([telegram_roll])
            self.assertEqual(self.post("2d6").status_code, 201)
            self.assertTrue(deliver_once(self.bot, self.store))
            self.assertTrue(deliver_once(self.bot, self.store))
        text, rolled, web_roll = self.client.get("/api/chat").json()["messages"]
        self.assertIsNone(text["total"])
        self.assertEqual(rolled["total"], "25")
        self.assertEqual(send.call_args_list[0].kwargs["reply_parameters"].message_id, 41)
        self.assertEqual(send.call_args_list[0].args[1], rolled["result_html"])
        self.assertEqual(send.call_args_list[1].args[1], "Лира · веб\n2d6\n" + web_roll["result_html"])
        self.assertEqual(randint.call_count, 4)

    def test_long_web_roll_takes_the_same_result_to_telegram_as_an_attachment(self):
        downloads = []

        def download(chat_id, file, **kwargs):
            self.assertEqual(chat_id, GROUP)
            self.assertIn("Итог броска: 1000", kwargs["caption"])
            downloads.append(file.file.read().decode("utf-8"))
            return SimpleNamespace(message_id=91)

        with patch("service.random.randint", return_value=1), patch.object(self.bot, "send_document", side_effect=download):
            entry_id = self.post("1000d6").json()["id"]
            self.assertTrue(deliver_once(self.bot, self.store))
        web = self.client.get(f"/api/chat/{entry_id}/result").text
        self.assertTrue(all(content.count("🎲1") == 1000 and "<b>1000</b>" in content for content in [web, *downloads]))
        self.assertEqual(len(downloads), 1)

    def test_replay_after_pruning_does_not_add_another_entry(self):
        with patch("chat_store.FEED_SIZE", 1):
            self.store.add_telegram(GROUP, 40, "telegram:2", "Бран", "Привет")
            self.store.add_telegram(GROUP, 41, "telegram:2", "Бран", "Второе сообщение")
            restarted = ChatStore(self.path, default_target=GROUP)
            self.assertIsNone(restarted.add_telegram(GROUP, 40, "telegram:2", "Бран", "Привет"))
        self.assertEqual([message["text"] for message in self.store.feed()[1]], ["Второе сообщение"])

    def test_retry_after_is_persisted_and_unknown_delivery_does_not_resend(self):
        entry_id = self.post("Привет!").json()["id"]
        self.post("До встречи!")
        failure = ApiTelegramException("sendMessage", Mock(), {
            "error_code": 429, "description": "Too many requests", "parameters": {"retry_after": 9},
        })
        with (
            patch("chat_store.time", return_value=100),
            patch("telegram_bridge.time", return_value=100),
            patch.object(self.bot, "send_message", side_effect=failure),
        ):
            self.assertTrue(deliver_once(self.bot, self.store))
        restarted = ChatStore(self.path, default_target=GROUP)
        self.assertIsNone(restarted.claim_delivery(now=108))
        self.assertEqual(restarted.claim_delivery(now=109)["id"], entry_id)
        restarted.recover_delivery()
        self.assertGreater(restarted.claim_delivery(now=110)["id"], entry_id)
        self.assertEqual(restarted.feed()[1][0]["delivery"], "uncertain")

    def test_another_group_is_not_mirrored(self):
        self.bot.process_new_messages([incoming(40, "Постороннее сообщение", group=-200)])
        self.assertEqual(self.store.feed(), (GROUP, []))

    def test_worker_switches_live_to_private_and_keeps_queued_messages_in_original_chat(self):
        self.assertEqual(self.post("Сообщение группы").status_code, 201)
        private = 123456
        self.store.set_target(private)
        with patch.object(self.bot, "send_message", return_value=SimpleNamespace(message_id=91)) as send:
            self.assertFalse(deliver_once(self.bot, self.store))
            self.bot.process_new_messages([incoming(10, "Входящее из лички", group=private)])
            self.assertEqual(self.post("Ответ для лички", private).status_code, 201)
            self.assertTrue(deliver_once(self.bot, self.store))
            self.store.set_target(GROUP)
            self.assertTrue(deliver_once(self.bot, self.store))
        self.assertEqual(send.call_args_list[0].args, (private, "Лира · веб\nОтвет для лички"))
        self.assertEqual(send.call_args_list[1].args, (GROUP, "Лира · веб\nСообщение группы"))

    def test_initially_disconnected_worker_picks_up_an_admin_change(self):
        private = 123456
        self.store.set_target(private)
        self.assertEqual(self.post("Ответ для лички", private).status_code, 201)
        self.store.set_target(None)
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        stop.wait.side_effect = lambda _: self.store.set_target(private)
        with patch.object(self.bot, "send_message", return_value=SimpleNamespace(message_id=91)) as send:
            run_bridge(self.bot, self.store, stop)
        send.assert_called_once_with(private, "Лира · веб\nОтвет для лички", timeout=20)
