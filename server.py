
STRING_RELAY_URL = 'https://remote-control-ee7w.onrender.com'
CAPTURE_FPS = 30

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
import queue
import logging
import subprocess
from pathlib import Path
from ctypes import wintypes
import socketio
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
import aiortc.sdp as sdp_module
from aiortc import AudioStreamTrack, VideoStreamTrack
import av
import fractions
import traceback
import faulthandler
try:
    import pyaudiowpatch as pyaudio
except Exception:
    pyaudio = None
    print("pyaudiowpatch module not found. System audio capture will be disabled.")
try:
    import pyautogui
except Exception:
    print("pyautogui module not found.")
    sys.exit(1)
try:
    import win32gui
except Exception:
    print("win32gui module not found. Please install pywin32 package.")
    win32gui = None
try:
    from PIL import Image, ImageGrab, ImageDraw
except Exception:
    print("PIL module not found. You can install pillow package. Continuing without it.")
    Image = None
    ImageDraw = None
try:
    import pystray
except Exception:
    pystray = None
    print("pystray module not found. System tray icon will be disabled.")


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


def vdbg(msg):
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
        sys.exit(1)

    class _CV2Fallback:
        COLOR_RGB2BGR = 4
        INTER_LINEAR = 1
        INTER_AREA = 0
        IMWRITE_JPEG_QUALITY = 1

        @staticmethod
        def cvtColor(frame, code):
            if code == _CV2Fallback.COLOR_RGB2BGR:
                return frame[...,::-1]
            return frame

        @staticmethod
        def resize(frame, size, interpolation=None):
            img = Image.fromarray(frame[...,::-1])
            resample = Image.BILINEAR if interpolation == _CV2Fallback.INTER_LINEAR else Image.LANCZOS
            img = img.resize(size, resample)
            return np.array(img)[...,::-1]

        @staticmethod
        def imencode(ext, frame, params=None):
            img = Image.fromarray(frame[...,::-1])
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

QUALITY_PROFILES = {
    'low': {'bitrate': 1_500_000, 'width': 960, 'height': 540},
    'medium': {'bitrate': 4_000_000, 'width': 1280, 'height': 720},
    'high': {'bitrate': 8_000_000, 'width': 1920, 'height': 1080},
}

try:
    import mss
    _MSS_AVAILABLE = True
except Exception:
    _MSS_AVAILABLE = False

_mss_instance = None
_mss_monitor = None
_mss_lock = threading.Lock()

parser = argparse.ArgumentParser(description='Remote control server')
parser.add_argument('--password', default=None, help='Optional password required for client authentication')
parser.add_argument('--relay-url', default=None, help='Render Socket.IO relay URL')
parser.add_argument('--server-id', default=None, help='Unique ID for this server')
parser.add_argument('--debug', action='store_true', help='Enable verbose debug prints')
args = parser.parse_args()
relay_url = args.relay_url
if not relay_url:
    relay_url = os.environ.get('RELAY_URL', None)
if not relay_url:
    relay_url = STRING_RELAY_URL
if not relay_url:
    info('No relay URL provided. Please set the RELAY_URL environment variable or use --relay-url argument.')
    sys.exit(1)
VERBOSE = bool(getattr(args, 'debug', False))

if not args.server_id:
    host_name = socket.gethostname().replace(' ', '-').lower()
    args.server_id = f"{host_name}-{uuid.uuid4().hex[:8]}"

relay_socket = socketio.Client(
    logger=False,
    engineio_logger=False,
    reconnection=False,
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

faulthandler.enable()


def _thread_exception_logger(args):
    error(f'unhandled exception in thread {args.thread.name}: {args.exc_type.__name__}: {args.exc_value}')
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)


threading.excepthook = _thread_exception_logger


def create_tray_icon():
    global tray_icon
    try:
        if os.name == 'nt' and Image is not None and ImageDraw is not None and pystray is not None:
            img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((10, 10, 54, 54), fill='#4f46e5')
            d.ellipse((20, 20, 44, 44), fill='#ffffff')

            def on_quit(icon, item):
                info('tray quit requested; shutting down')
                shutdown_event.set()
                try:
                    icon.stop()
                except Exception:
                    pass

            def on_open_logs(icon, item):
                open_logs_folder()
                dbg('tray open logs requested')

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
        else:
            info('tray not available; running headless')
    except KeyboardInterrupt:
        info('KeyboardInterrupt received; shutting down')
        shutdown_event.set()


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


