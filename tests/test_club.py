import unittest
import sqlite3
import tempfile
from pathlib import Path
import re
from app import app

class ClubProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original_config = app.config.copy()
        app.config.update(TESTING=True, DATA_DIR=self.temp.name, SECRET_KEY="test-session-key")
        self.client = app.test_client()
        self.connections = []
        self.register(self.client, "manager1", "Bayern München")
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
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
        return match.group(1) if match else ""

    def post(self, client, path, data=None):
        with client.session_transaction() as session:
            token = session.get("csrf_token", "")
        return client.post(path, data={**(data or {}), "csrf_token": token}, follow_redirects=False)

    def register(self, client, username="manager1", club_name="Bayern München"):
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
        conn.row_factory = sqlite3.Row
        self.connections.append(conn)
        return conn

    def test_club_profile_overview_tab(self):
        """Kiểm tra tab Tổng quan với tủ áo đấu và bảng thông tin chi tiết CLB"""
        response = self.client.get("/club")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertIn("TỔNG QUAN", html)
        self.assertIn("Bayern München", html)
        self.assertIn("Áo sân nhà", html)
        self.assertIn("Áo sân khách", html)
        self.assertIn("Chi tiết về CLB", html)
        self.assertIn("Cấp độ của CLB", html)
        self.assertIn("Sân vận động", html)
        self.assertIn("Sức chứa", html)
        self.assertIn("Tên Fanclub", html)

    def test_club_profile_season_stats_tab(self):
        """Kiểm tra tab Thống kê mùa giải với 4 cúp và bảng thống kê kết quả"""
        response = self.client.get("/club?tab=muagiai")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertIn("THỐNG KÊ MÙA GIẢI", html)
        self.assertIn("CHUYÊN NGHIỆP", html)
        self.assertIn("GIẢI CN TÀI NĂNG", html)
        self.assertIn("CUP", html)
        self.assertIn("CÚP TUYỆT ĐỈNH", html)
        self.assertIn("THẮNG", html)
        self.assertIn("HÒA", html)
        self.assertIn("BẠI", html)
        self.assertIn("Bàn thắng được ghi", html)
        self.assertIn("Thắng đậm nhất", html)
        self.assertIn("Nhiều bàn thắng nhất", html)

    def test_club_profile_history_stats_tab(self):
        """Kiểm tra tab Thống kê lịch sử với phòng truyền thống 5 loại cúp"""
        response = self.client.get("/club?tab=lichsu")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertIn("THỐNG KÊ TỔNG THỂ", html)
        self.assertIn("CHUYÊN NGHIỆP", html)
        self.assertIn("GIẢI CN TÀI NĂNG", html)
        self.assertIn("GIẢI CNGHIỆP VÀNG", html)
        self.assertIn("CUP", html)
        self.assertIn("CÚP TUYỆT ĐỈNH", html)
        self.assertIn("Mạch bất bại dài nhất", html)

    def test_update_club_profile(self):
        """Kiểm tra chức năng chỉnh sửa thông tin CLB qua modal bút chì"""
        update_data = {
            "club_name": "FC Bayern München Pro",
            "manager_name": "HLV Thành Nhật",
            "stadium_name": "Allianz Arena Vip",
            "country": "Việt Nam",
            "country_flag": "🇻🇳",
            "city": "TP HaNoi",
            "fanclub_name": "Mia San Mia Official",
        }
        res = self.post(self.client, "/club/update_profile", update_data)
        self.assertEqual(res.status_code, 302)

        # Kiểm tra database đã cập nhật
        row = self.conn.execute("SELECT * FROM club WHERE id=1").fetchone()
        self.assertEqual(row["name"], "FC Bayern München Pro")
        self.assertEqual(row["manager_name"], "HLV Thành Nhật")
        self.assertEqual(row["stadium_name"], "Allianz Arena Vip")
        self.assertEqual(row["city"], "TP HaNoi")
        self.assertEqual(row["fanclub_name"], "Mia San Mia Official")

        # Kiểm tra trang GET /club hiển thị thông tin mới
        view_res = self.client.get("/club")
        self.assertEqual(view_res.status_code, 200)
        view_html = view_res.get_data(as_text=True)
        self.assertIn("FC Bayern München Pro", view_html)
        self.assertIn("HLV Thành Nhật", view_html)
        self.assertIn("Allianz Arena Vip", view_html)
        self.assertIn("Mia San Mia Official", view_html)

    def test_settings_modal_rendered_in_base(self):
        """Kiểm tra modal Thiết Lập có trong base.html, không còn nút giao hữu/trợ lý ở sidebar"""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        # Kiểm tra có nút thiết lập ⚙️ và modal
        self.assertIn("settingsModalBackdrop", html)
        self.assertIn("THIẾT LẬP & TÀI KHOẢN", html)
        self.assertIn("openSettingsModal()", html)
        self.assertIn("ĐỔI MẬT KHẨU", html)
        self.assertIn("ĐĂNG XUẤT", html)

        # Kiểm tra không còn nút giao hữu hay trợ lý ở thanh sidebar dock bên trái
        sidebar_match = re.search(r'<aside class="sidebar-dock"[^>]*>(.*?)</aside>', html, re.DOTALL)
        self.assertTrue(sidebar_match is not None)
        sidebar_html = sidebar_match.group(1)
        self.assertNotIn('href="/friendly"', sidebar_html)
        self.assertNotIn('openAssistantModal', sidebar_html)
        self.assertIn('openSettingsModal()', sidebar_html)

    def test_change_password_flow(self):
        """Kiểm tra luồng đổi mật khẩu tài khoản và đăng nhập lại bằng mật khẩu mới"""
        # 1. Sai mật khẩu hiện tại
        res = self.post(self.client, "/change_password", {
            "current_password": "wrong-password",
            "new_password": "new-secure-password",
            "confirm_password": "new-secure-password",
        })
        self.assertEqual(res.status_code, 302)

        # 2. Mật khẩu xác nhận không khớp
        res = self.post(self.client, "/change_password", {
            "current_password": "secure-password",
            "new_password": "new-secure-password",
            "confirm_password": "different-password",
        })
        self.assertEqual(res.status_code, 302)

        # 3. Đổi mật khẩu thành công
        res = self.post(self.client, "/change_password", {
            "current_password": "secure-password",
            "new_password": "brand-new-password",
            "confirm_password": "brand-new-password",
        })
        self.assertEqual(res.status_code, 302)

        # 4. Đăng xuất
        logout_res = self.post(self.client, "/logout")
        self.assertEqual(logout_res.status_code, 302)

        # 5. Đăng nhập lại bằng mật khẩu cũ phải thất bại
        self.token(self.client, "/login")
        fail_login = self.post(self.client, "/login", {
            "username": "manager1",
            "password": "secure-password",
        })
        self.assertEqual(fail_login.status_code, 400)

        # 6. Đăng nhập lại bằng mật khẩu mới phải thành công
        self.token(self.client, "/login")
        success_login = self.post(self.client, "/login", {
            "username": "manager1",
            "password": "brand-new-password",
        })
        self.assertEqual(success_login.status_code, 302)


if __name__ == "__main__":
    unittest.main()
