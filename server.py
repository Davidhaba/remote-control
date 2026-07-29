import argparse
import socket
import threading
import numpy as np
import time
import json
import base64
import io
import sys
import ctypes
from ctypes import wintypes
try:
    import pyautogui
except Exception:
    print("pyautogui module not found. Please install pyautogui package.")
    exit(1)
try:
    import win32gui
except Exception:
    print("win32gui module not found. Please install pywin32 package.")
    win32gui = None
try:
    from PIL import Image, ImageGrab
except Exception:
    print("PIL module not found. You can install pillow package. Continuing without it.")
    Image = None

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def capture_cursor_as_png():
    if not win32gui or not Image:
        return None
    try:
        flags, hcursor, (cx, cy) = win32gui.GetCursorInfo()
        if not hcursor:
            return None
        icon_info = win32gui.GetIconInfo(hcursor)
        hotspot_x = icon_info[1] or 0
        hotspot_y = icon_info[2] or 0
    except Exception:
        return None

    try:
        w, h = 32, 32
        hdc_screen = user32.GetDC(None)
        if not hdc_screen:
            return None

        memdc = gdi32.CreateCompatibleDC(hdc_screen)
        if not memdc:
            user32.ReleaseDC(None, hdc_screen)
            return None

        bitmap = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        if not bitmap:
            gdi32.DeleteDC(memdc)
            user32.ReleaseDC(None, hdc_screen)
            return None

        old_bitmap = gdi32.SelectObject(memdc, bitmap)
        try:
            user32.DrawIconEx(memdc, 0, 0, hcursor, w, h, 0, None, 3)
            bitmap_info = BITMAPINFO()
            bitmap_info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bitmap_info.bmiHeader.biWidth = w
            bitmap_info.bmiHeader.biHeight = -h
            bitmap_info.bmiHeader.biPlanes = 1
            bitmap_info.bmiHeader.biBitCount = 32
            bitmap_info.bmiHeader.biCompression = 0

            pixel_buffer = (ctypes.c_ubyte * (w * h * 4))()
            if not gdi32.GetDIBits(hdc_screen, bitmap, 0, h, pixel_buffer, ctypes.byref(bitmap_info), 0):
                raise RuntimeError("GetDIBits failed")

            img = Image.frombuffer('RGBA', (w, h), pixel_buffer, 'raw', 'BGRA', 0, 1)
            img = img.convert('RGBA')
            bbox = img.getbbox()
            if bbox is None:
                gray = img.convert('L')
                alpha = gray.point(lambda p: 255 if p > 0 else 0)
                img.putalpha(alpha)
            else:
                aext = img.getchannel('A').getextrema()
                if aext[1] == 0:
                    gray = img.convert('L')
                    alpha = gray.point(lambda p: 255 if p > 0 else 0)
                    img.putalpha(alpha)
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            return base64.b64encode(buf.getvalue()).decode('ascii'), hotspot_x, hotspot_y
        finally:
            if old_bitmap:
                gdi32.SelectObject(memdc, old_bitmap)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memdc)
            user32.ReleaseDC(None, hdc_screen)
    except Exception:
        return None

try:
    import cv2
except ImportError:
    if Image is None:
        print("cv2 module not found. Please install opencv-python package.")
        exit(1)

    class _CV2Fallback:
        COLOR_RGB2BGR = 4
        INTER_LINEAR = 1
        INTER_AREA = 0
        IMWRITE_JPEG_QUALITY = 1

        @staticmethod
        def cvtColor(frame, code):
            if code == _CV2Fallback.COLOR_RGB2BGR:
                return frame[..., ::-1]
            return frame

        @staticmethod
        def resize(frame, size, interpolation=None):
            img = Image.fromarray(frame[..., ::-1])
            resample = Image.BILINEAR if interpolation == _CV2Fallback.INTER_LINEAR else Image.LANCZOS
            img = img.resize(size, resample)
            return np.array(img)[..., ::-1]

        @staticmethod
        def imencode(ext, frame, params=None):
            img = Image.fromarray(frame[..., ::-1])
            buf = io.BytesIO()
            quality = 85
            if params:
                for idx in range(0, len(params), 2):
                    if params[idx] == _CV2Fallback.IMWRITE_JPEG_QUALITY:
                        quality = params[idx + 1]
                        break
            img.save(buf, format='JPEG', quality=int(quality))
            return True, buf.getvalue()

    cv2 = _CV2Fallback()

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

FIXED_FRAME_WIDTH = 1280
FIXED_FRAME_HEIGHT = 720

