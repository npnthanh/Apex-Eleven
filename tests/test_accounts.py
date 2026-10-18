import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from werkzeug.security import check_password_hash

from app import app, generate_season
from auth import init_auth


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original_config = app.config.copy()
        app.config.update(TESTING=True, DATA_DIR=self.temp.name, SECRET_KEY="test-session-key")
        self.alice = app.test_client()
        self.bob = app.test_client()
        self.connections = []

    def tearDown(self):
        for conn in self.connections:
            conn.close()
        app.config.clear()
        app.config.update(self.original_config)
        self.temp.cleanup()

    def token(self, client, path="/login"):
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)

    def post(self, client, path, data=None):
        with client.session_transaction() as session:
            token = session["csrf_token"]
        return client.post(path, data={**(data or {}), "csrf_token": token})

    def register(self, client, username="alice", club_name="Sài Gòn FC"):
        self.token(client, "/register")
        result = self.post(client, "/register", {
            "username": username, "club_name": club_name,
            "password": "secure-password", "confirm_password": "secure-password",
        })
        self.assertEqual(result.status_code, 302)
        self.token(client, "/")
        return result

    def login(self, client, username="alice", password="secure-password", suffix=""):
        self.token(client)
        return self.post(client, "/login" + suffix, {"username": username, "password": password})

    def account(self, username="alice"):
        with closing(sqlite3.connect(self.root / "accounts.sqlite3")) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM user_account WHERE username=?", (username,)).fetchone()

    def save(self, username="alice"):
        conn = sqlite3.connect(self.root / "games" / (self.account(username)["game_key"] + ".sqlite3"))
        self.connections.append(conn)
        conn.row_factory = sqlite3.Row
        return conn

    def test_guests_cannot_read_or_change_game_and_static_does_not_seed(self):
        for rule in app.url_map.iter_rules():
            if rule.endpoint.startswith("auth.") or rule.endpoint == "static":
                continue
            path = re.sub(r"<int:\w+>", "1", rule.rule)
            path = path.replace("<name>", "stadium")
            method = "post" if "POST" in rule.methods else "get"
            response = getattr(self.alice, method)(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn("/login", response.location)
        response = self.alice.get("/static/style.css")
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertFalse((self.root / "games").exists())

    def test_registration_creates_private_save_and_hashes_password(self):
        self.register(self.alice, "ALICE")
        account = self.account()
        self.assertNotEqual(account["password_hash"], "secure-password")
        self.assertTrue(check_password_hash(account["password_hash"], "secure-password"))
        conn = self.save()
        club = conn.execute("SELECT * FROM club").fetchone()
        self.assertEqual(club["name"], "Sài Gòn FC")
        self.assertEqual(club["cash"], 2_000_000)
        self.assertIn(conn.execute("SELECT COUNT(*) FROM facility").fetchone()[0], (4, 7))
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM fixture").fetchone()[0], 0)

    def test_invalid_registration_does_not_create_account(self):
        self.token(self.alice, "/register")
        for data in [
            {"username": "a b", "password": "secure-password", "confirm_password": "secure-password", "club_name": "FC"},
            {"username": "alice", "password": "short", "confirm_password": "short", "club_name": "FC"},
            {"username": "alice", "password": "secure-password", "confirm_password": "different", "club_name": "FC"},
            {"username": "alice", "password": "secure-password", "confirm_password": "secure-password", "club_name": " "},
        ]:
            self.assertEqual(self.post(self.alice, "/register", data).status_code, 400)
        self.assertFalse((self.root / "games").exists())

    def test_initialization_failure_rolls_back_account_and_partial_save(self):
        self.token(self.alice, "/register")
        original_initializer = app.extensions["initialize_game"]

        def fail_after_seeding(**kwargs):
            original_initializer(**kwargs)
            raise RuntimeError("simulated initialization failure")

        with patch.dict(app.extensions, {"initialize_game": fail_after_seeding}):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                self.post(self.alice, "/register", {
                    "username": "alice", "club_name": "Alice FC",
                    "password": "secure-password", "confirm_password": "secure-password",
                })
        self.assertIsNone(self.account())
        self.assertEqual(list((self.root / "games").glob("*.sqlite3")), [])
        self.register(self.alice)

    def test_duplicate_username_is_case_insensitive(self):
        self.register(self.alice)
        self.token(self.bob, "/register")
        response = self.post(self.bob, "/register", {
            "username": "ALICE", "club_name": "Other FC",
            "password": "secure-password", "confirm_password": "secure-password",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("đã được sử dụng", response.get_data(as_text=True))
        self.assertEqual(len(list((self.root / "games").glob("*.sqlite3"))), 1)

    def test_login_logout_and_progress_survives_new_client(self):
        self.register(self.alice)
        self.post(self.alice, "/club/rename", {"new_name": "Persistent FC"})
        self.post(self.alice, "/advance_day")
        self.assertEqual(self.post(self.alice, "/logout").status_code, 302)
        self.assertEqual(self.alice.get("/").status_code, 302)
        fresh = app.test_client()
        self.assertEqual(self.login(fresh, "ALICE").status_code, 302)
        page = fresh.get("/").get_data(as_text=True)
        self.assertIn("Persistent FC", page)
        self.assertIn("Ngày 1", page)
        response = fresh.get("/")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("HttpOnly", response.headers.get("Set-Cookie", ""))
        self.assertIn("SameSite=Lax", response.headers.get("Set-Cookie", ""))

    def test_wrong_credentials_and_throttling(self):
        self.register(self.alice)
        for _ in range(5):
            self.assertEqual(self.login(self.bob, password="incorrect").status_code, 400)
        self.assertEqual(self.login(self.bob).status_code, 429)
        with closing(sqlite3.connect(self.root / "accounts.sqlite3")) as conn:
            conn.execute("UPDATE login_failure SET created_at=0")
            conn.commit()
        self.assertEqual(self.login(self.bob).status_code, 302)

    def test_all_post_forms_have_csrf_and_invalid_tokens_are_rejected(self):
        self.assertEqual(self.alice.post("/register", data={}).status_code, 400)
        self.register(self.alice)
        for path in ["/", "/squad", "/player/1", "/lineup", "/tactics", "/market", "/training", "/facilities", "/competitions", "/friendly"]:
            page = self.alice.get(path)
            self.assertEqual(page.status_code, 200, path)
            html = page.get_data(as_text=True)
            forms = re.findall(r"<form\b.*?</form>", html, re.DOTALL)
            self.assertTrue(forms, path)
            for form in forms:
                self.assertIn('name="csrf_token"', form, path)
        for endpoint in ["/logout", "/advance_day", "/reset_season", "/club/rename"]:
            for token in ["", "incorrect", "ký tự"]:
                self.assertEqual(self.alice.post(endpoint, data={"csrf_token": token}).status_code, 400)
        self.assertEqual(self.save().execute("SELECT season_day FROM club").fetchone()[0], 0)

    def test_login_rotates_csrf_and_does_not_redirect_to_external_site(self):
        self.register(self.alice)
        self.post(self.alice, "/logout")
        old_token = self.token(self.alice)
        result = self.post(self.alice, "/login?next=//evil.example", {"username": "alice", "password": "secure-password"})
        self.assertEqual(result.location, "/")
        self.assertNotEqual(self.token(self.alice, "/"), old_token)
        self.post(self.alice, "/logout")
        result = self.login(self.alice, suffix="?next=/squad")
        self.assertEqual(result.location, "/squad")

    def test_two_accounts_isolate_all_game_tables(self):
        self.register(self.alice, "alice", "Alice FC")
        self.register(self.bob, "bob", "Bob FC")
        self.assertNotEqual(self.account()["game_key"], self.account("bob")["game_key"])
        alice_db = self.save()
        bob_db = self.save("bob")
        tables = [row[0] for row in bob_db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'")]
        # Set a distinctive player ID that cannot be read or changed from Bob's save.
        alice_db.execute("UPDATE squad_player SET id=999 WHERE id=18")
        alice_db.commit()
        self.assertNotIn("Alice FC", self.bob.get("/player/999").get_data(as_text=True))
        self.post(self.bob, "/player/999/rename", {"new_name": "Stolen"})
        self.post(self.bob, "/sell/999")
        self.assertNotEqual(alice_db.execute("SELECT name FROM squad_player WHERE id=999").fetchone()[0], "Stolen")
        snapshot = {table: [tuple(row) for row in bob_db.execute(f"SELECT * FROM {table}")] for table in tables}
        for path, data in [
            ("/club/rename", {"new_name": "Alice United"}),
            ("/player/1/rename", {"new_name": "Alice Keeper"}),
            ("/lineup/autopick", {}),
            ("/tactics", {"formation": "4-3-3", "mentality": "attacking", "pressing": "high", "passing": "short"}),
            ("/training", {"focus": "fitness", "intensity": "light"}),
            ("/upgrade/stadium", {}),
            ("/friendly/play", {}),
            ("/advance_day", {}),
            ("/reset_season", {}),
        ]:
            self.assertEqual(self.post(self.alice, path, data).status_code, 302, path)
        for table in tables:
            self.assertEqual(snapshot[table], [tuple(row) for row in bob_db.execute(f"SELECT * FROM {table}")], table)

    def test_lineup_rejects_players_outside_current_save(self):
        self.register(self.alice)
        self.assertEqual(self.post(self.alice, "/lineup/save", {"slot_0": "999999"}).status_code, 400)
        self.assertEqual(self.post(self.alice, "/lineup/save", {"slot_0": "1", "slot_1": "1"}).status_code, 400)
        self.assertEqual(self.post(self.alice, "/lineup/save", {"slot_0": "1", "captain_slot": "999"}).status_code, 400)
        self.assertEqual(self.post(self.alice, "/lineup/save", {"slot_0": "1", "captain_slot": "0"}).status_code, 302)

    def test_new_season_reserves_each_generated_team_name(self):
        self.register(self.alice)
        generated = []

        def next_name(existing):
            self.assertTrue(set(generated) <= existing)
            name = f"Opponent {len(generated)}"
            generated.append(name)
            return name

        with patch("app.gen_team_name", side_effect=next_name):
            generate_season(self.save())
        self.assertIn(len(generated), (7, 19))

    def test_deleted_account_cannot_reuse_old_session(self):
        self.register(self.alice)
        with closing(sqlite3.connect(self.root / "accounts.sqlite3")) as conn:
            conn.execute("DELETE FROM user_account")
            conn.commit()
        self.assertEqual(self.alice.get("/").status_code, 302)
        with self.alice.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_legacy_import_preserves_source_and_backs_up_destination(self):
        self.register(self.alice)
        self.register(self.bob, "bob", "Old Save FC")
        source = self.root / "legacy.db"
        with closing(sqlite3.connect(source)) as backup:
            self.save("bob").backup(backup)
        before = source.read_bytes()
        runner = app.test_cli_runner()
        result = runner.invoke(args=["import-legacy", "--username", "alice", "--source", str(source)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.save().execute("SELECT name FROM club").fetchone()[0], "Old Save FC")
        self.assertEqual(source.read_bytes(), before)
        backups = list((self.root / "games").glob("*.backup-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute("SELECT name FROM club").fetchone()[0], "Sài Gòn FC")

    def test_invalid_legacy_source_does_not_replace_save(self):
        self.register(self.alice)
        source = self.root / "invalid.db"
        source.write_text("not a sqlite database")
        result = app.test_cli_runner().invoke(args=["import-legacy", "--username", "alice", "--source", str(source)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.save().execute("SELECT name FROM club").fetchone()[0], "Sài Gòn FC")

    def test_session_key_is_persistent_and_old_prototype_key_is_rejected(self):
        with patch.dict(os.environ, {"TOUCHLINE_DATA_DIR": str(self.root / "keys")}, clear=True):
            first = Flask("first")
            second = Flask("second")
            init_auth(first, lambda: None)
            init_auth(second, lambda: None)
        self.assertEqual(first.secret_key, second.secret_key)
        self.assertGreaterEqual(len(first.secret_key), 64)
        self.register(self.alice)
        original = Flask("old")
        original.secret_key = "touchline-boss-prototype"
        forged = original.session_interface.get_signing_serializer(original).dumps({"user_id": self.account()["id"]})
        self.bob.set_cookie("session", forged)
        self.assertEqual(self.bob.get("/").status_code, 302)


if __name__ == "__main__":
    unittest.main()
