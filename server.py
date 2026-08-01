import argparse
import os
import socket
import threading
import signal
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
import logging
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from ctypes import wintypes
import socketio
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
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

VERBOSE = False
LOG_DIR = Path(__file__).resolve().parent / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

DEBUG_LOG = LOG_DIR / 'debug.log'
INFO_LOG = LOG_DIR / 'info.log'
ERROR_LOG = LOG_DIR / 'error.log'

DEBUG_LOGGER = logging.getLogger('remote_control.debug')
INFO_LOGGER = logging.getLogger('remote_control.info')
ERROR_LOGGER = logging.getLogger('remote_control.error')

for logger in (DEBUG_LOGGER, INFO_LOGGER, ERROR_LOGGER):
    logger.setLevel(logging.DEBUG)
    logger.propagate = False


def _attach_file_handler(logger, path, level):
    path.parent.mkdir(parents=True, exist_ok=True)
    for existing_handler in list(logger.handlers):
        if isinstance(existing_handler, logging.FileHandler) and existing_handler.baseFilename == str(path):
            return
    handler = logging.FileHandler(path, encoding='utf-8')
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger.addHandler(handler)


_attach_file_handler(DEBUG_LOGGER, DEBUG_LOG, logging.DEBUG)
_attach_file_handler(INFO_LOGGER, INFO_LOG, logging.INFO)
_attach_file_handler(ERROR_LOGGER, ERROR_LOG, logging.ERROR)


def _write_log(logger, message, level):
    logger.log(level, message)


def dbg(msg):
    if VERBOSE:
        print(f'[debug] {msg}')
    _write_log(DEBUG_LOGGER, msg, logging.DEBUG)


def info(msg):
    print(f'[info] {msg}')
    _write_log(INFO_LOGGER, msg, logging.INFO)


def error(msg):
    print(f'[error] {msg}')
    _write_log(ERROR_LOGGER, msg, logging.ERROR)

def loop_running(loop):
    try:
        return loop is not None and (not loop.is_closed()) and loop.is_running()
    except Exception:
        return False

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
parser.add_argument('--rc-verbose', action='store_true', help='Enable verbose debug prints')
args = parser.parse_args()
VERBOSE = bool(getattr(args, 'rc_verbose', False))

if not args.server_id:
    host_name = socket.gethostname().replace(' ', '-').lower()
    args.server_id = f"{host_name}-{uuid.uuid4().hex[:8]}"

relay_socket = socketio.Client(
    logger=False,
    engineio_logger=False,
    reconnection=True,
    reconnection_attempts=0,
    reconnection_delay=1,
    reconnection_delay_max=5,
)
relay_connected = False
webrtc_sessions = {}
webrtc_sessions_lock = threading.Lock()
webrtc_loop = None
webrtc_loop_thread = None
webrtc_loop_ready = None
AUTH_TIMEOUT = 60 * 30
tray_icon = None
shutdown_event = threading.Event()


def connect_to_relay():
    global relay_socket, relay_connected
    try:
        if shutdown_event.is_set():
            return
        if getattr(relay_socket, 'connected', False):
            relay_connected = True
            return

        dbg(f'connecting to relay {args.relay_url}')
        try:
            relay_socket.disconnect()
        except Exception:
            pass
        relay_socket.connect(args.relay_url, transports=['websocket'])
    except Exception as e:
        relay_connected = False
        error(f'relay connection failed: {e}')


def relay_reconnect_loop():
    while True:
        try:
            if shutdown_event.is_set():
                break
            if not relay_connected or not getattr(relay_socket, 'connected', False):
                connect_to_relay()
                if relay_connected:
                    threading.Thread(target=relay_heartbeat, daemon=True).start()
            time.sleep(5)
        except Exception as exc:
            error(f'relay reconnect loop error: {exc}')
            if shutdown_event.is_set():
                break
            time.sleep(5)