def main():
    threading.Thread(target=create_tray_icon, daemon=True).start()
    threading.Thread(target=capture_cursor_worker, daemon=True).start()
    threading.Thread(target=relay_heartbeat, daemon=True).start()
    last_connect_time = 0
    while True:
        try:
            if shutdown_event.is_set():
                break
            if not relay_connected and (time.time() - last_connect_time) > 5:
                connect_to_relay()
                last_connect_time = time.time()
            time.sleep(0.1)
        except Exception as e:
            error(f'main loop exception: {e}')
            break


def relay_heartbeat():
    while True:
        try:
            global relay_connected
            if shutdown_event.is_set():
                break
            if relay_connected:
                relay_socket.emit('server_heartbeat', {'server_id': args.server_id})
            else:
                break
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


class ScreenVideoTrack(VideoStreamTrack):

    def __init__(self, capture_func, session):
        super().__init__()
        self.capture_func = capture_func
        self.session = session
        self.fps = CAPTURE_FPS if CAPTURE_FPS and CAPTURE_FPS > 0 else 30
        self.interval = 1.0 / float(self.fps)
        self._stopped = False
        self._last_pts = 0
        self._sent_log = 0
        try:
            dbg(f'ScreenVideoTrack: initialized fps={self.fps} for session={getattr(session, "browser_sid", None) or session.get("browser_sid", "n/a")}')
        except Exception:
            pass

    async def recv(self):
        if self._stopped:
            raise Exception('track stopped')
        t0 = time.time()
        frame = await asyncio.to_thread(self.capture_func)
        if frame is None:
            await asyncio.sleep(0.05)
            return await self.recv()

        try:
            target_width = int(self.session.get('target_w') or FIXED_FRAME_WIDTH)
            target_height = int(self.session.get('target_h') or FIXED_FRAME_HEIGHT)
            if frame.shape[1] != target_width or frame.shape[0] != target_height:
                scale = min(target_width / frame.shape[1], target_height / frame.shape[0])
                resized_width = max(1, int(frame.shape[1] * scale))
                resized_height = max(1, int(frame.shape[0] * scale))
                frame = cv2.resize(frame, (resized_width, resized_height), interpolation=cv2.INTER_AREA)
                if resized_width != target_width or resized_height != target_height:
                    canvas = np.zeros((target_height, target_width, 3), dtype=frame.dtype)
                    offset_x = (target_width - resized_width) // 2
                    offset_y = (target_height - resized_height) // 2
                    canvas[offset_y:offset_y + resized_height, offset_x:offset_x + resized_width] = frame
                    frame = canvas

            current_fps = max(1, int(self.session.get('fps') or self.fps))
            self.interval = 1.0 / current_fps
            if self.session.get('encoder_bitrate') != self.session.get('bitrate'):
                _apply_video_encoder_bitrate(self.session)
            channel = self.session.get('channel')
            if channel is not None and getattr(channel, 'readyState', None) == 'open':
                with cursor_lock:
                    cursor_b64 = cursor_cache.get('b64')
                    hotspot_x = cursor_cache.get('hx', 0)
                    hotspot_y = cursor_cache.get('hy', 0)
                    cursor_fmt = cursor_cache.get('fmt', 'png')
                last_cursor = self.session.get('last_sent_cursor_b64')
                if cursor_b64 is not None and cursor_b64 != last_cursor:
                    try:
                        channel.send(json.dumps({
                            'type': 'cursor',
                            'cursorImage': cursor_b64,
                            'cursorHotspotX': hotspot_x,
                            'cursorHotspotY': hotspot_y,
                            'cursorFormat': cursor_fmt,
                        }))
                        self.session['last_sent_cursor_b64'] = cursor_b64
                    except Exception as exc:
                        dbg(f'cursor update failed: {exc}')
                elif cursor_b64 is None and last_cursor is not None:
                    try:
                        channel.send(json.dumps({'type': 'cursor', 'cursorRemoved': True}))
                        self.session['last_sent_cursor_b64'] = None
                    except Exception as exc:
                        dbg(f'cursor removal update failed: {exc}')
            try:
                video_frame = av.VideoFrame.from_ndarray(frame, format='bgr24')
            except Exception:
                try:
                    rgb = frame[...,::-1]
                    video_frame = av.VideoFrame.from_ndarray(rgb, format='rgb24')
                except Exception:
                    raise
            pts = int(time.time() * 1000)
            if pts <= self._last_pts:
                pts = self._last_pts + 1
            self._last_pts = pts
            video_frame.pts = pts
            video_frame.time_base = fractions.Fraction(1, 1000)
            try:
                if self._sent_log < 5:
                    dbg(f'ScreenVideoTrack: produced frame pts={video_frame.pts} size={getattr(frame, "shape", None)}')
                    self._sent_log += 1
            except Exception:
                pass

            elapsed = time.time() - t0
            to_wait = max(0, self.interval - elapsed)
            if to_wait > 0:
                await asyncio.sleep(to_wait)
            return video_frame
        except Exception:
            await asyncio.sleep(0.02)
            return await self.recv()

    def stop(self):
        self._stopped = True
        try:
            super().stop()
        except Exception:
            pass