parser = argparse.ArgumentParser(description='Remote control server')
parser.add_argument('--password', default=None, help='Optional password required for client authentication')
args = parser.parse_args()

if args.password is None:
    try:
        entered_password = input('Set a password for remote access (leave empty to disable password): ').strip()
        server_password = entered_password or None
    except KeyboardInterrupt:
        print('\nNo password set.')
        server_password = None
else:
    server_password = args.password

manual_quality = None
use_manual_quality = False

def capture_screen_dxgi():
    try:
        pil_img = ImageGrab.grab()
        frame = np.array(pil_img)
        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        return frame
    except Exception:
        try:
            screenshot = pyautogui.screenshot()
            frame = np.array(screenshot)
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            return frame
        except Exception:
            return np.zeros((FIXED_FRAME_HEIGHT, FIXED_FRAME_WIDTH, 3), dtype=np.uint8)

mouse_down_state = {"down": False, "time": 0, "button": None}

cursor_cache = {"b64": None, "hx": 0, "hy": 0, "time": 0}
cursor_lock = threading.Lock()
cursor_worker_active = False

def authenticate_client(frame_con):
    global server_password
    if not server_password:
        return True
    try:
        frame_con.sendall(b"AUTH_REQUIRED\n")
        auth_data = b""
        while b"\n" not in auth_data:
            chunk = frame_con.recv(4096)
            if not chunk:
                return False
            auth_data += chunk
        received = auth_data.decode('utf-8', errors='ignore').strip()
        if not received.startswith('AUTH:'):
            frame_con.sendall(b"AUTH_FAILED\n")
            return False
        provided_password = received[5:].strip()
        if provided_password != server_password:
            frame_con.sendall(b"AUTH_FAILED\n")
            return False
        frame_con.sendall(b"AUTH_OK\n")
        return True
    except Exception:
        return False


def handle_client_connection(frame_con, client_address):
    latency = [0]
    clients_commands = []
    last_cursor_b64 = None
    last_hotspot_x = None
    last_hotspot_y = None
    missing_count = 0
    frame_skip = 0

    if not authenticate_client(frame_con):
        try:
            frame_con.close()
        except Exception:
            pass
        return

    def accept_command_connections():
        cmd_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cmd_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cmd_listener.bind((client_address[0], 0))
        cmd_listener.listen(5)
        cmd_port = cmd_listener.getsockname()[1]
        try:
            frame_con.sendall(f"CMD_PORT:{cmd_port}\n".encode())
        except Exception as e:
            print(f"[remote_server_v2] Failed to send CMD_PORT to {client_address}: {e}")
            try:
                cmd_listener.close()
            except Exception:
                pass
            return
        while True:
            try:
                cmd_con, cmd_addr = cmd_listener.accept()
            except Exception:
                break
            print(f"[remote_server_v2] accepted command connection from {cmd_addr}")
            clients_commands.append(cmd_con)

            def read_from_cmd_con(con):
                buffer = ""
                while True:
                    try:
                        data = con.recv(4096).decode()
                        if not data:
                            break
                        buffer += data
                        while '\n' in buffer:
                            line, buffer = buffer.split('\n', 1)
                            line = line.strip()
                            if not line:
                                continue
                            if line == "DISCONNECT":
                                try:
                                    con.close()
                                except Exception:
                                    pass
                                return
                            if line.startswith("LATENCY:"):
                                try:
                                    latency[0] = int(line.split(":", 1)[1].strip())
                                except Exception:
                                    pass
                                continue
                            if line.startswith("CMD:"):
                                try:
                                    cmd_data = json.loads(line[4:])
                                    execute_command(cmd_data)
                                except Exception:
                                    pass
                    except Exception:
                        try:
                            con.close()
                        except Exception:
                            pass
                        break

            threading.Thread(target=read_from_cmd_con, args=(cmd_con,), daemon=True).start()

    threading.Thread(target=accept_command_connections, daemon=True).start()

    while True:
        try:
            frame = capture_screen_dxgi()

            # select quality: manual override or latency-based
            if use_manual_quality and manual_quality is not None:
                try:
                    quality = int(manual_quality)
                except Exception:
                    quality = 85
            else:
                if latency[0] > 2000:
                    quality = 5
                elif latency[0] > 1000:
                    quality = 10
                elif latency[0] > 500:
                    quality = 20
                elif latency[0] > 300:
                    quality = 35
                elif latency[0] > 150:
                    quality = 50
                elif latency[0] > 80:
                    quality = 70
                else:
                    quality = 85

            # Always resize to fixed frame dimensions to keep client canvas size stable
            target_w, target_h = FIXED_FRAME_WIDTH, FIXED_FRAME_HEIGHT
            orig_h, orig_w = frame.shape[:2]
            # compute integer new size with preserved aspect via scaling, then pad/crop to exact target
            scale = min(float(target_w) / orig_w, float(target_h) / orig_h)
            scaled_w = max(1, int(orig_w * scale))
            scaled_h = max(1, int(orig_h * scale))
            # resize to scaled size first
            if scaled_w != orig_w or scaled_h != orig_h:
                interp = cv2.INTER_LINEAR if scale >= 1.0 else cv2.INTER_AREA
                frame = cv2.resize(frame, (scaled_w, scaled_h), interpolation=interp)
            # create a target canvas and center the scaled frame (letterbox/pillarbox) if needed
            if scaled_w != target_w or scaled_h != target_h:
                canvas_frame = np.zeros((target_h, target_w, 3), dtype=frame.dtype)
                x_off = (target_w - scaled_w) // 2
                y_off = (target_h - scaled_h) // 2
                canvas_frame[y_off:y_off+scaled_h, x_off:x_off+scaled_w] = frame
                frame = canvas_frame
            
            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])

            size = len(buffer)
            
            frame_con.sendall(size.to_bytes(4, 'big'))
            frame_con.sendall(buffer)
            
            with cursor_lock:
                cursor_b64 = cursor_cache["b64"]
                hotspot_x = cursor_cache["hx"]
                hotspot_y = cursor_cache["hy"]
                cursor_fmt = cursor_cache.get("fmt", "raw")
            
            try:
                meta = {}
                if cursor_b64 is not None:
                    missing_count = 0
                    if cursor_b64 != last_cursor_b64 or hotspot_x != last_hotspot_x or hotspot_y != last_hotspot_y:
                        meta = {'cursorImage': cursor_b64, 'cursorHotspotX': hotspot_x, 'cursorHotspotY': hotspot_y, 'cursorFormat': cursor_fmt}
                        last_cursor_b64 = cursor_b64
                        last_hotspot_x = hotspot_x
                        last_hotspot_y = hotspot_y
                else:
                    missing_count += 1
                    if missing_count >= 3 and last_cursor_b64 is not None:
                        meta = { }
                        last_cursor_b64 = None
                        last_hotspot_x = None
                        last_hotspot_y = None

                meta_bytes = json.dumps(meta).encode('utf-8')
                meta_size = len(meta_bytes)
                frame_con.sendall(meta_size.to_bytes(4, 'big'))
                frame_con.sendall(meta_bytes)
            except Exception:
                pass

        except Exception:
            try:
                frame_con.close()
            except Exception:
                pass
            break

