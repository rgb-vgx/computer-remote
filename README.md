# python-remote-mvp

Một bản **MVP remote desktop bằng Python** viết để **học nguyên lý hoạt động**:
host chụp màn hình → stream JPEG qua TCP → client hiển thị; client bắt mouse →
gửi event về host → host inject vào desktop.

- **Host / remote**: Kubuntu (X11). Chụp màn hình + nhận input.
- **Client / viewer**: Windows. Hiển thị + điều khiển chuột.
- Kết nối điểm-điểm qua **Tailscale** (`100.x.y.z:7777`).

---

## ⚠️ Cảnh báo an toàn — đọc trước khi chạy

- **Chỉ dùng trên máy mà bạn sở hữu / có toàn quyền.** Remote control = điều khiển
  chuột trên máy thật. Không dùng để truy cập máy người khác.
- **Không expose trực tiếp ra Internet.** Đừng forward port 7777 ra ngoài.
  Hãy đi qua **Tailscale** (mạng riêng, mã hoá ở tầng transport).
- Token chỉ để **tránh connect nhầm trong tailnet**, không phải lớp bảo mật mạnh.
  Không có TLS ở tầng ứng dụng vì đã dựa vào Tailscale lo phần mã hoá.
- **Không chạy nếu bạn chưa hiểu rủi ro của remote control.** Người cầm client
  điều khiển được chuột máy host.
- App **chạy foreground, log rõ ràng, không tự khởi động, không chạy ẩn, không
  persistence, không keylogger.** Muốn dừng: đóng cửa sổ / Ctrl+C.

---

## 1. Cài đặt trên Kubuntu (HOST)

```bash
sudo apt update
sudo apt install python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> Host **bắt buộc chạy trong một desktop session X11** (khuyến nghị
> **Plasma (X11)** ở màn hình đăng nhập). Mở **Konsole** từ trong desktop đó.
> Không chạy qua SSH/TTY và không dùng Wayland.

## 2. Cài đặt trên Windows (CLIENT)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3. Chạy HOST (Kubuntu)

```bash
python host.py --bind 0.0.0.0 --port 7777 --token "change-me" --fps 8 --quality 60
```

Tham số:

| Cờ | Mặc định | Ý nghĩa |
|---|---|---|
| `--bind` | `0.0.0.0` | Địa chỉ bind |
| `--port` | `7777` | Cổng TCP |
| `--token` | *(bắt buộc)* | Client phải gửi đúng mới được stream |
| `--fps` | `15` | Frame/giây mục tiêu (10–15 hợp lý) |
| `--quality` | `75` | Chất lượng JPEG 1–100 |
| `--max-width` | `1920` | Resize xuống nếu rộng hơn (giữ tỉ lệ) |
| `--view-only` | *(tắt)* | Bật để chỉ xem, **không** nhận điều khiển |

## 4. Lấy Tailscale IP trên Kubuntu

```bash
tailscale ip -4
```

## 5. Chạy CLIENT (Windows)

```powershell
python client.py
```

Trong cửa sổ, nhập:

```text
Host:  100.x.y.z      # Tailscale IP của Kubuntu
Port:  7777
Token: change-me
```

Bấm **Connect**. Di chuột / click trái / click phải trong vùng hiển thị để điều
khiển host. Bấm **Disconnect** (hoặc đóng cửa sổ) để dừng.

Combo **Độ phân giải** đổi `max-width` của stream ngay khi đang xem (thấp hơn
= mượt hơn, ít băng thông hơn): nhiều preset từ 320 đến 3840 (4K), hoặc gõ số
tùy ý trong khoảng 160–7680. Chọn **Theo host** để giữ đúng tham số
`--max-width` lúc host khởi động. Với H.264, host tạo lại encoder và client
tạo lại decoder tương ứng. Góc phải dưới hiện kích thước frame đang nhận.

Client tự lưu Host/Port/Token/độ phân giải/kích thước cửa sổ cho lần sau. Khi
mất mạng, client **tự kết nối lại** (1s → 2s → 4s → 8s → 10s); nếu host từ
chối (sai token hoặc chủ động ngắt) thì dừng và hiện lý do.

Host GUI (`remote-host-gui`) hiện client đang kết nối, báo qua tray icon, và
có nút **Ngắt client** để đá client ra.

## 6. Nếu bật UFW trên Kubuntu

Chỉ mở cổng trên interface Tailscale (không mở ra LAN/Internet):

```bash
sudo ufw allow in on tailscale0 to any port 7777 proto tcp
```

---

## 7. Troubleshooting

**Client không connect:**

```bash
tailscale status              # cả 2 máy thấy nhau?
ping 100.x.y.z                # ping được Tailscale IP của host?
ss -ltnp | grep 7777          # host có đang listen?
```

**Host không capture được màn hình:**

```bash
echo $XDG_SESSION_TYPE        # phải là 'x11'
echo $DISPLAY                 # không được rỗng (vd ':0')
```

- Chạy trong **Konsole của desktop session**, không qua SSH/TTY.
- Đăng nhập bằng **Plasma (X11)**, không phải Wayland.

**Điều khiển chuột không hoạt động:**

- Xác nhận host đang ở X11 (xem trên).
- Kiểm tra quyền input (pynput cần X11 session hợp lệ).
- Thử chạy host + client trên **cùng một máy Kubuntu** trước để loại trừ vấn đề mạng.
- Kiểm tra không bật `--view-only`.

---

## 8. Kiến trúc (tóm tắt)

```
        Kubuntu HOST                              Windows CLIENT
   ┌────────────────────┐                    ┌──────────────────────┐
   │ mss.grab màn hình  │                    │  PySide6 GUI          │
   │   ↓                │   TCP (Tailscale)  │   RemoteView (QLabel) │
   │ cv2 resize+JPEG    │ ── frame (type 1) ─►  cv2.imdecode → QImage│
   │   ↓ send_packet    │                    │                       │
   │ [capture thread]   │                    │  bắt mouse → normalize│
   │                    │ ◄ control (type 2)─── (0..1)  [NetworkWorker│
   │ pynput inject      │                    │            QThread]   │
   │ [control thread]   │   hello (type 3)   │                       │
   └────────────────────┘   info  (type 4)   └──────────────────────┘