class SystemAudioTrack(AudioStreamTrack):

    def __init__(self):
        super().__init__()
        if pyaudio is None:
            raise RuntimeError('pyaudiowpatch is not installed')
        self._audio = pyaudio.PyAudio()
        device = self._audio.get_default_wasapi_loopback()
        self.rate = int(device.get('defaultSampleRate') or 48000)
        self.channels = min(2, int(device.get('maxInputChannels') or 2))
        if self.channels < 1:
            raise RuntimeError('WASAPI loopback device has no input channels')
        self.samples = 960
        self._stopped = False
        self._audio_queue = queue.Queue(maxsize=3)
        self._stream = self._audio.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=device['index'],
            frames_per_buffer=self.samples,
        )
        self._stream.start_stream()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        self._pts = 0

    def _capture_loop(self):
        while not self._stopped:
            try:
                raw = self._stream.read(self.samples, exception_on_overflow=False)
                try:
                    self._audio_queue.put_nowait(raw)
                except queue.Full:
                    self._audio_queue.get_nowait()
                    self._audio_queue.put_nowait(raw)
            except Exception:
                if not self._stopped:
                    time.sleep(0.01)

    async def recv(self):
        if self._stopped:
            raise Exception('track stopped')
        raw = await asyncio.to_thread(self._audio_queue.get)
        if raw is None or self._stopped:
            raise Exception('track stopped')
        frame = av.AudioFrame(
            format='s16',
            layout='stereo' if self.channels == 2 else 'mono',
            samples=self.samples,
        )
        frame.planes[0].update(raw)
        frame.pts = self._pts
        frame.sample_rate = self.rate
        frame.time_base = fractions.Fraction(1, self.rate)
        self._pts += self.samples
        return frame

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        try:
            self._audio_queue.put_nowait(None)
        except queue.Full:
            try:
                self._audio_queue.get_nowait()
                self._audio_queue.put_nowait(None)
            except queue.Empty:
                pass
        try:
            self._stream.stop_stream()
        except Exception:
            pass
        capture_thread = getattr(self, '_capture_thread', None)
        if capture_thread is not None and capture_thread.is_alive():
            capture_thread.join(timeout=2.0)
        if capture_thread is not None and capture_thread.is_alive():
            error('audio capture thread did not stop; leaving PortAudio stream open')
            super().stop()
            return
        try:
            self._stream.close()
            self._audio.terminate()
        except Exception:
            pass
        super().stop()


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


