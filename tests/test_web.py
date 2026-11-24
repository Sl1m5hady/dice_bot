from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from chat_store import FEED_SIZE
from dice_parser import MAX_EXPRESSION_LENGTH
from web_app import create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = {
            "WEB_ACCESS_CODE": "club-code",
            "WEB_ADMIN_CODE": "admin-code",
            "SECRET_KEY": "an independent session signing secret",
            "DATABASE": str(Path(directory.name) / "rolls.sqlite3"),
            "GROUP_ID": None,
        }
        self.app = create_app(self.config)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.character_id = str(uuid4())

    def login(self, client):
        return client.post("/api/session", json={"code": "club-code"})

    def message(self, client, name, text, target_id=None):
        return client.post(
            "/api/chat", json={"name": name, "text": text, "character_id": self.character_id, "target_id": target_id}
        )

    def test_gate_login_and_logout(self):
        self.assertIn("Кубомет", self.client.get("/").text)
        protected = (
            ("GET", "/api/chat"), ("POST", "/api/chat"),
            ("GET", "/api/chat/1/result"), ("GET", "/api/chat/1/text"),
        )
        for method, path in protected:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.client.request(method, path).status_code, 401)
        self.assertEqual(self.client.post("/api/session", json={"code": "wrong"}).status_code, 401)
        login = self.login(self.client)
        self.assertTrue(login.json()["authenticated"])
        self.assertIn("HttpOnly", login.headers["Set-Cookie"])
        self.assertIn("SameSite=strict", login.headers["Set-Cookie"])
        self.assertEqual(self.client.get("/api/chat").json(), {"messages": [], "target_id": None})
        self.assertEqual(self.client.delete("/api/session").json(), {"authenticated": False})
        self.assertEqual(self.client.get("/api/chat").status_code, 401)

    def test_two_sessions_share_text_and_rolls_and_preserve_the_original_name(self):
        with TestClient(self.app) as second:
            self.login(self.client)
            self.login(second)
            with patch("service.random.randint", side_effect=[1, 20]):
                self.assertEqual(self.message(self.client, "Лира", "d20kh+3").status_code, 201)
                self.assertEqual(self.message(self.client, "Новое имя", "Обсудим d6 позже.").status_code, 201)
            roll, message = second.get("/api/chat").json()["messages"]
            self.assertEqual((roll["name"], roll["text"], roll["total"]), ("Лира", "d20kh+3", "23"))
            self.assertIn("<s>🎲1</s>", roll["result_html"])
            self.assertEqual(roll["character_id"], self.character_id)
            self.assertEqual((message["name"], message["text"], message["total"]), ("Новое имя", "Обсудим d6 позже.", None))

    def test_formula_errors_are_private_and_never_saved(self):
        self.login(self.client)
        for text in ("d6/0", "d6+" + "0" * MAX_EXPRESSION_LENGTH):
            with self.subTest(formula=text[:30]):
                self.assertEqual(self.message(self.client, "Лира", text).status_code, 400)
        self.assertEqual(self.client.get("/api/chat").json()["messages"], [])

    def test_long_result_and_original_text_are_downloadable(self):
        self.login(self.client)
        repeated = (MAX_EXPRESSION_LENGTH - 2) // 2
        name = "<img src=x>"
        text = "d6" + "+1" * repeated + " — <script>текст</script>"
        with patch("service.random.randint", return_value=5):
            self.assertEqual(self.message(self.client, name, text).status_code, 201)
        row, = self.client.get("/api/chat").json()["messages"]
        self.assertEqual(row["name"], name)
        self.assertEqual(row["total"], str(repeated + 5))
        self.assertTrue(row["has_download"])
        self.assertTrue(row["text_truncated"])
        self.assertIsNone(row["result_html"])
        path = f"/api/chat/{row['id']}"
        self.assertEqual(self.client.get(path + "/text").json()["text"], text)
        response = self.client.get(path + "/result")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("🎲5" + " + 1" * repeated, response.text)
        self.assertIn("&lt;script&gt;текст&lt;/script&gt;", response.text)
        self.assertNotIn("<script>", response.text)
        with TestClient(self.app) as guest:
            self.assertEqual(guest.get(path + "/result").status_code, 401)

    def test_feed_survives_restart_and_displays_the_last_100(self):
        self.login(self.client)
        for index in range(FEED_SIZE + 1):
            self.assertEqual(self.message(self.client, f"Персонаж {index}", "Привет!").status_code, 201)
        with TestClient(create_app(self.config)) as restarted:
            self.login(restarted)
            rows = restarted.get("/api/chat").json()["messages"]
            self.assertEqual(len(rows), FEED_SIZE)
            self.assertEqual((rows[0]["name"], rows[-1]["name"]), ("Персонаж 1", "Персонаж 100"))
            self.assertEqual(restarted.get("/api/chat/1/text").status_code, 404)

    def test_previous_web_history_is_migrated_once_and_not_queued(self):
        path = Path(self.config["DATABASE"]).with_name("legacy.sqlite3")
        with sqlite3.connect(path) as db:
            db.execute("""
                CREATE TABLE rolls (
                    id INTEGER PRIMARY KEY, character_id TEXT, character_name TEXT,
                    created_at TEXT, formula TEXT, total TEXT, rendered TEXT
                )
            """)
            db.execute("INSERT INTO rolls VALUES (1, ?, 'Лира', '2026-11-24T12:00:00+00:00', 'd6', '4', '4')",
                       (self.character_id,))
        config = dict(self.config, DATABASE=path, GROUP_ID=-100)
        first = create_app(config)
        self.assertIsNone(first.state.chat_store.claim_delivery())
        self.assertEqual(first.state.chat_store.feed(), (-100, []))
        first.state.chat_store.set_target(None)
        _, rows = create_app(config).state.chat_store.feed()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["text"], rows[0]["total"], rows[0]["delivery"]), ("d6", "4", None))

    def test_admin_can_switch_to_a_private_chat_without_exposing_previous_messages(self):
        settings = "/api/admin/chat"
        self.assertEqual(self.client.put(settings, json={"target_id": "123456"}).status_code, 401)
        self.login(self.client)
        self.assertEqual(self.client.put(settings, json={"target_id": "123456"}).status_code, 403)
        self.assertEqual(self.client.post("/api/admin/session", json={"code": "club-code"}).status_code, 403)
        self.assertEqual(self.client.post("/api/admin/session", json={"code": "admin-code"}).status_code, 200)
        self.assertEqual(self.client.put(settings, json={"target_id": "0"}).status_code, 400)
        local_id = self.message(self.client, "Лира", "Только локально").json()["id"]
        self.assertEqual(self.client.put(settings, json={"target_id": "123456"}).json(), {"target_id": "123456"})
        self.assertEqual(self.client.get("/api/chat").json(), {"messages": [], "target_id": "123456"})
        self.assertEqual(self.client.get(f"/api/chat/{local_id}/text").status_code, 404)
        self.assertEqual(self.message(self.client, "Лира", "Старый черновик").status_code, 409)
        self.assertEqual(self.message(self.client, "Лира", "Тест лички", "123456").status_code, 201)
        self.assertEqual(create_app(self.config).state.chat_store.active_target(), 123456)
        self.assertEqual(self.client.put(settings, json={"target_id": None}).json(), {"target_id": None})
        self.assertEqual([entry["text"] for entry in self.client.get("/api/chat").json()["messages"]], ["Только локально"])
        self.client.delete("/api/session")
        self.login(self.client)
        self.assertEqual(self.client.get(settings).status_code, 403)
