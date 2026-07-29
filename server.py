import argparse
import os
import socket
import threading
import numpy as np
import time
import json
import base64
import io
import sys
import ctypes
import uuid
import hashlib
import asyncio
from urllib.parse import urlparse, parse_qs
from ctypes import wintypes
import socketio
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
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
parser.add_argument('--relay-url', default=os.environ.get('RELAY_URL', 'https://remote-control-ee7w.onrender.com'), help='Render Socket.IO relay URL')
parser.add_argument('--server-id', default=None, help='Unique ID for this server')
args = parser.parse_args()

if not args.server_id:
    host_name = socket.gethostname().replace(' ', '-').lower()
    args.server_id = f"{host_name}-{uuid.uuid4().hex[:8]}"

relay_socket = socketio.Client(logger=False, engineio_logger=False)
relay_connected = False
authorized_browsers = {}
webrtc_sessions = {}
webrtc_sessions_lock = threading.Lock()
AUTH_TIMEOUT = 60 * 30


def connect_to_relay():
    global relay_socket, relay_connected
    try:
        relay_socket.connect(args.relay_url, transports=['websocket'])
        relay_connected = True
        print(f'[relay] direct_host={get_local_ip()} direct_port={port}')
        relay_socket.emit('register_server', {
            'server_id': args.server_id,
            'name': socket.gethostname(),
            'hostname': socket.gethostname(),
            'address': args.server_id,
            'direct_host': get_local_ip(),
            'direct_port': port,
            'password_protected': bool(server_password),
        })
        threading.Thread(target=relay_heartbeat, daemon=True).start()
    except Exception as e:
        print(f'[relay] connection failed: {e}')


def relay_heartbeat():
    while True:
        try:
            if relay_connected:
                relay_socket.emit('server_heartbeat', {'server_id': args.server_id})
        except Exception:
            pass
        time.sleep(5)


frame_seq = 0
latest_payload = None
payload_lock = threading.Lock()


def relay_frame_sender():
    global frame_seq, latest_payload
    while True:
        try:
            if not relay_connected or not authorized_browsers:
                time.sleep(0.25)
                continue

            frame = capture_screen_dxgi()
            if frame is None:
                time.sleep(0.25)
                continue

            try:
                if use_manual_quality and manual_quality is not None:
                    try:
                        quality = int(manual_quality)
                    except Exception:
                        quality = 70
                else:
                    quality = 70
                _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            except Exception:
                time.sleep(0.25)
                continue

            encoded_frame = base64.b64encode(buffer).decode('ascii')
            with cursor_lock:
                cursor_b64 = cursor_cache.get('b64')
                hotspot_x = cursor_cache.get('hx', 0)
                hotspot_y = cursor_cache.get('hy', 0)
                cursor_fmt = cursor_cache.get('fmt', 'png')

            frame_seq += 1
            payload = {
                'browser_sid': None,
                'frame_id': frame_seq,
                'frame': encoded_frame,
                'cursorImage': cursor_b64,
                'cursorHotspotX': hotspot_x,
                'cursorHotspotY': hotspot_y,
                'cursorFormat': cursor_fmt,
            }

            with payload_lock:
                latest_payload = payload
        except Exception:
            pass
        time.sleep(0.1)


def relay_frame_transmitter():
    global latest_payload
    while True:
        try:
            if not relay_connected or not authorized_browsers:
                time.sleep(0.25)
                continue

            with payload_lock:
                payload = latest_payload
                latest_payload = None

            if not payload:
                time.sleep(0.05)
                continue

            for browser_sid in list(authorized_browsers.keys()):
                frame_payload = payload.copy()
                frame_payload['browser_sid'] = browser_sid
                relay_socket.emit('server_frame', frame_payload)
        except Exception:
            pass
        time.sleep(0.05)


def _cleanup_authorized_browsers():
    while True:
        try:
            now = time.time()
            for sid, exp in list(authorized_browsers.items()):
                if exp <= now:
                    del authorized_browsers[sid]
        except Exception:
            pass
        time.sleep(30)