async def _async_cleanup_webrtc_session(browser_sid, remove_session=True):
    with webrtc_sessions_lock:
        session = webrtc_sessions.get(browser_sid)
        if not session:
            return

    session['open'] = False
    pc = session.get('pc')
    if pc is not None:
        try:
            await asyncio.wait_for(pc.close(), timeout=2)
        except Exception:
            pass

    channel = session.get('channel')
    if channel is not None:
        try:
            if getattr(channel, 'readyState', None) != 'closed':
                channel.close()
        except Exception:
            pass
    try:
        video_track = session.get('video_track') if session else None
        if video_track is not None:
            try:
                video_track.stop()
            except Exception:
                pass
    except Exception:
        pass
    try:
        audio_track = session.get('audio_track') if session else None
        if audio_track is not None:
            audio_track.stop()
    except Exception:
        pass

    with webrtc_sessions_lock:
        session = webrtc_sessions.get(browser_sid)
        if session:
            session['frame_task'] = None
            session['channel'] = None
            session['pc'] = None
            if remove_session:
                webrtc_sessions.pop(browser_sid, None)


def _apply_video_encoder_bitrate(session):
    if not session:
        return False
    bitrate = session.get('bitrate')
    pc = session.get('pc')
    if not bitrate or pc is None:
        return False

    applied = False
    for sender in pc.getSenders():
        track = getattr(sender, 'track', None)
        if track is None or getattr(track, 'kind', None) != 'video':
            continue
        encoder = getattr(sender, '_RTCRtpSender__encoder', None)
        if encoder is None or not hasattr(encoder, 'target_bitrate'):
            continue
        try:
            target_bitrate = int(bitrate)
            codec = getattr(encoder, 'codec', None)
            target_width = int(session.get('target_w') or 0)
            target_height = int(session.get('target_h') or 0)
            if codec is not None and (
                codec.width != target_width or codec.height != target_height
            ):
                continue
            if encoder.target_bitrate != target_bitrate:
                encoder.target_bitrate = target_bitrate
            applied = True
        except Exception as exc:
            dbg(f'video bitrate update skipped: {exc}')

    if applied:
        session['encoder_bitrate'] = int(bitrate)
    return applied


async def _apply_video_bitrate(session, bitrate):
    if session is not None:
        session['bitrate'] = int(bitrate)
        _apply_video_encoder_bitrate(session)


async def _async_shutdown_webrtc_resources(session_ids):
    for sid in session_ids:
        try:
            await _async_cleanup_webrtc_session(sid)
        except Exception:
            pass
    try:
        asyncio.get_running_loop().stop()
    except Exception:
        pass


def _shutdown_webrtc_resources():
    global webrtc_loop, webrtc_loop_thread
    with webrtc_sessions_lock:
        session_ids = list(webrtc_sessions.keys())

    if webrtc_loop is not None and loop_running(webrtc_loop):
        try:
            future = asyncio.run_coroutine_threadsafe(
                _async_shutdown_webrtc_resources(session_ids),
                webrtc_loop,
            )
            try:
                future.result(timeout=5)
            except Exception:
                pass
        except Exception:
            pass
    else:
        for sid in session_ids:
            try:
                _cleanup_webrtc_session(sid)
            except Exception:
                pass

    if webrtc_loop is not None:
        try:
            if loop_running(webrtc_loop):
                webrtc_loop.call_soon_threadsafe(webrtc_loop.stop)
        except Exception:
            pass

        try:
            if webrtc_loop_thread is not None and webrtc_loop_thread.is_alive():
                webrtc_loop_thread.join(timeout=2)
        except Exception:
            pass

        try:
            if not webrtc_loop.is_closed():
                webrtc_loop.close()
        except Exception:
            pass

        webrtc_loop = None
        webrtc_loop_thread = None


def _cleanup_webrtc_session(browser_sid, remove_session=True):
    with webrtc_sessions_lock:
        session = webrtc_sessions.pop(browser_sid, None) if remove_session else webrtc_sessions.get(browser_sid)
        if not session:
            return

        session['open'] = False
        task = session.get('frame_task')
        loop = session.get('loop')
        pc = session.get('pc')
        channel = session.get('channel')
        video_track = session.get('video_track')
        audio_track = session.get('audio_track')
        session['frame_task'] = None
        session['channel'] = None
        session['pc'] = None

    if task is not None:
        try:
            if loop is not None and loop_running(loop) and hasattr(loop, 'call_soon_threadsafe'):
                loop.call_soon_threadsafe(task.cancel)
            elif hasattr(task, 'cancel'):
                task.cancel()
        except Exception:
            pass

    if pc is not None and loop_running(loop):
        try:
            asyncio.run_coroutine_threadsafe(pc.close(), loop)
        except Exception as exc:
            error(f'error scheduling pc.close() for browser_sid={browser_sid}: {exc}')
    elif channel is not None:
        try:
            if getattr(channel, 'readyState', None) != 'closed':
                channel.close()
        except Exception:
            pass
    for track in (video_track, audio_track):
        if track is not None:
            try:
                track.stop()
            except Exception:
                pass