```

- **Protocol** (`common/protocol.py`): mỗi packet = header 5 byte
  (`uint32 BE payload_len` + `uint8 type`) + payload. `recv_exact` xử lý
  partial read để tránh dính/đứt packet trên TCP stream.
- **Toạ độ normalized 0..1**: client gửi tỉ lệ theo vùng ảnh, host nhân với
  kích thước màn hình thật → resize cửa sổ client vẫn click đúng chỗ.
- **Chỉ gửi frame khi có thay đổi**: host so khớp pixel chính xác với frame
  trước (màn hình tĩnh cho frame giống hệt nhau) → không tốn băng thông, vẫn
  bắt được thay đổi nhỏ như con trỏ soạn thảo; heartbeat khi tĩnh 1 frame/s
  với JPEG, 4 frame/s với H.264 (bù delay 1–2 frame của decoder).
- **Threading**: host có 2 thread (capture/send, receive/control) cho mỗi client;
  client để toàn bộ socket trong 1 QThread, GUI thread chỉ chạm UI qua Qt signal.

---

## 9. Đóng gói release

### Tự build trên máy của bạn

```bash
source .venv/bin/activate
pip install -r packaging/requirements-build.txt
python packaging/build.py            # build tất cả target hợp với OS hiện tại
```

Kết quả: `dist/<target>/` (chạy trực tiếp) và
`release/<target>-<version>-<os>-<arch>.(tar.gz|zip)`.

| Target | OS | File chạy |
|---|---|---|
| `remote-host` | Linux | `remote-host` (CLI) |
| `remote-host-gui` | Linux | `remote-host-gui` (GUI/tray) |
| `remote-client` | Linux, Windows | `remote-client` / `remote-client.exe` |

### Release tự động qua GitHub Actions

Push tag `v*` → workflow `.github/workflows/release.yml` build trên
`ubuntu-latest` (host + client) và `windows-latest` (client), rồi tạo GitHub
Release kèm tất cả file:

```bash
git tag v0.1.0
git push origin v0.1.0
```

Chạy tay không cần tag: **Actions → Release → Run workflow** (chỉ tạo artifacts).

Lưu ý khi dùng bản đóng gói:

- **H.264 cần `ffmpeg` cài sẵn** trên máy chạy host (không bundle trong release);
  thiếu ffmpeg thì tự fallback JPEG.
- Bản Linux build trên Ubuntu 24.04 → cần **glibc ≥ 2.39** (Ubuntu 24.04+,
  Debian 13+). Máy cũ hơn chạy từ source.
- Log ghi cạnh file thực thi: `<thư mục app>/logs/{host,client}.log`.
- File Windows chưa ký số nên SmartScreen có thể cảnh báo — chọn
  *More info → Run anyway*.
- Build ghi `version.txt` cạnh exe để updater biết version hiện tại.

### Tự cập nhật trong app

Client và host GUI có nút **Cập nhật**: gọi GitHub Releases API (repo public,
không cần token), so version với bản đang chạy, tải asset đúng OS/arch rồi:

- **Bản đóng gói**: sinh script tách rời, chờ app thoát → thay thư mục
  (giữ `logs/`) → tự mở lại.
- **Bản chạy source**: `git pull --ff-only` + `pip install -r requirements.txt`
  rồi khởi động lại.

Nếu update trục trặc, xem `<thư mục app>/logs/update.log` (script ghi từng
bước: chờ app thoát, robocopy/mv, rc, mở lại). CI chạy smoke test
`packaging/smoke_update.py` trên cả Linux và Windows cho luồng này.

Version hiện tại hiện trên tiêu đề cửa sổ. Bản `≤ v0.1.3` chưa có nút này —
cần cài tay một lần, sau đó update trong app.

---

## 10. Giới hạn của bản MVP

- Chỉ **1 client** tại một thời điểm.
- Chỉ **X11** (không Wayland — cố ý không bypass quyền OS).
- Codec **JPEG từng frame** mặc định (H.264 cần ffmpeg, giảm băng thông
  nhưng thêm 1–2 frame delay do decode).
- Chỉ **mouse** (move / click trái, phải / scroll) và **keyboard** (ký tự,
  phím đặc biệt, modifier). Chưa hỗ trợ dead-key / IME phức tạp.
- Clipboard chỉ text; không multi-monitor, không audio, không file transfer.
- Token đơn giản, không TLS tầng ứng dụng (dựa vào Tailscale).

---

## 11. Roadmap nâng cấp

1. **Video codec**: encode/decode trong process (PyAV) hoặc hardware
   (VAAPI/NVENC) để giảm latency và CPU.
2. **Clipboard ảnh/file**, dead-key / IME.
3. **Multi-monitor**: chọn / chuyển màn hình.
4. **Reconnect** tự động khi rớt mạng.
5. **Auth tốt hơn**: challenge-response, key trao đổi, rate-limit.
6. **Transport**: QUIC / WebRTC (NAT traversal, độ trễ thấp).
7. **Port C++/Qt** sau khi bản Python chạy ổn định (hiệu năng capture/encode).