@relay_socket.on('request_session')
def on_request_session(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    req_password = data.get('password')
    if server_password:
        if not req_password or req_password != server_password:
            if browser_sid:
                relay_socket.emit('session_denied', {
                    'browser_sid': browser_sid,
                    'server_id': args.server_id,
                    'reason': 'auth_failed'
                })
            return

    if browser_sid:
        try:
            authorized_browsers[browser_sid] = time.time() + AUTH_TIMEOUT
        except Exception:
            pass
        relay_socket.emit('session_ready', {'browser_sid': browser_sid, 'server_id': args.server_id})


@relay_socket.on('relay_command')
def on_relay_command(data):
    data = data or {}
    cmd = data.get('cmd')
    browser_sid = data.get('browser_sid')
    is_auth = False
    if browser_sid:
        exp = authorized_browsers.get(browser_sid)
        if exp and exp > time.time():
            is_auth = True
        else:
            # cleanup if expired
            authorized_browsers.pop(browser_sid, None)

    if not is_auth:
        try:
            relay_socket.emit('session_denied', {
                'browser_sid': browser_sid,
                'server_id': args.server_id,
                'reason': 'not_authorized'
            })
        except Exception:
            pass
        return

    if cmd:
        execute_command(cmd)


@relay_socket.on('end_session')
def on_end_session(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    if browser_sid:
        authorized_browsers.pop(browser_sid, None)


@relay_socket.on('session_denied')
def on_session_denied(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    if browser_sid:
        authorized_browsers.pop(browser_sid, None)


@relay_socket.on('webrtc_offer')
def on_webrtc_offer(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    offer = data.get('offer')
    if not browser_sid or not offer:
        return
    threading.Thread(target=lambda: _run_webrtc_offer(browser_sid, offer), daemon=True).start()


@relay_socket.on('webrtc_candidate')
def on_webrtc_candidate(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    candidate = data.get('candidate')
    target = (data.get('target') or 'browser').lower()
    if not browser_sid or not candidate or target != 'browser':
        return
    with webrtc_sessions_lock:
        session = webrtc_sessions.get(browser_sid)
    if not session:
        return
    try:
        if isinstance(candidate, dict):
            candidate_obj = RTCIceCandidate(
                candidate=candidate.get('candidate'),
                sdpMid=candidate.get('sdpMid'),
                sdpMLineIndex=candidate.get('sdpMLineIndex'),
            )
            session['pc'].addIceCandidate(candidate_obj)
        else:
            session['pc'].addIceCandidate(candidate)
    except Exception:
        pass


def _run_webrtc_offer(browser_sid, offer):
    async def _async_handle_offer():
        pc = RTCPeerConnection()
        session = {'pc': pc, 'browser_sid': browser_sid, 'channel': None, 'open': False}
        with webrtc_sessions_lock:
            webrtc_sessions[browser_sid] = session

        @pc.on("datachannel")
        def on_datachannel(channel):
            session['channel'] = channel

            @channel.on("open")
            def on_open():
                session['open'] = True
                threading.Thread(target=lambda: _send_webrtc_frames(browser_sid), daemon=True).start()

            @channel.on("message")
            def on_message(message):
                try:
                    if isinstance(message, bytes):
                        payload = message.decode('utf-8', errors='ignore')
                    else:
                        payload = message
                    data = json.loads(payload)
                    if isinstance(data, dict):
                        execute_command(data)
                except Exception:
                    pass

            @channel.on("close")
            def on_close():
                session['open'] = False
                with webrtc_sessions_lock:
                    webrtc_sessions.pop(browser_sid, None)

        @pc.on("icecandidate")
        def on_icecandidate(event):
            if event.candidate:
                try:
                    relay_socket.emit('webrtc_candidate', {
                        'browser_sid': browser_sid,
                        'candidate': event.candidate.to_json(),
                        'target': 'browser',
                    })
                except Exception:
                    pass

        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer['sdp'], type=offer['type']))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        relay_socket.emit('webrtc_answer', {
            'browser_sid': browser_sid,
            'answer': {
                'type': pc.localDescription.type,
                'sdp': pc.localDescription.sdp,
            },
        })

    try:
        asyncio.run(_async_handle_offer())
    except Exception as exc:
        print(f'[webrtc] offer failed: {exc}')


def _send_webrtc_frames(browser_sid):
    while True:
        try:
            with webrtc_sessions_lock:
                session = webrtc_sessions.get(browser_sid)
                channel = session.get('channel') if session else None
                is_open = bool(session and session.get('open')) if session else False
            if not is_open or not channel:
                return

            frame = capture_screen_dxgi()
            if frame is None:
                time.sleep(0.05)
                continue

            if use_manual_quality and manual_quality is not None:
                try:
                    quality = int(manual_quality)
                except Exception:
                    quality = 70
            else:
                quality = 70

            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            encoded_frame = base64.b64encode(buffer).decode('ascii')
            with cursor_lock:
                cursor_b64 = cursor_cache.get('b64')
                hotspot_x = cursor_cache.get('hx', 0)
                hotspot_y = cursor_cache.get('hy', 0)
                cursor_fmt = cursor_cache.get('fmt', 'png')

            payload = {
                'type': 'frame',
                'frame_id': frame_seq,
                'frame': encoded_frame,
                'cursorImage': cursor_b64,
                'cursorHotspotX': hotspot_x,
                'cursorHotspotY': hotspot_y,
                'cursorFormat': cursor_fmt,
            }
            channel.send(json.dumps(payload))
            time.sleep(0.05)
        except Exception:
            break


if args.password is None:
    try:
        while True:
            entered_password = input('Set a password for remote access: ').strip()
            if entered_password:
                server_password = entered_password
                break
            print('Password cannot be empty. Please enter a non-empty password.')
    except KeyboardInterrupt:
        print('\nPassword is required. Exiting.')
        sys.exit(1)
else:
    if not args.password.strip():
        print('Error: password cannot be empty.')
        sys.exit(1)
    server_password = args.password.strip()

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

def _recv_exact(sock, size):
    data = b''
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _read_http_request(sock):
    data = b''
    while b'\r\n\r\n' not in data:
        chunk = sock.recv(4096)
        if not chunk:
            return None
        data += chunk
    headers, _sep, _rest = data.partition(b'\r\n\r\n')
    try:
        text = headers.decode('utf-8', errors='ignore')
    except Exception:
        return None
    lines = text.split('\r\n')
    if not lines:
        return None
    request_line = lines[0].split()
    if len(request_line) < 3:
        return None
    method, path = request_line[0], request_line[1]
    header_lines = lines[1:]
    headers = {}
    for line in header_lines:
        if ':' in line:
            name, value = line.split(':', 1)
            headers[name.strip().lower()] = value.strip()
    return method, path, headers


def _send_http_response(sock, status_code, reason, headers=None, body=b''):
    if headers is None:
        headers = {}
    headers = {k.lower(): v for k, v in headers.items()}
    response_lines = [f'HTTP/1.1 {status_code} {reason}']
    response_lines.append('Connection: close')
    for name, value in headers.items():
        response_lines.append(f'{name}: {value}')
    response_lines.append(f'Content-Length: {len(body)}')
    response_lines.append('')
    response_lines.append('')
    response = '\r\n'.join(response_lines).encode('utf-8') + body
    sock.sendall(response)


def _send_ws_message(sock, data, opcode=2):
    if isinstance(data, str):
        payload = data.encode('utf-8')
    else:
        payload = data
    header = bytearray()
    fin_and_opcode = 0x80 | (opcode & 0x0f)
    header.append(fin_and_opcode)
    length = len(payload)
    if length <= 125:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header.extend(length.to_bytes(2, 'big'))
    else:
        header.append(127)
        header.extend(length.to_bytes(8, 'big'))
    sock.sendall(bytes(header) + payload)


def _recv_ws_frame(sock):
    header = _recv_exact(sock, 2)
    if not header:
        return None, None
    b1, b2 = header
    opcode = b1 & 0x0f
    masked = bool(b2 & 0x80)
    length = b2 & 0x7f
    if length == 126:
        ext = _recv_exact(sock, 2)
        if not ext:
            return None, None
        length = int.from_bytes(ext, 'big')
    elif length == 127:
        ext = _recv_exact(sock, 8)
        if not ext:
            return None, None
        length = int.from_bytes(ext, 'big')
    mask_key = _recv_exact(sock, 4) if masked else None
    if mask_key is None and masked:
        return None, None
    payload = _recv_exact(sock, length) if length else b''
    if payload is None:
        return None, None
    if masked and mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _perform_websocket_handshake(frame_con):
    request = _read_http_request(frame_con)
    if not request:
        return False
    method, path, headers = request
    if method != 'GET' or headers.get('upgrade', '').lower() != 'websocket' or 'sec-websocket-key' not in headers:
        _send_http_response(frame_con, 400, 'Bad Request')
        return False
    parsed = urlparse(path)
    if parsed.path != '/ws':
        _send_http_response(frame_con, 404, 'Not Found')
        return False
    query = parse_qs(parsed.query)
    provided_password = query.get('password', [None])[0]
    if server_password and provided_password != server_password:
        _send_http_response(frame_con, 401, 'Unauthorized')
        return False
    key = headers['sec-websocket-key'].strip()
    accept_raw = hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('utf-8')).digest()
    accept = base64.b64encode(accept_raw).decode('ascii')
    response_headers = {
        'Upgrade': 'websocket',
        'Connection': 'Upgrade',
        'Sec-WebSocket-Accept': accept,
    }
    response_lines = ['HTTP/1.1 101 Switching Protocols']
    for h, v in response_headers.items():
        response_lines.append(f'{h}: {v}')
    response_lines.append('')
    response_lines.append('')
    frame_con.sendall('\r\n'.join(response_lines).encode('utf-8'))
    return True


def handle_client_connection(frame_con, client_address):
    if not _perform_websocket_handshake(frame_con):
        try:
            frame_con.close()
        except Exception:
            pass
        return

    active = {'running': True}
    last_cursor_b64 = None
    last_hotspot_x = None
    last_hotspot_y = None
    missing_count = 0

    def command_reader():
        while active['running']:
            opcode, payload = _recv_ws_frame(frame_con)
            if opcode is None:
                break
            if opcode == 8:
                break
            if opcode == 9:
                try:
                    _send_ws_message(frame_con, payload or b'', opcode=10)
                except Exception:
                    break
                continue
            if opcode == 1:
                try:
                    message = payload.decode('utf-8', errors='ignore')
                    data = json.loads(message)
                    if isinstance(data, dict):
                        execute_command(data)
                except Exception:
                    pass
                continue
        active['running'] = False
        try:
            frame_con.close()
        except Exception:
            pass

    threading.Thread(target=command_reader, daemon=True).start()

    while active['running']:
        try:
            frame = capture_screen_dxgi()
            if frame is None:
                time.sleep(0.025)
                continue

            if use_manual_quality and manual_quality is not None:
                try:
                    quality = int(manual_quality)
                except Exception:
                    quality = 85
            else:
                quality = 85

            target_w, target_h = FIXED_FRAME_WIDTH, FIXED_FRAME_HEIGHT
            orig_h, orig_w = frame.shape[:2]
            scale = min(float(target_w) / orig_w, float(target_h) / orig_h)
            scaled_w = max(1, int(orig_w * scale))
            scaled_h = max(1, int(orig_h * scale))
            if scaled_w != orig_w or scaled_h != orig_h:
                interp = cv2.INTER_LINEAR if scale >= 1.0 else cv2.INTER_AREA
                frame = cv2.resize(frame, (scaled_w, scaled_h), interpolation=interp)
            if scaled_w != target_w or scaled_h != target_h:
                canvas_frame = np.zeros((target_h, target_w, 3), dtype=frame.dtype)
                x_off = (target_w - scaled_w) // 2
                y_off = (target_h - scaled_h) // 2
                canvas_frame[y_off:y_off+scaled_h, x_off:x_off+scaled_w] = frame
                frame = canvas_frame

            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            _send_ws_message(frame_con, buffer, opcode=2)

            with cursor_lock:
                cursor_b64 = cursor_cache['b64']
                hotspot_x = cursor_cache['hx']
                hotspot_y = cursor_cache['hy']
                cursor_fmt = cursor_cache.get('fmt', 'raw')

            if cursor_b64 is not None:
                if cursor_b64 != last_cursor_b64 or hotspot_x != last_hotspot_x or hotspot_y != last_hotspot_y:
                    cursor_payload = json.dumps({
                        'type': 'cursor',
                        'cursorImage': cursor_b64,
                        'cursorHotspotX': hotspot_x,
                        'cursorHotspotY': hotspot_y,
                        'cursorFormat': cursor_fmt,
                    })
                    _send_ws_message(frame_con, cursor_payload, opcode=1)
                    last_cursor_b64 = cursor_b64
                    last_hotspot_x = hotspot_x
                    last_hotspot_y = hotspot_y
            else:
                if last_cursor_b64 is not None:
                    cursor_payload = json.dumps({'type': 'cursor', 'cursorRemoved': True})
                    _send_ws_message(frame_con, cursor_payload, opcode=1)
                    last_cursor_b64 = None
                    last_hotspot_x = None
                    last_hotspot_y = None

            time.sleep(0.02)
        except Exception:
            active['running'] = False
            break

    try:
        frame_con.close()
    except Exception:
        pass

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
    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except Exception:
        pass
    while True:
        try:
            server.bind((local_ip, 0))
            port = server.getsockname()[1]
            break
        except OSError:
            time.sleep(0.1)
        except Exception:
            port_ready.set()
            return
    server.listen(5)
    port_ready.set()
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
    try:
        broadcast_listener.bind(("0.0.0.0", 45))
    except OSError:
        try:
            broadcast_listener.bind(("", 45))
        except Exception:
            return
    while True:
        try:
            data, addr = broadcast_listener.recvfrom(1024)
            if data.decode(errors='ignore').strip() == "DISCOVER_SERVER":
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
port_ready = threading.Event()
cursor_thread = threading.Thread(target=capture_cursor_worker, daemon=True)
tcp_thread = threading.Thread(target=tcp_server, daemon=True)
udp_thread = threading.Thread(target=udp_broadcast_listener, daemon=True)
relay_frame_thread = threading.Thread(target=relay_frame_sender, daemon=True)
relay_transmitter_thread = threading.Thread(target=relay_frame_transmitter, daemon=True)
cursor_thread.start()
tcp_thread.start()
port_ready.wait()
udp_thread.start()
relay_frame_thread.start()
relay_transmitter_thread.start()
connect_to_relay()
try:
    threading.Thread(target=_cleanup_authorized_browsers, daemon=True).start()
except Exception:
    pass
try:
    tcp_thread.join()
    udp_thread.join()
except KeyboardInterrupt:
    pass
