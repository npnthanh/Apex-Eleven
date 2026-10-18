import unittest
import sqlite3
import tempfile
from pathlib import Path
import re
from app import (
    app, compute_league_table, compute_player_leaderboards,
    compute_cup_bracket_data, compute_all_groups, generate_season
)

class CompetitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original_config = app.config.copy()
        app.config.update(TESTING=True, DATA_DIR=self.temp.name, SECRET_KEY="test-session-key")
        self.client = app.test_client()
        self.connections = []
        self.register(self.client, "manager1", "Bayern FC")
        self.conn = self.save("manager1")

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

    def register(self, client, username="manager1", club_name="Bayern FC"):
        self.token(client, "/register")
        result = self.post(client, "/register", {
            "username": username, "club_name": club_name,
            "password": "secure-password", "confirm_password": "secure-password",
        })
        self.assertEqual(result.status_code, 302)
        self.token(client, "/")
        return result

    def account(self, username="manager1"):
        conn = sqlite3.connect(self.root / "accounts.sqlite3")
        conn.row_factory = sqlite3.Row
        self.connections.append(conn)
        return conn.execute("SELECT * FROM user_account WHERE username=?", (username,)).fetchone()

    def save(self, username="manager1"):
        conn = sqlite3.connect(self.root / "games" / (self.account(username)["game_key"] + ".sqlite3"))
        self.connections.append(conn)
        conn.row_factory = sqlite3.Row
        return conn

    def test_national_league_20_teams_and_rules(self):
        """Test Giải Vô Địch Quốc Gia: 20 đội, Top 4 C1, Top 5-8 C2, Top 18-20 xuống hạng."""
        tbl = compute_league_table(self.conn)
        self.assertEqual(len(tbl), 20, "Giải VĐQG phải có đúng 20 đội (1 người chơi + 19 máy)")

        # Check promotion / relegation zones
        for r in tbl:
            if r["rank"] <= 4:
                self.assertEqual(r["zone"], "c1", f"Top {r['rank']} phải có suất C1")
            elif 5 <= r["rank"] <= 8:
                self.assertEqual(r["zone"], "c2", f"Hạng {r['rank']} phải có suất C2")
            elif r["rank"] >= 18:
                self.assertEqual(r["zone"], "relegation", f"Hạng {r['rank']} phải là khu vực xuống hạng")

        # Check render
        res = self.client.get("/competitions?tab=standings&comp=League")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("te-league-table-card", html)
        self.assertIn("zone-c1", html)
        self.assertIn("zone-c2", html)
        self.assertIn("form-pill", html)

    def test_champions_and_europa_groups(self):
        """Test C1 (Champions League) có 8 bảng đấu và Top 2 vào knockout."""
        groups = compute_all_groups(self.conn, "Champions")
        self.assertEqual(len(groups), 8, "C1 phải có đúng 8 bảng A-H")
        for g_name, g_rows in groups.items():
            self.assertEqual(len(g_rows), 4, f"Bảng {g_name} phải có đúng 4 đội")

        res = self.client.get("/competitions?tab=standings&comp=Champions")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("groups-grid-layout", html)
        self.assertIn("Bảng A", html)
        self.assertIn("Bảng H", html)

    def test_apex_cup_64_teams_two_legged(self):
        """Test Cúp Quốc Gia Apex: 64 đội, 32 cặp đấu loại trực tiếp 2 lượt (Lượt đi - Lượt về)."""
        rounds = compute_cup_bracket_data(self.conn)
        self.assertTrue(len(rounds) >= 1, "Phải có dữ liệu vòng đấu Cúp Quốc Gia Apex")
        r1 = rounds[0]
        self.assertEqual(len(r1["ties"]), 32, "Vòng 64 phải có đúng 32 cặp đấu")
        for tie in r1["ties"]:
            self.assertIsNotNone(tie["leg1"], "Phải có trận lượt 1")
            self.assertIsNotNone(tie["leg2"], "Phải có trận lượt 2")

        res = self.client.get("/competitions?tab=standings&comp=ApexCup")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("cup-tie-card", html)
        self.assertIn("Lượt 1", html)
        self.assertIn("Lượt 2", html)

    def test_player_stats_leaderboards(self):
        """Test hệ thống chỉ số cầu thủ: Vua phá lưới, Xếp hạng MVP, Kiến tạo theo Screenshot 2."""
        boards = compute_player_leaderboards(self.conn)
        self.assertIn("scorers", boards)
        self.assertIn("ratings", boards)
        self.assertIn("assists", boards)

        self.assertEqual(boards["scorers"], [], "Không được có bàn thắng mẫu trước trận đầu tiên")
        self.assertEqual(boards["ratings"], [], "Chưa thi đấu thì chưa có điểm MVP")
        self.assertEqual(boards["assists"], [], "Chưa thi đấu thì chưa có kiến tạo")

        res = self.client.get("/competitions?tab=stats")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("BÀN THẮNG", html)
        self.assertIn("XẾP HẠNG", html)
        self.assertIn("KIẾN TẠO", html)
        self.assertNotIn('class="hero-top1-block"', html)

if __name__ == "__main__":
    unittest.main()