def capture_cursor_worker():
    global cursor_cache, cursor_lock
    last_b64 = None
    
    while True:
        try:
            time.sleep(0.033)
            if sys.platform.startswith('win'):
                try:
                    cursor_data = capture_cursor_as_png()
                    if not cursor_data:
                        continue

                    cursor_b64, hotspot_x, hotspot_y = cursor_data
                    if cursor_b64 != last_b64:
                        with cursor_lock:
                            cursor_cache["b64"] = cursor_b64
                            cursor_cache["hx"] = hotspot_x
                            cursor_cache["hy"] = hotspot_y
                            cursor_cache["fmt"] = "png"
                        last_b64 = cursor_b64
                except Exception:
                    last_b64 = None
        except Exception:
            pass

def execute_command(cmd_data):
    cmd_type = cmd_data.get("type")
    if cmd_type == "mouse_move":
        x, y = cmd_data.get("x"), cmd_data.get("y")
        normalized = cmd_data.get("normalized", False)
        try:
            sx, sy = pyautogui.size()
            if normalized and x is not None and y is not None:
                tx = max(0, min(int(float(x) * sx), sx - 1))
                ty = max(0, min(int(float(y) * sy), sy - 1))
            else:
                tx = max(0, min(int(x), sx - 1))
                ty = max(0, min(int(y), sy - 1))
            pyautogui.moveTo(tx, ty, duration=0)
        except Exception:
            pass
    elif cmd_type == "mouse_click":
        button = cmd_data.get("button", "left")
        x, y = cmd_data.get("x"), cmd_data.get("y")
        normalized = cmd_data.get("normalized", False)
        try:
            if x is not None and y is not None:
                sx, sy = pyautogui.size()
                if normalized:
                    tx = max(0, min(int(float(x) * sx), sx - 1))
                    ty = max(0, min(int(float(y) * sy), sy - 1))
                else:
                    tx = max(0, min(int(x), sx - 1))
                    ty = max(0, min(int(y), sy - 1))
                pyautogui.moveTo(tx, ty, duration=0)
        except Exception:
            pass
        try:
            pyautogui.click(button=button)
        except Exception:
            pass
    elif cmd_type == "mouse_down":
        button = cmd_data.get("button", "left")
        x, y = cmd_data.get("x"), cmd_data.get("y")
        normalized = cmd_data.get("normalized", False)
        try:
            if x is not None and y is not None:
                sx, sy = pyautogui.size()
                if normalized:
                    tx = max(0, min(int(float(x) * sx), sx - 1))
                    ty = max(0, min(int(float(y) * sy), sy - 1))
                else:
                    tx = max(0, min(int(x), sx - 1))
                    ty = max(0, min(int(y), sy - 1))
                pyautogui.moveTo(tx, ty, duration=0)
        except Exception:
            pass
        try:
            pyautogui.mouseDown(button=button)
            mouse_down_state["down"] = True
            mouse_down_state["time"] = time.time()
            mouse_down_state["button"] = button
        except Exception:
            pass
    elif cmd_type == "mouse_up":
        button = cmd_data.get("button", "left")
        x, y = cmd_data.get("x"), cmd_data.get("y")
        normalized = cmd_data.get("normalized", False)
        try:
            if x is not None and y is not None:
                sx, sy = pyautogui.size()
                if normalized:
                    tx = max(0, min(int(float(x) * sx), sx - 1))
                    ty = max(0, min(int(float(y) * sy), sy - 1))
                else:
                    tx = max(0, min(int(x), sx - 1))
                    ty = max(0, min(int(y), sy - 1))
                pyautogui.moveTo(tx, ty, duration=0)
        except Exception:
            pass
        try:
            pyautogui.mouseUp(button=button)
            mouse_down_state["down"] = False
            mouse_down_state["button"] = None
        except Exception:
            pass
    elif cmd_type == "mouse_scroll":
        clicks = cmd_data.get("clicks", 1)
        try:
            pyautogui.scroll(int(clicks))
        except Exception:
            pass
    elif cmd_type == "key_press":
        key = cmd_data.get("key")
        try:
            pyautogui.press(key)
        except Exception:
            pass
    elif cmd_type == "key_down":
        key = cmd_data.get("key")
        try:
            pyautogui.keyDown(key)
        except Exception:
            pass
    elif cmd_type == "key_up":
        key = cmd_data.get("key")
        try:
            pyautogui.keyUp(key)
        except Exception:
            pass
    elif cmd_type == "write":
        text = cmd_data.get("text", "")
        try:
            pyautogui.write(text, interval=0)
        except Exception:
            pass
    elif cmd_type == "set_quality":
        # command from client to control encoding quality
        # expected payload: { type: 'set_quality', mode: 'auto'|'manual', quality: <int> }
        global manual_quality, use_manual_quality
        try:
            mode = cmd_data.get('mode')
            if mode == 'manual':
                q = cmd_data.get('quality')
                if q is not None:
                    manual_quality = int(q)
                    use_manual_quality = True
            else:
                # auto
                use_manual_quality = False
                manual_quality = None
        except Exception:
            pass

