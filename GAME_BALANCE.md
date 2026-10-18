# Rà soát dữ liệu và cân bằng trò chơi

## Những dữ liệu mẫu đã được thay thế

| Phần | Trước đây | Hiện tại |
|---|---|---|
| Tài khoản mới | Cấp 8; tên HLV, sân, ngày thành lập và thành tích có sẵn | Cấp 1; HLV theo tài khoản, ngày thành lập theo ngày tạo, thành tích bằng 0 |
| Cầu thủ | Cùng một bộ cầu thủ khởi đầu do seed cố định; thành tích tự tính từ tuổi/ID | Sinh riêng và lưu một lần; thành tích lấy từ lần ra sân thực tế |
| Thống kê giải | Ghi sẵn bàn thắng, kiến tạo, MVP với `fixture_id=0` | Chưa đá thì bảng trống; ghi từng cầu thủ, tỷ số, khán giả và MVP sau trận |
| Đội hình đối thủ | Tên và chỉ số vẽ lại khi xem trang | Đội hình máy được sinh và lưu, các trang cùng đọc một nguồn |
| Tập luyện | Tăng nguyên điểm liên tục; có thể mua thẳng chỉ số 200 và nâng cả tiềm năng | Lưu mức tăng lẻ; phụ thuộc bài tập, tuổi, chỉ số hiện tại, thể lực và giới hạn trong ngày; mọi cầu thủ có cùng trần 200% |
| Tài nguyên | Cộng 10 Token không giới hạn; reset mùa trả lại tiền | Bỏ Token miễn phí; chuyển mùa giữ ngân sách, trả lương tuần, ghi thu chi |
| Nâng cấp | Nhận cấp mới ngay khi bắt đầu; 5 Token có thể bỏ qua cả tiền xây | Chỉ áp dụng khi hoàn thành; trả tiền xây trước; Token chỉ rút ngắn công trình đang xây |
| Học viện | Ký một cầu thủ rồi sinh lại nguyên lứa; bán cầu thủ miễn phí lấy lời | Một lứa 11 người mỗi mùa; phí ký 65% giá trị, bán thu 45%; đội một tối đa 25 người |
| Mùa giải | Các giải trùng ngày; vòng cúp dừng giữa chừng | Có lịch nghỉ và đầy đủ vòng cúp đến chung kết; lưu người thắng luân lưu |

## Bộ quy tắc hiện tại

