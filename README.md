# ⚽ Apex Eleven (Top Eleven Edition)

Game quản lý bóng đá chiến thuật đỉnh cao, lấy cảm hứng từ tựa game huyền thoại **Top Eleven - Be a Football Manager**.

---

## 🌟 Tính năng nổi bật

- 🎮 **Giao diện Top Eleven Gaming:** Theme sân vận động đêm hiện đại, hiệu ứng ánh sáng neon thể thao, huy hiệu CLB kim loại và HUD tiền tệ 3D (Cash & Token).
- 🧲 **Sa bàn chiến thuật & Kéo thả 11 đá chính:** Mặt cỏ 3D chân thực, đĩa token cầu thủ đổi màu theo vai trò (GK, DEF, MID, FWD), OVR rating, thanh thể lực ⚡ và vương miện Đội trưởng (👑).
- 🛒 **Sàn chuyển nhượng đấu giá trực tiếp:** Đồng hồ đếm ngược 3 phút/phiên, báo động đỏ khi đếm lùi, ghim thẻ vàng mục tiêu theo dõi và trả giá 1-chạm (+Cash hoặc +Token).
- 🏆 **Đấu trường giải đấu & Matchday Scoreboard:** Đồ họa truyền hình trực tiếp, dự báo xác suất Thắng / Hòa / Thua (%) và so sánh xG đối thủ, bảng xếp hạng League với khu vực dự Champions Cup.
- 🏋️ **Trung tâm huấn luyện & Y tế:** Bài tập chuyên sâu theo nhóm (Tấn công, Phòng ngự, Thể lực, Chiến thuật) với 3 mức cường độ.
- 🏟️ **Cơ sở vật chất & Sân vận động:** Nâng cấp Sân bóng, Sân tập, Trung tâm y tế và Lò đào tạo trẻ với thanh đếm lùi thi công.
- 🤝 **Trận đấu giao hữu:** Thử nghiệm chiến thuật và cọ xát tân binh khi lịch chính thức cho phép.

---

## 🚀 Cài đặt & Khởi chạy

1. Cài đặt Python 3.10+ (khuyến nghị 3.11 hoặc mới hơn)
2. Mở terminal tại thư mục dự án và chạy:

```bash
pip install -r requirements.txt
python app.py
```

3. Mở trình duyệt truy cập: **[http://127.0.0.1:5000](http://127.0.0.1:5000)**

4. Chọn **Đăng ký**, nhập tên đăng nhập, tên đội bóng và mật khẩu (8–128 ký tự). Sau khi tạo tài khoản, bạn được đăng nhập và nhận đội bóng khởi đầu với 18 cầu thủ. Các lần sau dùng **Đăng nhập** để tiếp tục; nút **Đăng xuất** nằm trên thanh thông tin.

### Tài khoản và lưu tiến trình

- Tên đăng nhập có 3–32 ký tự không dấu: chữ, số, `.`, `_`, `-`; không phân biệt chữ hoa/thường.
- Mỗi tài khoản có file SQLite riêng cho toàn bộ đội bóng, cầu thủ, đội hình, chiến thuật, ngân sách, chợ đấu giá, cơ sở vật chất, lịch thi đấu và nhật ký. Tiến trình tự lưu sau mỗi thao tác và vẫn còn khi khởi động lại ứng dụng.
- Mật khẩu được băm bằng Werkzeug; phiên đăng nhập được ký và có hạn tối đa 7 ngày. Mọi biểu mẫu POST đều có mã CSRF. Đăng nhập sai 5 lần cho cùng tài khoản/IP hoặc 30 lần/IP trong 15 phút sẽ bị giới hạn tạm thời.
- Dữ liệu mặc định nằm trong `instance/`: `accounts.sqlite3` lưu tài khoản, `games/` lưu tiến trình và `session.key` là khóa ký phiên. Thư mục này không được đưa vào Git. Sao lưu **toàn bộ thư mục** để giữ tài khoản và tiến trình.
- Có thể đặt `TOUCHLINE_DATA_DIR` thành đường dẫn tuyệt đối khác, hoặc đặt `SECRET_KEY` qua biến môi trường. Nếu không đặt khóa, ứng dụng tự tạo và giữ lại trong `session.key`. Khi chạy bằng HTTPS, đặt `SESSION_COOKIE_SECURE=1`. Chỉ bật `FLASK_DEBUG=1` khi phát triển.

### Chuyển dữ liệu từ phiên bản cũ

File `game.db` cũ được giữ nguyên và không tự động gán cho người đăng ký đầu tiên. Để tiếp tục đội bóng cũ, đăng ký tài khoản trước, dừng máy chủ và chạy:

```bash
flask --app app import-legacy --username ten_dang_nhap
```

Lệnh này sao lưu tiến trình hiện tại của tài khoản trước khi nhập `game.db`; file nguồn được giữ nguyên. Có thể chọn nguồn khác bằng `--source duong_dan_file.db`. Sau đó khởi động lại máy chủ và đăng nhập.

### Kiểm thử

Quy tắc mùa giải, tuổi, tập luyện, thể lực, giải nghệ, tài nguyên và xử lý bản lưu cũ được mô tả trong [GAME_BALANCE.md](GAME_BALANCE.md).

```bash
python -m unittest discover -s tests -v
```

---

## 🎯 Hướng dẫn chơi nhanh

- **Sang ngày mới:** Hoàn thành trận đấu trong ngày rồi bấm **"⏭️ Sang ngày"**. Một ngày mô phỏng tương đương 24 giờ hồi phục.
- **Xếp đội hình:** Vào mục **Đội hình chính** để kéo thả 11 cầu thủ và bấm ⭐ trao băng Đội trưởng.
- **Săn cầu thủ:** Canh 15-30 giây cuối ở **Chợ đấu giá** để trả giá chốt đơn cầu thủ chất lượng cao.
- **Giữ thể lực:** Hồi 10% mỗi 8 giờ; một thẻ hồi 10% cho một người. Dưới 20% không được tập. Trận chính thức thường hao 40% thể lực.
- **Sang mùa mới:** Tổng kết sau khi hết lịch đấu và kỳ nghỉ: top 8 tăng 1 cấp, cầu thủ tăng 1 tuổi, giảm 20% kỹ năng và xét giải nghệ riêng từng người.