def tcp_server():
    global port
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    local_ip = get_local_ip()
    while True:
        try:
            server.bind((local_ip, port))
            break
        except OSError as e:
            if e.errno == 10048:
                port += 1
            else:
                raise
    server.listen(5)
    while True:
        try:
            con, address = server.accept()
            threading.Thread(target=handle_client_connection, args=(con, address), daemon=True).start()
        except Exception:
            pass

def udp_broadcast_listener():
    global port
    broadcast_listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    broadcast_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    broadcast_listener.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    broadcast_listener.bind(("", 45))
    while True:
        try:
            data, addr = broadcast_listener.recvfrom(1024)
            if data.decode() == "DISCOVER_SERVER":
                response = f"{get_local_ip()}:{port}"
                broadcast_listener.sendto(response.encode(), addr)
        except Exception:
            pass

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    finally:
        s.close()
    return local_ip

port = 1
cursor_thread = threading.Thread(target=capture_cursor_worker, daemon=True)
tcp_thread = threading.Thread(target=tcp_server, daemon=True)
udp_thread = threading.Thread(target=udp_broadcast_listener, daemon=True)
cursor_thread.start()
tcp_thread.start()
udp_thread.start()
try:
    tcp_thread.join()
    udp_thread.join()
except KeyboardInterrupt:
    pass
