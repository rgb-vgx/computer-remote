#!/usr/bin/env python3
"""host.py — Remote desktop HOST (chạy trên Kubuntu / X11).

Chức năng:
  * Chụp màn hình bằng mss, encode JPEG bằng OpenCV, stream qua TCP tới 1 client.
  * Nhận control event (mouse) từ client và inject vào desktop bằng pynput.
  * Auth bằng token: client phải gửi hello kèm token đúng mới được stream.

Ràng buộc an toàn (cố ý):
  * Chạy foreground, log rõ ràng ra stdout.
  * KHÔNG persistence / auto-start / chạy ẩn / keylogger / escalation.
  * Chỉ điều khiển được khi user host chủ động chạy chương trình + cấp token.

Chạy:
  python host.py --bind 0.0.0.0 --port 7777 --token "change-me" --fps 8 --quality 60
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import struct
import sys
import threading
import time

import cv2
import mss
import numpy as np

from common import protocol

log = logging.getLogger("host")


def check_display_env() -> None:
    """In thông tin session GUI và cảnh báo nếu không phải X11.

    KHÔNG ép thoát: vẫn cho chạy tiếp, nhưng cảnh báo capture/inject có thể
    fail nếu đang ở tty / Wayland / không có $DISPLAY.
    """
    session = os.environ.get("XDG_SESSION_TYPE", "")
    display = os.environ.get("DISPLAY", "")
    log.info("XDG_SESSION_TYPE=%r", session)
    log.info("DISPLAY=%r", display)

    if session.lower() != "x11" or not display:
        log.warning("=" * 64)
        log.warning("CẢNH BÁO: Không phát hiện session X11 hợp lệ.")
        log.warning("Bản MVP này cần chạy TRONG một desktop GUI session (X11).")
        log.warning("Khuyến nghị: đăng nhập 'Plasma (X11)' và mở Konsole từ đó.")
        log.warning("KHÔNG chạy qua SSH/TTY — sẽ không capture/input được.")
        log.warning("Kiểm tra: echo $XDG_SESSION_TYPE   (phải là 'x11')")
        log.warning("          echo $DISPLAY             (vd ':0', không rỗng)")
        log.warning("Wayland không được hỗ trợ ở bản này (không bypass quyền OS).")
        log.warning("=" * 64)
    else:
        log.info("Session X11 OK — sẵn sàng capture & inject input.")


class HostServer:
    """TCP server stream màn hình + nhận control. Chỉ phục vụ 1 client/lần."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stop_event = threading.Event()  # báo dừng toàn server
        self.server_sock: socket.socket | None = None

        # Mouse controller — khởi tạo lazy để nếu thiếu X11 thì lỗi rõ ràng
        # ngay lúc inject chứ không phải lúc import.
        self._mouse = None
        self._view_only_logged = False

    # ---- vòng đời server -------------------------------------------------

    def serve_forever(self) -> None:
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.args.bind, self.args.port))
        self.server_sock.listen(1)
        # timeout để vòng accept định kỳ kiểm tra stop_event (Ctrl+C).
        self.server_sock.settimeout(1.0)
        log.info("Đang lắng nghe trên %s:%d (chờ 1 client)",
                 self.args.bind, self.args.port)

        while not self.stop_event.is_set():
            try:
                client_sock, addr = self.server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            log.info("Client kết nối từ %s:%d", *addr)
            try:
                self._handle_client(client_sock, addr)
            except Exception as exc:  # noqa: BLE001 — log mọi lỗi của 1 phiên
                log.error("Lỗi khi xử lý client %s: %s", addr, exc)
            finally:
                _safe_close(client_sock)
                log.info("Client %s:%d đã ngắt — quay lại chờ client mới", *addr)

    def shutdown(self) -> None:
        self.stop_event.set()
        _safe_close(self.server_sock)

    # ---- xử lý 1 client --------------------------------------------------

    def _handle_client(self, sock: socket.socket, addr) -> None:
        # Auth: packet đầu tiên phải là hello kèm token đúng.
        sock.settimeout(10.0)
        ptype, payload = protocol.recv_packet(sock)
        if ptype != protocol.PKT_HELLO:
            log.warning("Client %s gửi packet không phải hello (type=%d) — đóng",
                        addr, ptype)
            return
        try:
            hello = protocol.decode_json(payload)
        except Exception:
            log.warning("Hello không phải JSON hợp lệ — đóng")
            return

        if hello.get("token") != self.args.token:
            log.warning("AUTH FAIL từ %s — token sai. Đóng kết nối.", addr)
            try:
                protocol.send_json(sock, protocol.PKT_INFO,
                                   {"type": "error", "message": "auth failed"})
            except OSError:
                pass
            return

        log.info("AUTH SUCCESS từ %s", addr)
        protocol.send_json(sock, protocol.PKT_INFO,
                           {"type": "info", "message": "auth ok"})

        # Stream: bỏ timeout đọc (control thread chờ event dài tuỳ ý),
        # capture thread tự nhịp theo fps.
        sock.settimeout(None)
        client_stop = threading.Event()

        send_thread = threading.Thread(
            target=self._capture_loop, args=(sock, client_stop),
            name="capture", daemon=True)
        recv_thread = threading.Thread(
            target=self._control_loop, args=(sock, client_stop),
            name="control", daemon=True)
        send_thread.start()
        recv_thread.start()

        # Chờ tới khi 1 trong 2 thread báo dừng (client ngắt / lỗi) hoặc Ctrl+C.
        while not client_stop.is_set() and not self.stop_event.is_set():
            time.sleep(0.2)
        client_stop.set()
        _safe_close(sock)  # buộc thread đang block recv thoát ra
        send_thread.join(timeout=2.0)
        recv_thread.join(timeout=2.0)

    # ---- thread capture/send --------------------------------------------

    def _capture_loop(self, sock: socket.socket, client_stop: threading.Event) -> None:
        frame_budget = 1.0 / max(1, self.args.fps)
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.args.quality]

        frames_since_log = 0
        last_log = time.time()

        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]  # [0] = tất cả màn hình gộp; [1] = chính
                while not client_stop.is_set() and not self.stop_event.is_set():
                    t0 = time.time()

                    raw = sct.grab(monitor)
                    # mss trả BGRA; OpenCV cũng dùng BGR → bỏ kênh alpha.
                    img = np.asarray(raw)[:, :, :3]

                    h, w = img.shape[:2]
                    if w > self.args.max_width:
                        scale = self.args.max_width / w
                        img = cv2.resize(
                            img, (self.args.max_width, int(h * scale)),
                            interpolation=cv2.INTER_AREA)

                    ok, buf = cv2.imencode(".jpg", img, encode_params)
                    if not ok:
                        log.error("cv2.imencode thất bại — bỏ frame")
                        continue

                    protocol.send_packet(sock, protocol.PKT_FRAME, buf.tobytes())

                    frames_since_log += 1
                    now = time.time()
                    if now - last_log >= 5.0:
                        fps = frames_since_log / (now - last_log)
                        log.info("Đang stream ~%.1f FPS (%dx%d, q=%d)",
                                 fps, img.shape[1], img.shape[0], self.args.quality)
                        frames_since_log = 0
                        last_log = now

                    # Nhịp theo fps.
                    elapsed = time.time() - t0
                    if elapsed < frame_budget:
                        time.sleep(frame_budget - elapsed)
        except (ConnectionError, OSError, struct.error) as exc:
            log.info("Capture loop dừng (kết nối): %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.error("Capture loop lỗi: %s", exc)
        finally:
            client_stop.set()

    # ---- thread receive/control -----------------------------------------

    def _control_loop(self, sock: socket.socket, client_stop: threading.Event) -> None:
        try:
            while not client_stop.is_set() and not self.stop_event.is_set():
                ptype, payload = protocol.recv_packet(sock)
                if ptype != protocol.PKT_CONTROL:
                    continue
                if self.args.view_only:
                    if not self._view_only_logged:
                        log.info("--view-only đang bật: bỏ qua mọi control event.")
                        self._view_only_logged = True
                    continue
                try:
                    event = protocol.decode_json(payload)
                except Exception:
                    continue
                self._apply_event(event)
        except (ConnectionError, OSError, struct.error) as exc:
            log.info("Control loop dừng (kết nối): %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.error("Control loop lỗi: %s", exc)
        finally:
            client_stop.set()

    def _apply_event(self, event: dict) -> None:
        """Map normalized coord (0..1) sang pixel và inject bằng pynput."""
        mouse = self._get_mouse()
        if mouse is None:
            return
        from pynput.mouse import Button

        screen_w, screen_h = self._screen_size()
        kind = event.get("event")

        if kind == "mouse_move":
            x = _clamp01(event.get("x", 0.0)) * screen_w
            y = _clamp01(event.get("y", 0.0)) * screen_h
            mouse.position = (int(x), int(y))
        elif kind in ("mouse_down", "mouse_up"):
            # Đặt vị trí trước rồi mới press/release để click đúng chỗ.
            x = _clamp01(event.get("x", 0.0)) * screen_w
            y = _clamp01(event.get("y", 0.0)) * screen_h
            mouse.position = (int(x), int(y))
            btn = Button.right if event.get("button") == "right" else Button.left
            if kind == "mouse_down":
                mouse.press(btn)
            else:
                mouse.release(btn)

    # ---- helpers lazy-init ----------------------------------------------

    def _get_mouse(self):
        if self._mouse is None:
            try:
                from pynput.mouse import Controller
                self._mouse = Controller()
            except Exception as exc:  # noqa: BLE001
                log.error("Không khởi tạo được pynput mouse (cần X11?): %s", exc)
                self._mouse = False  # đánh dấu đã thử & thất bại
        return self._mouse or None

    def _screen_size(self) -> tuple[int, int]:
        # Lấy 1 lần kích thước màn hình thật để map toạ độ.
        if not hasattr(self, "_screen_cache"):
            with mss.mss() as sct:
                mon = sct.monitors[1]
                self._screen_cache = (mon["width"], mon["height"])
        return self._screen_cache


def _clamp01(v: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def _safe_close(sock) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Remote desktop HOST (Kubuntu/X11).")
    p.add_argument("--bind", default="0.0.0.0",
                   help="Địa chỉ bind (mặc định 0.0.0.0). Qua Tailscale nên ổn.")
    p.add_argument("--port", type=int, default=7777, help="Cổng TCP (mặc định 7777).")
    p.add_argument("--token", required=True,
                   help="Token bắt buộc — client phải gửi đúng mới được stream.")
    p.add_argument("--fps", type=int, default=8, help="Số frame/giây mục tiêu.")
    p.add_argument("--quality", type=int, default=60, help="Chất lượng JPEG (1-100).")
    p.add_argument("--max-width", type=int, default=1280,
                   help="Resize xuống nếu rộng hơn (giữ aspect ratio).")
    p.add_argument("--view-only", action="store_true",
                   help="Nếu bật: chỉ stream, KHÔNG nhận/inject control.")
    return p.parse_args(argv)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()

    log.info("=== Remote desktop HOST (MVP, foreground) ===")
    check_display_env()
    if args.view_only:
        log.info("Chế độ --view-only: input control sẽ bị bỏ qua.")

    server = HostServer(args)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Nhận Ctrl+C — đang tắt...")
    finally:
        server.shutdown()
        log.info("Host đã tắt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
