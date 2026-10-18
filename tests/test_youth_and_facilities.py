import unittest
import sqlite3
import tempfile
from pathlib import Path
import re
from app import (
    app, facility_effects,
    ensure_all_facilities, ensure_youth_table, get_or_regen_youth_squad,
    cltt_to_stars_10, cltt_stars_info, ALL_FACILITIES, YOUTH_POSITIONS_11
)
from auth import init_auth

class TestYouthAndFacilities(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original_config = app.config.copy()
        app.config.update(TESTING=True, DATA_DIR=self.temp.name, SECRET_KEY="test-session-key")
        self.client = app.test_client()
        self.connections = []
        self.register(self.client, "testuser", "FC Test")
        self.conn = self.save("testuser")

    def tearDown(self):
        for c in self.connections:
            try:
                c.close()
            except Exception:
                pass
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

    def register(self, client, username="testuser", club_name="FC Test"):
        self.token(client, "/register")
        result = self.post(client, "/register", {
            "username": username, "club_name": club_name,
            "password": "secure-password", "confirm_password": "secure-password",
        })
        self.assertEqual(result.status_code, 302)
        self.token(client, "/")
        return result

    def account(self, username="testuser"):
        conn = sqlite3.connect(self.root / "accounts.sqlite3")
        conn.row_factory = sqlite3.Row
        self.connections.append(conn)
        return conn.execute("SELECT * FROM user_account WHERE username=?", (username,)).fetchone()

    def save(self, username="testuser"):
        conn = sqlite3.connect(self.root / "games" / (self.account(username)["game_key"] + ".sqlite3"))
        self.connections.append(conn)
        conn.row_factory = sqlite3.Row
        return conn

    def test_youth_academy_11_players_18yo(self):
        youths = get_or_regen_youth_squad(self.conn)
        self.assertEqual(len(youths), 11)
        positions = [y["position"] for y in youths]
        # Check all 11 required positions exist
        for pos in ["GK", "DL", "DC", "DR", "DMC", "MC", "ML", "MR", "AMC", "AML", "ST"]:
            self.assertIn(pos, positions)
        # Check all are aged 18
        for y in youths:
            self.assertEqual(y["age"], 18)
            self.assertGreater(y["cltt"], 0)
            self.assertLessEqual(y["cltt"], 200)

    def test_10_stars_system_200_percent(self):
        # 20% per star, max 200% = 10 stars
        self.assertEqual(cltt_to_stars_10(20), "★")
        self.assertEqual(cltt_to_stars_10(40), "★★")
        self.assertEqual(cltt_to_stars_10(100), "★★★★★")
        self.assertEqual(cltt_to_stars_10(113), "★★★★★½")  # Matches screenshot 2: CLTT 113 -> 5.5 stars!
        self.assertEqual(cltt_to_stars_10(180), "★★★★★★★★★")  # Matches screenshot 2: Target 180 -> 9 stars!
        self.assertEqual(cltt_to_stars_10(200), "★★★★★★★★★★")
        self.assertEqual(cltt_to_stars_10(220), "★★★★★★★★★★")

        # Color progression / star info
        info = cltt_stars_info(113)
        self.assertEqual(info["cltt"], 113.0)
        self.assertEqual(info["full"], 5)
        self.assertTrue(info["half"])
        self.assertEqual(info["val"], 5.7)  # 113 / 20 = 5.65 -> 5.7

    def test_facility_effects_and_upgrades(self):
        eff1 = facility_effects(self.conn)
        self.assertIn("capacity", eff1)
        self.assertIn("parking_capacity", eff1)
        self.assertIn("parking_fee", eff1)
        self.assertIn("food_income_per_fan", eff1)
        self.assertIn("youth_base_cltt", eff1)
        self.assertIn("rest_packs_daily", eff1)
        self.assertIn("heal_packs_daily", eff1)

        # Level 1 values
        init_cap = eff1["capacity"]
        init_parking = eff1["parking_capacity"]
        init_food = eff1["food_income_per_fan"]

        # Upgrade facilities to level 2
        self.conn.execute("UPDATE facility SET level=2 WHERE name='stadium'")
        self.conn.execute("UPDATE facility SET level=2 WHERE name='parking'")
        self.conn.execute("UPDATE facility SET level=2 WHERE name='food_court'")
        self.conn.execute("UPDATE facility SET level=2 WHERE name='youth_academy'")
        self.conn.commit()

        eff2 = facility_effects(self.conn)
        self.assertGreater(eff2["capacity"], init_cap)
        self.assertGreater(eff2["parking_capacity"], init_parking)
        self.assertGreater(eff2["food_income_per_fan"], init_food)
        self.assertGreater(eff2["youth_base_cltt"], eff1["youth_base_cltt"])

    def test_youth_signing_to_senior_squad(self):
        youths = get_or_regen_youth_squad(self.conn)
        y = youths[0]
        y_id = y["id"]

        # Call youth sign endpoint via client
        resp = self.post(self.client, f"/youth/sign/{y_id}")
        self.assertEqual(resp.status_code, 302)

        # Verify player is now in squad_player
        promoted = self.conn.execute("SELECT * FROM squad_player WHERE name=? ORDER BY id DESC LIMIT 1", (y["name"],)).fetchone()
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted["age"], 18)
        self.assertEqual(promoted["position"], y["position"])

        # Verify youth record is marked signed
        yp_rec = self.conn.execute("SELECT is_signed FROM youth_player WHERE id=?", (y_id,)).fetchone()
        self.assertEqual(yp_rec["is_signed"], 1)

    def test_routes_youth_and_facilities(self):
        # Youth Academy GET route
        resp_youth = self.client.get("/youth")
        self.assertEqual(resp_youth.status_code, 200)
        youth_html = resp_youth.get_data(as_text=True)
        self.assertIn("HỌC VIỆN BÓNG ĐÁ TRẺ", youth_html)
        self.assertIn("HLV MỚI ĐÃ ĐẾN!", youth_html)

        # Facilities GET route (replaces black screen)
        resp_fac = self.client.get("/facilities")
        self.assertEqual(resp_fac.status_code, 200)
        fac_html = resp_fac.get_data(as_text=True)
        self.assertIn("KHUÔN VIÊN", fac_html)
        self.assertIn("Bãi Đỗ Xe", fac_html)
        self.assertIn("Khu Ẩm Thực", fac_html)
        self.assertIn("Sân Vận Động", fac_html)
        self.assertIn("Cơ Sở Đào Tạo Trẻ", fac_html)
        self.assertIn("T.Tâm Huấn Luyện", fac_html)
        self.assertIn("Khu Trị Thương", fac_html)
        self.assertIn("Fan CLB", fac_html)

if __name__ == "__main__":
    unittest.main()