- **Khởi đầu:** 18 cầu thủ, 2.000.000 tiền mặt, 20 Token, 10 thẻ thể lực, 5 thẻ tinh thần, 3 thẻ y tế. Cả 7 công trình bắt đầu cấp 1.
- **Tên cầu thủ:** sinh tên theo 15 quốc tịch: Việt Nam, Anh, Pháp, Đức, Tây Ban Nha, Ý, Bồ Đào Nha, Brazil, Argentina, Hà Lan, Thụy Điển, Thổ Nhĩ Kỳ, Bulgaria, Nhật Bản và Hàn Quốc. Tên và quốc tịch được tạo cùng nhau, lưu khi tạo cầu thủ mới cho đội khởi đầu, học viện, thị trường và các gợi ý chiêu mộ. Đội máy cũng dùng kho tên đa quốc gia. Cầu thủ đã lưu giữ nguyên tên và tiến trình; các đợt chuyển nhượng, ưu đãi và lứa học viện mới dùng kho tên mới.
- **Mùa giải:** 20 đội, 38 vòng sân nhà/sân khách, mỗi vòng cách 7 ngày. Lịch mùa chuẩn dài 273 ngày mô phỏng; cúp xen giữa, mỗi đội có ít nhất 3 ngày giữa hai trận. Mùa cũ được giãn lại nếu cần để giữ các kết quả đã chơi. Giải châu lục dùng thể thức riêng của trò chơi, không tuyên bố là lịch/luật UEFA hiện hành.
- **Thăng cấp:** phải kết thúc lịch đấu và kỳ nghỉ cuối mùa. Top 8 tăng đúng 1 cấp; hạng 9 trở xuống giữ cấp. Top 4 được dự C1 mùa sau; hạng 5–8 dự C2. Không có thao tác reset để nhận tiền lại.
- **Sang mùa:** cầu thủ đội một tăng 1 tuổi và cả 6 kỹ năng gốc nhân `0,8`. Kinh nghiệm và chiều cao/cân nặng không bị giảm theo kỹ năng. Lịch sử mùa và cầu thủ được lưu trước khi tạo lịch mới. Mười lăm chỉ số chi tiết là phép tính từ 6 kỹ năng đang lưu, không phải số minh họa theo ID.
- **Thể lực:** 10% mỗi 8 giờ, gồm cả thời gian thoát game. Sang 1 ngày mô phỏng cộng 24 giờ vào đồng hồ lưu trữ, tương đương tối đa 30% hồi phục; không có phần thưởng hồi phục khác từ nút sang ngày. Tối đa 100%, không tích trữ lượt hồi lúc đã đầy.
- **Thi đấu:** trận chính thức hao khoảng 40% thể lực (35% khi pressing thấp, 45% khi cao, còn được điều chỉnh bởi đặc tính cầu thủ). Giao hữu hao 25%, cũng cần cách lịch chính thức và lần giao hữu trước ít nhất 3 ngày.
- **Xếp đội hình tự do:** lưu tọa độ từng vị trí theo sơ đồ đang chọn. Vai trò trên sân lấy từ vùng thả, dùng khi tính hiệu quả thi đấu và hiển thị ở sân tập; vị trí sở trường của cầu thủ không bị đổi. Sân tấn công từ trái sang phải: vùng giữa theo chiều dài gồm GK (0–18%), DC (18–36%), DMC (36–44%), MC (44–58%), AMC (58–70%), ST (70–100%). Hai biên ngoài dải ngang 25–75% lần lượt là DL/DR, ML/MR, AML/AMR theo tuyến. Bật “Khu vực vị trí” hoặc bắt đầu kéo để xem ranh giới. “Tự động xếp” khôi phục tọa độ và vị trí của sơ đồ mẫu.
- **Tập luyện:** không tập dưới 20% hoặc khi không đủ thể lực trả cho bài đã chọn. Các bài tập cộng dồn mức hao tổn; gói khó tốn nhiều hơn và tăng kỹ năng nhanh hơn gói nhẹ. Mọi cầu thủ đội một và học viện đều có thể tập từng kỹ năng lên **200%**, không bị giới hạn bởi tiềm năng cũ. Mức tăng chia cho các kỹ năng được tập, giảm khi gần 200% nhưng có hệ số tối thiểu để chạm được trần; tổng tối đa 1,2 điểm kỹ năng/cầu thủ/ngày. Đây là tổng của 6 kỹ năng, không phải tăng 1,2 OVR cho từng kỹ năng. Thẻ thể lực không bỏ qua giới hạn trong ngày. Cột `potential` cũ được giữ trong bản lưu để tương thích nhưng không ảnh hưởng tốc độ tập hay giới hạn kỹ năng.
- **Thẻ phục hồi:** 1 thẻ hồi 10% cho 1 cầu thủ; hồi cả đội tiêu hao 1 thẻ cho từng người cần hồi. Không trừ thẻ khi ID sai hoặc cầu thủ đã đầy thể lực. Thẻ y tế chữa tối đa 3 ngày chấn thương/thẻ và không tự tăng thể lực.
- **Tuổi và kinh nghiệm:** tuổi trẻ có hệ số phát triển cao hơn; kinh nghiệm được tích lũy khi thực sự ra sân và qua mùa giải. Độ trưởng thành và kinh nghiệm có thể cộng tối đa 20% hiệu quả thi đấu, không cộng vào kỹ năng gốc. Kết quả trận vẫn có xác suất, không bảo đảm người già hoặc chỉ số cao luôn thắng.
- **Giải nghệ:** mỗi người có tuổi giải nghệ riêng được sinh và lưu một lần. Cầu thủ sân thường trong khoảng 32–39; thủ môn thường 35–42; cầu thủ nhập từ bản lưu đã lớn tuổi được bảo đảm ít nhất một mùa còn lại. Người giải nghệ rời đội hình nhưng hồ sơ và thành tích vẫn được giữ. Nếu thiếu 11 người sau giải nghệ hàng loạt, đội đôn đủ người từ chính lứa học viện mới để tránh mất khả năng thi đấu.
- **Kinh tế:** lương được trả mỗi 7 ngày; thiếu tiền được ghi thành nợ thay vì bỏ qua chi phí. Hợp đồng áo đấu nhận 100.000 khi ký và 12.000/ngày; TV nhận 2 Token khi ký và 1 Token mỗi 3 ngày. CLB có nợ phải trả nợ trước khi tuyển thêm người.
- **Công trình:** giới hạn cấp CLB + 2, trần 10. Chi phí tăng theo cấp; lợi ích và mô tả đọc chung một bộ công thức. Công trình đang xây chưa có lợi ích cấp mới. Cấp 1 có sân 2.000 chỗ, cửa hàng 5.000/ngày, 1 thẻ thể lực/ngày và 1 thẻ y tế/tuần. Các công trình nâng lợi ích dần thay vì tăng hàng chục điểm chất lượng cầu thủ mỗi cấp.