@relay_socket.on('end_session')
def on_end_session(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    if browser_sid:
        _cleanup_webrtc_session(browser_sid)


@relay_socket.on('session_denied')
def on_session_denied(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    info(f'session_denied browser_sid={browser_sid} data={data}')
    if browser_sid:
        _cleanup_webrtc_session(browser_sid)


@relay_socket.on('connect')
def on_relay_connect():
    global relay_connected
    if shutdown_event.is_set():
        return
    relay_connected = True
    info('relay socket connected')
    dbg('relay connected via Socket.IO; media and control use WebRTC')
    try:
        relay_socket.emit('register_server', {
            'server_id': args.server_id,
            'name': socket.gethostname(),
            'hostname': socket.gethostname(),
            'address': args.server_id,
        })
    except Exception as exc:
        error(f're-register server failed after reconnect: {exc}')


@relay_socket.on('disconnect')
def on_relay_disconnect():
    global relay_connected
    relay_connected = False
    if shutdown_event.is_set():
        return
    info('relay socket disconnected')
    with webrtc_sessions_lock:
        session_ids = list(webrtc_sessions.keys())
    for sid in session_ids:
        try:
            _cleanup_webrtc_session(sid)
        except Exception:
            pass


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


def _parse_turn_servers_from_env():
    urls = os.environ.get('TURN_URLS')
    if not urls:
        return []
    url_list = [u.strip() for u in urls.split(',') if u.strip()]
    user = os.environ.get('TURN_USER')
    passwd = os.environ.get('TURN_PASS')
    servers = []
    for u in url_list:
        try:
            if user and passwd:
                servers.append(RTCIceServer(urls=[u], username=user, credential=passwd))
            else:
                servers.append(RTCIceServer(urls=[u]))
        except Exception:
            continue
    return servers


@relay_socket.on('webrtc_offer')
def on_webrtc_offer(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    offer = data.get('offer')
    dbg(f'received webrtc_offer browser_sid={browser_sid} offer_present={bool(offer)}')
    dbg(f'offer payload keys={list(offer.keys()) if isinstance(offer, dict) else type(offer)}')
    if not offer:
        info('[server] invalid offer payload')
        return
    if not browser_sid:
        info('[server] missing browser_sid')
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
                'quality_profile': 'medium',
                'bitrate': QUALITY_PROFILES['medium']['bitrate'],
                'fps': 30,
                'target_w': FIXED_FRAME_WIDTH,
                'target_h': FIXED_FRAME_HEIGHT,
            }
            return

        if session.get('pc') is None or session.get('loop') is None or not session.get('remote_description_set', False):
            session.setdefault('candidate_queue', []).append(candidate)
            dbg(f'queued candidate until PC exists or remote description set ' + str(browser_sid))
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
        ice_servers = [RTCIceServer(urls=['stun:stun.l.google.com:19302'])]
        try:
            extra = _parse_turn_servers_from_env()
            if extra:
                ice_servers.extend(extra)
        except Exception:
            pass
        pc = RTCPeerConnection(configuration=RTCConfiguration(ice_servers))

        @pc.on('iceconnectionstatechange')
        def _on_iceconnectionstatechange():
            try:
                dbg(f'iceConnectionState change for browser_sid={browser_sid}: {pc.iceConnectionState}')
            except Exception:
                dbg('iceConnectionState change (failed to read state)')

        @pc.on('connectionstatechange')
        def _on_connectionstatechange():
            try:
                dbg(f'connectionState change for browser_sid={browser_sid}: {pc.connectionState}')
            except Exception:
                dbg('connectionState change (failed to read state)')

        @pc.on('signalingstatechange')
        def _on_signalingstatechange():
            try:
                dbg(f'signalingState change for browser_sid={browser_sid}: {pc.signalingState}')
            except Exception:
                dbg('signalingState change (failed to read state)')

        @pc.on('track')
        def _on_track(track):
            try:
                dbg(f'pc.ontrack for browser_sid={browser_sid}: kind={getattr(track, "kind", None)} id={getattr(track, "id", None)}')
            except Exception:
                dbg('pc.ontrack event (failed to log)')

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
            session['loop'] = asyncio.get_running_loop()
            session['channel'] = None
            session['open'] = False
            session.setdefault('candidate_queue', [])
            session['remote_description_set'] = False

        @pc.on("datachannel")
        def on_datachannel(channel):
            info(f'datachannel created for browser_sid={browser_sid} id={channel.label}')
            dbg(f'datachannel protocol={channel.protocol} negotiated={channel.negotiated} readyState={channel.readyState}')
            session['channel'] = channel

            @channel.on("open")
            def on_open():
                info(f'datachannel open event for browser_sid={browser_sid}')
                session['open'] = True
                info(f'datachannel ready for control messages browser_sid={browser_sid}')

            @channel.on("message")
            def on_message(message):
                try:
                    if isinstance(message, bytes):
                        payload = message.decode('utf-8', errors='ignore')
                    else:
                        payload = message
                    data = json.loads(payload)
                    if isinstance(data, dict):
                        execute_command(data, browser_sid=browser_sid)
                    else:
                        info(f'datachannel received non-object payload for browser_sid={browser_sid}: {payload}')
                except Exception as exc:
                    error(f'datachannel message failed for browser_sid={browser_sid}: {repr(exc)}')

            @channel.on("close")
            def on_close():
                info(f'datachannel closed for browser_sid={browser_sid}')
                threading.Thread(
                    target=lambda: _cleanup_webrtc_session(browser_sid),
                    name=f'cleanup-{browser_sid[:8]}',
                    daemon=True,
                ).start()

            if channel.readyState == 'open':
                session['open'] = True

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

        try:
            offer_sdp = offer.get('sdp', '')[:2000].replace('\n', '\\n') if isinstance(offer, dict) else str(type(offer))
            vdbg(f'offer sdp snippet for browser_sid={browser_sid}: {offer_sdp}')
        except Exception:
            pass

        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer['sdp'], type=offer['type']))
        try:
            trs = pc.getTransceivers()
            dbg(f'transceivers for browser_sid={browser_sid}: count={len(trs)}')
            for i, t in enumerate(trs):
                try:
                    od = getattr(t, '_offerDirection', None)
                    dbg(f' transceiver[{i}] kind={t.kind} direction={getattr(t, "direction", None)} recv_direction={od}')
                    if od not in getattr(sdp_module, 'DIRECTIONS', []):
                        dbg(f'  sanitizing transceiver[{i}] _offerDirection from {od} to sendrecv')
                        try:
                            setattr(t, '_offerDirection', 'sendrecv')
                        except Exception:
                            dbg(f'  failed to set _offerDirection for transceiver[{i}]')
                except Exception:
                    dbg(f' transceiver[{i}] debug failed')
        except Exception:
            pass
        with webrtc_sessions_lock:
            session = webrtc_sessions.get(browser_sid)
            if session:
                session['remote_description_set'] = True
        try:
            video_track = ScreenVideoTrack(capture_screen_dxgi, session)
            try:
                transceiver = next(
                    (item for item in pc.getTransceivers() if item.kind == 'video'),
                    None,
                )
                if transceiver is None:
                    transceiver = pc.addTransceiver('video', direction='sendonly')
                else:
                    transceiver.direction = 'sendonly'
                sender = transceiver.sender
                res = sender.replaceTrack(video_track)
                if asyncio.iscoroutine(res):
                    await res
                dbg(f'configured RTP video transceiver and replaced track for browser_sid={browser_sid}')
            except Exception as exc2:
                dbg(f'addTransceiver failed, falling back to addTrack: {exc2}')
                pc.addTrack(video_track)

            with webrtc_sessions_lock:
                session = webrtc_sessions.get(browser_sid)
                if session is not None:
                    session['video_track'] = video_track
            try:
                audio_track = SystemAudioTrack()
                transceiver = next(
                    (item for item in pc.getTransceivers() if item.kind == 'audio'),
                    None,
                )
                if transceiver is None:
                    transceiver = pc.addTransceiver('audio', direction='sendonly')
                else:
                    transceiver.direction = 'sendonly'
                res = transceiver.sender.replaceTrack(audio_track)
                if asyncio.iscoroutine(res):
                    await res
                with webrtc_sessions_lock:
                    session['audio_track'] = audio_track
                dbg(f'configured WASAPI loopback audio for browser_sid={browser_sid}')
            except Exception as exc:
                dbg(f'Audio capture unavailable for browser_sid={browser_sid}: {exc}')

            try:
                senders = pc.getSenders()
                dbg(f'senders count={len(senders)} for browser_sid={browser_sid}')
                for si, s in enumerate(senders):
                    try:
                        dbg(f' sender[{si}] track={getattr(s, "track", None)} kind={getattr(s, "track", None) and getattr(s.track, "kind", None)}')
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception as exc:
            error(f'failed to add video track for browser_sid={browser_sid}: {exc}\n{traceback.format_exc()}')
        dbg(f'remote description set for browser_sid={browser_sid}')
        dbg(f'peer connection state after remote description: {pc.connectionState} iceConnectionState={pc.iceConnectionState}')
        answer = await pc.createAnswer()
        try:
            trs = pc.getTransceivers()
            for i, t in enumerate(trs):
                try:
                    a = getattr(t, 'direction', None)
                    b = getattr(t, '_offerDirection', None)
                    valid = getattr(sdp_module, 'DIRECTIONS', ['sendrecv', 'sendonly', 'recvonly', 'inactive'])
                    changed = False
                    if not isinstance(a, str) or a not in valid:
                        dbg(f'sanitize transceiver[{i}].direction: {a} -> sendrecv')
                        try:
                            setattr(t, 'direction', 'sendrecv')
                            changed = True
                        except Exception:
                            pass
                    if not isinstance(b, str) or b not in valid:
                        dbg(f'sanitize transceiver[{i}]._offerDirection: {b} -> sendrecv')
                        try:
                            setattr(t, '_offerDirection', 'sendrecv')
                            changed = True
                        except Exception:
                            pass
                    if changed:
                        dbg(f'transceiver[{i}] sanitized')
                except Exception:
                    dbg(f'failed to sanitize transceiver[{i}]')
        except Exception:
            dbg('transceiver sanitization failed')

        await pc.setLocalDescription(answer)
        dbg(f'local description set for browser_sid={browser_sid}')
        dbg(f'created answer for browser_sid={browser_sid} sdp_length={len(pc.localDescription.sdp) if pc.localDescription else 0}')
        try:
            answer_sdp = pc.localDescription.sdp[:2000].replace('\n', '\\n') if pc.localDescription else ''
            vdbg(f'answer sdp snippet for browser_sid={browser_sid}: {answer_sdp}')
        except Exception:
            pass
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
                'remote_description_set': False,
                'quality_profile': 'medium',
                'bitrate': QUALITY_PROFILES['medium']['bitrate'],
                'fps': 30,
                'target_w': FIXED_FRAME_WIDTH,
                'target_h': FIXED_FRAME_HEIGHT,
            }
            webrtc_sessions[browser_sid] = session
        else:
            session['loop'] = webrtc_loop

    future = asyncio.run_coroutine_threadsafe(_async_handle_offer(), webrtc_loop)

    def _offer_done(f):
        try:
            f.result()
        except Exception as exc:
            error(f'[webrtc] offer task failed: {exc}\n{traceback.format_exc()}')

    future.add_done_callback(_offer_done)


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


