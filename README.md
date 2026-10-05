# python-remote-mvp

Một bản **MVP remote desktop bằng Python** viết để **học nguyên lý hoạt động**:
host chụp màn hình → stream JPEG qua TCP → client hiển thị; client bắt mouse →
gửi event về host → host inject vào desktop.

- **Host / remote**: Kubuntu (X11) **hoặc Windows 10/11**. Chụp màn hình + nhận input.
- **Client / viewer**: Windows **hoặc Linux**. Hiển thị + điều khiển chuột/bàn phím.
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
- App **chạy foreground, log rõ ràng, không chạy ẩn, không persistence, không
  keylogger** — chỉ tự khởi động nếu bạn tự bật option *Khởi động cùng hệ thống*
  trong GUI. Muốn dừng: đóng cửa sổ / Ctrl+C.

---

## 1. Cài đặt trên Kubuntu (HOST Linux)

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

## 2. Cài đặt trên Windows (CLIENT hoặc HOST)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> Windows cũng chạy được **host** (`host_gui.py` / `host.py`) — capture qua GDI,
> input qua pynput, không cần X11. H.264 cần `ffmpeg` (thêm vào PATH).

## 3. Chạy HOST

Linux (CLI):

```bash
python host.py --bind 0.0.0.0 --port 7777 --token "change-me" --fps 15
```

Windows (GUI — khuyến nghị) hoặc Linux (GUI/tray):

```bash
python host_gui.py --token "change-me"
```

Tham số:

| Cờ | Mặc định | Ý nghĩa |
|---|---|---|
| `--bind` | `0.0.0.0` | Địa chỉ bind |
| `--port` | `7777` | Cổng TCP |
| `--token` | *(bắt buộc)* | Client phải gửi đúng mới được stream |
| `--fps` | `15` | Frame/giây mục tiêu (10–15 hợp lý) |
| `--quality` | `90` | Chất lượng JPEG 1–100 (sampling 4:4:4 — chữ nét) |
| `--max-width` | `2560` | Resize xuống nếu rộng hơn (giữ tỉ lệ) |
| `--h264-crf` | `18` | CRF H.264 0–51 (thấp hơn = nét hơn) |
| `--view-only` | *(tắt)* | Bật để chỉ xem, **không** nhận điều khiển |

## 4. Lấy Tailscale IP trên Kubuntu

```bash
tailscale ip -4
```

## 5. Chạy CLIENT (Windows hoặc Linux)

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

Combo **Độ phân giải** (mặc định **Tự động (vừa cửa sổ)**): stream tự khớp
đúng kích thước cửa sổ xem này và hiển thị 1:1 pixel — nét tối đa, ít băng
thông nhất (giống AnyDesk). Width tính theo tỉ lệ màn hình host đang chọn
(contain-fit, không bao giờ request lớn hơn monitor). Cửa sổ đổi tỉ lệ thì
client tự gửi lại yêu cầu (debounce 200ms; host gộp tạo lại encoder tối đa
2 lần/giây). Các chế độ khác:

- **Preset / gõ tay** — 320→3840 (4K) hoặc số tùy ý 160–7680.
- **Theo host** — giữ nguyên `--max-width` lúc host khởi động.

Với H.264, host tạo lại encoder (gửi kèm `generation` để client bỏ packet cũ
đến trễ) và client tạo lại decoder tương ứng. Góc phải dưới hiện kích thước
frame đang nhận.

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
        HOST (Linux X11 / Windows)                CLIENT (Windows/Linux)
   ┌────────────────────┐                    ┌──────────────────────┐
   │ mss.grab màn hình  │                    │  PySide6 GUI          │
   │   ↓                │   TCP (Tailscale)  │   RemoteView (QLabel) │
   │ cv2 resize+JPEG    │ ── frame (type 1) ─►  cv2.imdecode → QImage│
   │   ↓ send_packet    │                    │                       │
   │ [capture thread]   │                    │  bắt mouse → normalize│
   │                    │ ◄ control (type 2)─── (0..1)  [NetworkWorker│
   │ inject (XTEST/     │                    │            QThread]   │
   │  pynput)           │   hello (type 3)   │                       │
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
| `remote-host` | Linux, Windows | `remote-host` / `remote-host.exe` (CLI) |
| `remote-host-gui` | Linux, Windows | `remote-host-gui` / `remote-host-gui.exe` (GUI/tray) |
| `remote-client` | Linux, Windows | `remote-client` / `remote-client.exe` |

### Release tự động qua GitHub Actions

Push tag `v*` → workflow `.github/workflows/release.yml` build trên
`ubuntu-latest` và `windows-latest` (cả host + client cho từng OS), rồi tạo
GitHub Release kèm tất cả file:

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
- Host Linux chỉ **X11** (không Wayland — cố ý không bypass quyền OS);
  host Windows không cần X11.
- Codec **JPEG từng frame** mặc định (H.264 cần ffmpeg, giảm băng thông
  nhưng thêm 1–2 frame delay do decode).
- **Keyboard**: ký tự/phím đặc biệt/modifier, kể cả **ký tự Unicode ngoài layout
  host** (tiếng Việt có dấu, CJK...) — inject trực tiếp qua X11 keysym-remap /
  Windows SendInput Unicode. Text do IME/bộ gõ phía **client** commit được gửi
  nguyên ký tự; compose IME ngay trên host vẫn có thể lệch.
- Clipboard chỉ text; không audio, không file transfer.
- **Multi-monitor**: client chọn màn hình trong combo "Màn hình" (host báo danh
  sách khi kết nối); toạ độ chuột được map theo đúng monitor đang xem.
- Token đơn giản (đã so constant-time + khóa IP sau 5 lần sai), không TLS
  tầng ứng dụng (dựa vào Tailscale).

---

## 11. Phát triển & kiểm thử

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check .        # lint
pytest              # unit/integration tests
```

CI chạy lint + test trên mỗi push/PR (`.github/workflows/tests.yml`);
release workflow còn chạy smoke test cài đặt updater trên cả Linux/Windows.

---

## 12. Roadmap nâng cấp

1. **Video codec**: encode/decode trong process (PyAV) hoặc hardware
   (VAAPI/NVENC) để giảm latency và CPU.
2. **Clipboard ảnh/file**.
3. **Dirty-rectangle/XDamage** + adaptive fps/quality theo băng thông.
4. **Auth tốt hơn**: challenge-response, key trao đổi (đã có rate-limit).
5. **Transport**: QUIC / WebRTC (NAT traversal, độ trễ thấp).
6. **Port C++/Qt** sau khi bản Python chạy ổn định (hiệu năng capture/encode).