## Bản lưu cũ và tính nhất quán

Lần đầu chuyển sang bộ quy tắc này, game tự tạo bản sao `*.before-rules.sqlite3` cạnh bản lưu. Chỉ xóa các thành tích mẫu đã biết và những thống kê có `fixture_id=0`; giữ cầu thủ, số dư, công trình đã sở hữu và các kết quả thực tế. Cấp 8 có sẵn từ bản mẫu được đưa về cấp 1 một lần. Các lần sau không đặt lại cấp, tuổi, kỹ năng hay tiến trình.

Mỗi thao tác thay đổi game và mỗi đợt cập nhật đấu giá/trận máy là một giao dịch SQLite: lỗi giữa chừng sẽ hoàn tác cả tiền và trạng thái liên quan. Quà, trận đấu và chuyển mùa có điều kiện chống nhận lặp. Việc đổi tên cầu thủ không làm mất thành tích theo ID.

Bản cũ chỉ ghi cầu thủ ghi bàn/kiến tạo và không có danh sách đầy đủ người ra sân. Các bàn thắng/kiến tạo thực tế còn log được giữ; game không tự bịa số lần ra sân hoặc gán thành tích cho một người khi có nhiều cầu thủ trùng tên. Lịch sử ra sân đầy đủ được ghi cho các trận kể từ bản cập nhật này.

Đội máy, cầu thủ chuyển nhượng và thời tiết là nội dung mô phỏng được tạo trong game, không phải thông tin trực tiếp từ đời thực. Đấu giá có đối thủ máy; không phải hệ thống PvP nhiều tài khoản. Kinh tế và mức tăng chỉ số đã có kiểm thử quy tắc; cân bằng về cảm giác chơi lâu dài vẫn cần điều chỉnh theo phản hồi chơi thực tế.

## Kiểm tra

```powershell
python -m unittest discover -s tests -v
```

Kiểm thử bao gồm tài khoản riêng, thống kê bằng 0 trước trận, ghi nhận lần ra sân, tiến trình cúp đến chung kết, khoảng cách lịch đấu, tăng tuổi và giảm 20% một lần, thăng cấp top 8, kinh nghiệm, giải nghệ, hồi thể lực ngoại tuyến, thẻ hồi phục, chống lạm phát chỉ số, quyền lợi công trình, bản sao chuyển đổi dữ liệu và hoàn tác giao dịch lỗi.