def capture_screen_dxgi():
    global _mss_instance, _mss_monitor
    if _MSS_AVAILABLE:
        try:
            with _mss_lock:
                if _mss_instance is None:
                    _mss_instance = mss.MSS()
                    _mss_monitor = _mss_instance.monitors[1] if len(_mss_instance.monitors) > 1 else _mss_instance.monitors[0]
                img = _mss_instance.grab(_mss_monitor)
                frame = np.asarray(img)
                return frame[...,:3] if frame.shape[2] == 4 else frame
        except Exception:
            pass

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


def capture_cursor_worker():
    global cursor_cache, cursor_lock
    last_b64 = None
    if os.name == 'nt':
        while True:
            try:
                time.sleep(0.033)
            except Exception:
                break
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


def execute_command(cmd_data, browser_sid=None, ws_settings=None):
    cmd_type = cmd_data.get("type")
    if cmd_type == 'set_capture_params':
        try:
            with webrtc_sessions_lock:
                session = webrtc_sessions.get(browser_sid)
                if session is None and ws_settings is not None:
                    session = ws_settings
            if session is not None:
                fps = cmd_data.get('fps')
                if fps:
                    try:
                        session['fps'] = int(fps)
                    except Exception:
                        pass
                mq = cmd_data.get('min_quality')
                if mq is not None:
                    try:
                        session['min_quality'] = int(mq)
                    except Exception:
                        pass
                Mq = cmd_data.get('max_quality')
                if Mq is not None:
                    try:
                        session['max_quality'] = int(Mq)
                    except Exception:
                        pass
                ki = cmd_data.get('keyframe_interval')
                if ki is not None:
                    try:
                        session['keyframe_interval'] = int(ki)
                    except Exception:
                        pass
                at = cmd_data.get('adaptive_threshold')
                if at is not None:
                    try:
                        session['adaptive_threshold'] = float(at)
                    except Exception:
                        pass
                br = cmd_data.get('bitrate')
                if br is not None:
                    try:
                        session['bitrate'] = int(br)
                    except Exception:
                        pass
                tw = cmd_data.get('target_width')
                th = cmd_data.get('target_height')
                if tw is not None and th is not None:
                    try:
                        session['target_w'] = int(tw)
                        session['target_h'] = int(th)
                    except Exception:
                        pass
        except Exception:
            pass
        return
    if cmd_type == 'request_keyframe':
        try:
            with webrtc_sessions_lock:
                session = webrtc_sessions.get(browser_sid)
                if session is not None:
                    session['hq_until'] = int(frame_seq) + 2
        except Exception:
            pass
        return
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
    elif cmd_type in ('set_quality_profile', 'set_stream_setting'):
        settings = None
        if browser_sid:
            with webrtc_sessions_lock:
                settings = webrtc_sessions.get(browser_sid)
        if settings is None:
            settings = ws_settings
        if settings is None:
            return

        if cmd_type == 'set_quality_profile':
            profile_name = cmd_data.get('profile', 'medium')
            profile = QUALITY_PROFILES.get(profile_name, QUALITY_PROFILES['medium'])
            settings['quality_profile'] = profile_name if profile_name in QUALITY_PROFILES else 'medium'
            settings['bitrate'] = profile['bitrate']
            settings['target_w'] = profile['width']
            settings['target_h'] = profile['height']
            settings['encoder_bitrate'] = None
            if browser_sid and settings.get('loop') and loop_running(settings['loop']):
                asyncio.run_coroutine_threadsafe(
                    _apply_video_bitrate(settings, profile['bitrate']),
                    settings['loop'],
                )
        else:
            setting = cmd_data.get('setting')
            if setting == 'fps':
                settings['fps'] = max(1, min(60, int(cmd_data.get('value', 30))))


def open_logs_folder():
    try:
        if os.name == 'nt':
            os.startfile(str(LOG_DIR))
        else:
            subprocess.Popen(['xdg-open', str(LOG_DIR)])
    except Exception as exc:
        error(f'failed to open logs folder: {exc}')


if os.name == 'nt':
    try:
        kernel32 = ctypes.windll.kernel32
        HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _console_ctrl_handler(ctrl_type):
            try:
                if ctrl_type in (0, 1, 2, 5, 6):
                    dbg(f'Windows console control event received: {ctrl_type}')
                    if not shutdown_event.is_set():
                        info('shutting down')
                    shutdown_event.set()
                    try:
                        relay_socket.disconnect()
                    except Exception:
                        pass
                    try:
                        _shutdown_webrtc_resources()
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


main()