def relay_heartbeat():
    while True:
        try:
            global relay_connected
            if shutdown_event.is_set():
                break
            if relay_connected:
                relay_socket.emit('server_heartbeat', {'server_id': args.server_id})
        except Exception as exc:
            error(f'[relay] heartbeat failed: {exc}')
            relay_connected = False
            break
        time.sleep(5)


def _create_webrtc_event_loop():
    loop = asyncio.new_event_loop()
    def loop_runner():
        asyncio.set_event_loop(loop)
        loop.run_forever()
    thread = threading.Thread(target=loop_runner, daemon=True)
    thread.start()
    return loop, thread


frame_seq = 0


@relay_socket.on('request_session')
def on_request_session(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    req_password = data.get('password')
    dbg(f'request_session browser_sid={browser_sid}')
    if server_password:
        if not req_password or req_password != server_password:
            info('auth failed for browser ' + str(browser_sid))
            if browser_sid:
                relay_socket.emit('session_denied', {
                    'browser_sid': browser_sid,
                    'server_id': args.server_id,
                    'reason': 'auth_failed'
                })
            return

    if browser_sid:
        dbg('authorizing browser ' + str(browser_sid))
        relay_socket.emit('session_ready', {'browser_sid': browser_sid, 'server_id': args.server_id})


@relay_socket.on('end_session')
def on_end_session(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    if browser_sid:
        with webrtc_sessions_lock:
            webrtc_sessions.pop(browser_sid, None)


@relay_socket.on('session_denied')
def on_session_denied(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    info(f'session_denied browser_sid={browser_sid} data={data}')
    if browser_sid:
        with webrtc_sessions_lock:
            webrtc_sessions.pop(browser_sid, None)


@relay_socket.on('connect')
def on_relay_connect():
    global relay_connected
    if shutdown_event.is_set():
        return
    relay_connected = True
    info('relay socket connected')
    dbg(f'relay connected, direct_host={get_local_ip()} direct_port={port}')
    try:
        relay_socket.emit('register_server', {
            'server_id': args.server_id,
            'name': socket.gethostname(),
            'hostname': socket.gethostname(),
            'address': args.server_id,
            'direct_host': get_local_ip(),
            'direct_port': port,
        })
    except Exception as exc:
        error(f're-register server failed after reconnect: {exc}')


@relay_socket.on('disconnect')
def on_relay_disconnect():
    global relay_connected
    relay_connected = False
    info('relay socket disconnected')
    if shutdown_event.is_set():
        return
    with webrtc_sessions_lock:
        session_ids = list(webrtc_sessions.keys())
    for sid in session_ids:
        try:
            with webrtc_sessions_lock:
                session = webrtc_sessions.get(sid)
            if not session:
                continue
            session['open'] = False
            task = session.get('frame_task')
            if task is not None:
                try:
                    task.cancel()
                except Exception:
                    pass
            pc = session.get('pc')
            loop = session.get('loop')
            if pc is not None and loop_running(loop):
                try:
                    asyncio.run_coroutine_threadsafe(pc.close(), loop)
                except Exception as exc:
                    error(f'error closing pc for browser_sid={sid}: {exc}')
        except Exception:
            pass
        with webrtc_sessions_lock:
            webrtc_sessions.pop(sid, None)


def _build_candidate(candidate_data):
    try:
        candidate_sdp = None
        if isinstance(candidate_data, dict):
            candidate_sdp = candidate_data.get('candidate')
            if isinstance(candidate_sdp, dict):
                candidate_sdp = candidate_sdp.get('candidate')
        elif isinstance(candidate_data, str):
            candidate_sdp = candidate_data

        if not candidate_sdp:
            return None

        candidate_sdp = candidate_sdp.strip()
        ice_candidate = candidate_from_sdp(candidate_sdp)

        if isinstance(candidate_data, dict):
            if candidate_data.get('sdpMid') is not None:
                ice_candidate.sdpMid = candidate_data.get('sdpMid')
            if candidate_data.get('sdpMLineIndex') is not None:
                ice_candidate.sdpMLineIndex = candidate_data.get('sdpMLineIndex')
        return ice_candidate
    except Exception as exc:
        error(f'build candidate failed candidate_data={candidate_data} exc={exc}')
        return None


@relay_socket.on('webrtc_offer')
def on_webrtc_offer(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    offer = data.get('offer')
    info(f'received webrtc_offer browser_sid={browser_sid} offer_present={bool(offer)}')
    dbg(f'offer payload keys={list(offer.keys()) if isinstance(offer, dict) else type(offer)}')
    if not browser_sid or not offer:
        info('[server] invalid offer payload or missing browser_sid')
        return

    with webrtc_sessions_lock:
        session = webrtc_sessions.get(browser_sid)
        if not session:
            webrtc_sessions[browser_sid] = {
                'pc': None,
                'browser_sid': browser_sid,
                'channel': None,
                'open': False,
                'candidate_queue': [],
                'loop': None,
                'remote_description_set': False,
            }
        else:
            session.setdefault('candidate_queue', [])
            if 'loop' not in session:
                session['loop'] = None
            if 'remote_description_set' not in session:
                session['remote_description_set'] = False

    threading.Thread(target=lambda: _run_webrtc_offer(browser_sid, offer), daemon=True).start()


@relay_socket.on('webrtc_candidate')
def on_webrtc_candidate(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    candidate = data.get('candidate')
    target = (data.get('target') or 'browser').lower()
    dbg(f'webrtc_candidate browser_sid={browser_sid} target={target} candidate_present={bool(candidate)}')
    if not browser_sid or not candidate or target != 'server':
        return

    with webrtc_sessions_lock:
        session = webrtc_sessions.get(browser_sid)
        if not session:
            dbg('buffering candidate before session created ' + str(browser_sid))
            webrtc_sessions[browser_sid] = {
                'pc': None,
                'browser_sid': browser_sid,
                'channel': None,
                'open': False,
                'candidate_queue': [candidate],
                'loop': None,
                'remote_description_set': False,
            }
            return

        if session.get('pc') is None or session.get('loop') is None or not session.get('remote_description_set', False):
            session.setdefault('candidate_queue', []).append(candidate)
            dbg('queued candidate until PC exists or remote description set ' + str(browser_sid))
            dbg(f'queue_length={len(session["candidate_queue"])} browser_sid={browser_sid}')
            return

        ice_candidate = _build_candidate(candidate)
        if ice_candidate is None:
            error('candidate parse failed ' + str(browser_sid))
            dbg(f'bad candidate payload={candidate}')
            return

        if not loop_running(session.get('loop')):
            dbg(f'skipping addIceCandidate; session loop not running browser_sid={browser_sid}')
            return
        future = asyncio.run_coroutine_threadsafe(session['pc'].addIceCandidate(ice_candidate), session['loop'])
        def _candidate_done(f):
            exc = f.exception()
            if exc is not None:
                error(f'addIceCandidate failed browser_sid={browser_sid}: {repr(exc)} candidate={candidate}')
            else:
                dbg(f'added ICE candidate from browser browser_sid={browser_sid}')
        future.add_done_callback(_candidate_done)


def _run_webrtc_offer(browser_sid, offer):
    async def _async_handle_offer():
        pc = RTCPeerConnection(configuration=RTCConfiguration([
            RTCIceServer(urls=['stun:stun.l.google.com:19302']),
        ]))
        with webrtc_sessions_lock:
            session = webrtc_sessions.get(browser_sid)
            if not session:
                session = {
                    'pc': None,
                    'browser_sid': browser_sid,
                    'channel': None,
                    'open': False,
                    'candidate_queue': [],
                    'loop': None,
                    'remote_description_set': False,
                }
                webrtc_sessions[browser_sid] = session
            session['pc'] = pc
            session['channel'] = None
            session['open'] = False
            session.setdefault('candidate_queue', [])
            session['loop'] = asyncio.get_running_loop()
            session['remote_description_set'] = False

        @pc.on("datachannel")
        def on_datachannel(channel):
            info(f'datachannel created for browser_sid={browser_sid} id={channel.label}')
            dbg(f'datachannel protocol={channel.protocol} negotiated={channel.negotiated} readyState={channel.readyState}')
            session['channel'] = channel

            def start_frame_sender():
                if session.get('frame_task') is not None:
                    try:
                        done = session['frame_task'].done()
                    except Exception:
                        done = True
                    if not done:
                        return
                if channel.readyState != 'open':
                    dbg(f'datachannel not open yet for browser_sid={browser_sid} readyState={channel.readyState}')
                    return
                session['open'] = True
                info(f'datachannel open, starting frame sender for browser_sid={browser_sid}')
                coro = _send_webrtc_frames(browser_sid)
                try:
                    loop = asyncio.get_running_loop()
                    if loop is session['loop']:
                        session['frame_task'] = asyncio.ensure_future(coro)
                    else:
                        if loop_running(session.get('loop')):
                            session['frame_task'] = asyncio.run_coroutine_threadsafe(coro, session['loop'])
                        else:
                            dbg(f"not scheduling frame sender; session loop not running for browser_sid={browser_sid}")
                except RuntimeError:
                    if loop_running(session.get('loop')):
                        session['frame_task'] = asyncio.run_coroutine_threadsafe(coro, session['loop'])
                    else:
                        dbg(f"runtimeerror: session loop not running for browser_sid={browser_sid}")

            @channel.on("open")
            def on_open():
                info(f'datachannel open event for browser_sid={browser_sid}')
                start_frame_sender()

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
                    else:
                        info(f'datachannel received non-object payload for browser_sid={browser_sid}: {payload}')
                except Exception as exc:
                    error(f'datachannel message failed for browser_sid={browser_sid}: {repr(exc)}')

            @channel.on("close")
            def on_close():
                info(f'datachannel closed for browser_sid={browser_sid}')
                session['open'] = False
                task = session.get('frame_task')
                if task is not None:
                    try:
                        task.cancel()
                    except Exception:
                        pass
                with webrtc_sessions_lock:
                    webrtc_sessions.pop(browser_sid, None)

            if channel.readyState == 'open':
                start_frame_sender()

        @pc.on("icecandidate")
        def on_icecandidate(event):
            if event.candidate:
                try:
                    relay_socket.emit('webrtc_candidate', {
                        'browser_sid': browser_sid,
                        'candidate': event.candidate.to_json(),
                        'target': 'browser',
                    })
                    dbg(f'emitted ICE candidate to browser browser_sid={browser_sid}')
                except Exception as exc:
                    error(f'emit candidate failed {exc}')

        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer['sdp'], type=offer['type']))
        with webrtc_sessions_lock:
            session = webrtc_sessions.get(browser_sid)
            if session:
                session['remote_description_set'] = True
        info(f'remote description set for browser_sid={browser_sid}')
        dbg(f'peer connection state after remote description: {pc.connectionState} iceConnectionState={pc.iceConnectionState}')
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        info(f'local description set for browser_sid={browser_sid}')
        dbg(f'created answer for browser_sid={browser_sid} sdp_length={len(pc.localDescription.sdp) if pc.localDescription else 0}')
        relay_socket.emit('webrtc_answer', {
            'browser_sid': browser_sid,
            'answer': {
                'type': pc.localDescription.type,
                'sdp': pc.localDescription.sdp,
            },
        })

        queued_candidates = []
        with webrtc_sessions_lock:
            session = webrtc_sessions.get(browser_sid)
            if session:
                session['pc'] = pc
                queued_candidates = session.pop('candidate_queue', [])
        for queued_candidate in queued_candidates:
            ice_candidate = _build_candidate(queued_candidate)
            if ice_candidate is None:
                error(f'queued candidate parse failed browser_sid={browser_sid} queued_candidate={queued_candidate}')
                continue
            try:
                await pc.addIceCandidate(ice_candidate)
            except Exception as exc:
                error(f'addIceCandidate failed for queued candidate browser_sid={browser_sid}: {repr(exc)} queued_candidate={queued_candidate}')
    global webrtc_loop, webrtc_loop_thread
    if webrtc_loop is None:
        webrtc_loop, webrtc_loop_thread = _create_webrtc_event_loop()

    with webrtc_sessions_lock:
        session = webrtc_sessions.get(browser_sid)
        if session is None:
            session = {
                'pc': None,
                'browser_sid': browser_sid,
                'channel': None,
                'open': False,
                'candidate_queue': [],
                'loop': webrtc_loop,
            }
            webrtc_sessions[browser_sid] = session
        else:
            session['loop'] = webrtc_loop

    future = asyncio.run_coroutine_threadsafe(_async_handle_offer(), webrtc_loop)
    def _offer_done(f):
        try:
            f.result()
        except Exception as exc:
            error(f'[webrtc] offer task failed: {exc}')
    future.add_done_callback(_offer_done)


async def _send_webrtc_frames(browser_sid):
    global frame_seq
    dbg(f'_send_webrtc_frames starting for browser_sid={browser_sid}')
    dbg(f'initial frame_seq={frame_seq}')
    while True:
        try:
            with webrtc_sessions_lock:
                session = webrtc_sessions.get(browser_sid)
                channel = session.get('channel') if session else None
                is_open = bool(session and session.get('open')) if session else False
            if not is_open or not channel or channel.readyState != 'open':
                dbg(f'_send_webrtc_frames stopping; channel not open for browser_sid={browser_sid}')
                return

            frame = await asyncio.to_thread(capture_screen_dxgi)
            if frame is None:
                await asyncio.sleep(0.05)
                continue

            if use_manual_quality and manual_quality is not None:
                try:
                    quality = int(manual_quality)
                except Exception:
                    quality = 70
            else:
                quality = 70

            def encode_frame():
                _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                return base64.b64encode(buffer).decode('ascii')

            encoded_frame = await asyncio.to_thread(encode_frame)
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
            frame_seq += 1
            message = json.dumps(payload)
            try:
                buffered = getattr(channel, 'bufferedAmount', None)
                if buffered is not None and buffered > 65536:
                    while getattr(channel, 'bufferedAmount', 0) > 32768:
                        await asyncio.sleep(0.05)

                if getattr(channel, 'bufferedAmountLowThreshold', None) is not None:
                    channel.bufferedAmountLowThreshold = 32768

                channel.send(message)
                if frame_seq % 50 == 0:
                    info(f'sent frame {frame_seq} browser_sid={browser_sid} size={len(message)} buffered={getattr(channel, "bufferedAmount", "n/a")}')
                    dbg(f'cursor present={bool(cursor_b64)} hotspot=({hotspot_x},{hotspot_y}) format={cursor_fmt}')
                    dbg(f'cursor present={bool(cursor_b64)} hotspot=({hotspot_x},{hotspot_y}) format={cursor_fmt}')
            except Exception as exc:
                error(f'frame send failed browser_sid={browser_sid} exc={repr(exc)}')
                break
            delay = 0.12
            if use_manual_throttle and manual_throttle_interval_ms is not None:
                try:
                    delay = max(0.01, float(manual_throttle_interval_ms) / 1000.0)
                except Exception:
                    delay = 0.12
            await asyncio.sleep(delay)
        except Exception as exc:
            error(f'_send_webrtc_frames exception browser_sid={browser_sid} exc={exc}')
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
manual_throttle_interval_ms = None
use_manual_throttle = False

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
        global manual_quality, use_manual_quality
        try:
            mode = cmd_data.get('mode')
            if mode == 'manual':
                q = cmd_data.get('quality')
                if q is not None:
                    manual_quality = int(q)
                    use_manual_quality = True
            else:
                use_manual_quality = False
                manual_quality = None
        except Exception:
            pass
    elif cmd_type == "set_throttle":
        global manual_throttle_interval_ms, use_manual_throttle
        try:
            mode = cmd_data.get('mode')
            if mode == 'manual':
                interval = cmd_data.get('interval_ms')
                if interval is not None:
                    manual_throttle_interval_ms = int(interval)
                    use_manual_throttle = True
            else:
                use_manual_throttle = False
                manual_throttle_interval_ms = None
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
cursor_thread.start()
tcp_thread.start()
port_ready.wait()
udp_thread.start()
threading.Thread(target=relay_reconnect_loop, daemon=True).start()

def open_logs_folder():
    try:
        if os.name == 'nt':
            os.startfile(str(LOG_DIR))
        else:
            subprocess.Popen(['xdg-open', str(LOG_DIR)])
    except Exception as exc:
        error(f'failed to open logs folder: {exc}')


def _signal_handler(signum, frame):
    info(f'signal {signum} received; shutting down')
    shutdown_event.set()

signal.signal(signal.SIGINT, _signal_handler)
try:
    signal.signal(signal.SIGTERM, _signal_handler)
except Exception:
    pass

if os.name == 'nt':
    try:
        kernel32 = ctypes.windll.kernel32
        HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        def _console_ctrl_handler(ctrl_type):
            # 0=CTRL_C_EVENT, 1=CTRL_BREAK_EVENT, 2=CTRL_CLOSE_EVENT, 5=CTRL_LOGOFF_EVENT, 6=CTRL_SHUTDOWN_EVENT
            try:
                if ctrl_type in (0, 1, 2, 5, 6):
                    if not shutdown_event.is_set():
                        info('shutting down')
                    shutdown_event.set()
                    try:
                        relay_socket.disconnect()
                    except Exception:
                        pass
                    try:
                        if webrtc_loop and loop_running(webrtc_loop):
                            webrtc_loop.call_soon_threadsafe(webrtc_loop.stop)
                    except Exception:
                        pass
                    try:
                        if tray_icon is not None:
                            tray_icon.stop()
                    except Exception:
                        pass
                    return True
            except Exception:
                pass
            return False
        console_handler = HandlerRoutine(_console_ctrl_handler)
        kernel32.SetConsoleCtrlHandler(console_handler, True)
    except Exception:
        pass

try:
    if os.name == 'nt':
        try:
            import pystray
            from PIL import Image as PILImage, ImageDraw as PILImageDraw
        except Exception:
            info('tray not available; running headless')
            shutdown_event.wait()
        else:
            img = PILImage.new('RGBA', (64, 64), (0, 0, 0, 0))
            d = PILImageDraw.Draw(img)
            d.ellipse((10, 10, 54, 54), fill='#4f46e5')
            d.ellipse((20, 20, 44, 44), fill='#ffffff')

            def on_quit(icon, item):
                info('tray quit requested; shutting down')
                shutdown_event.set()
                try:
                    relay_socket.disconnect()
                except Exception:
                    pass
                try:
                    if webrtc_loop and loop_running(webrtc_loop):
                        webrtc_loop.call_soon_threadsafe(webrtc_loop.stop)
                except Exception:
                    pass
                try:
                    icon.stop()
                except Exception:
                    pass

            def on_open_logs(icon, item):
                dbg('tray open logs requested')
                open_logs_folder()

            icon = pystray.Icon(
                'remote_control',
                img,
                'Remote control server',
                menu=pystray.Menu(
                    pystray.MenuItem('Open Logs Folder', on_open_logs),
                    pystray.MenuItem('Quit', on_quit),
                ),
            )
            try:
                tray_icon = icon
            except Exception:
                tray_icon = None
            try:
                icon.run()
            except Exception as e:
                info(f'tray icon run failed: {e}')
                shutdown_event.wait()
    else:
        shutdown_event.wait()
except KeyboardInterrupt:
    info('KeyboardInterrupt received; shutting down')
    shutdown_event.set()
finally:
    try:
        relay_socket.disconnect()
    except Exception:
        pass
    try:
        if webrtc_loop and loop_running(webrtc_loop):
            webrtc_loop.call_soon_threadsafe(webrtc_loop.stop)
    except Exception:
        pass
