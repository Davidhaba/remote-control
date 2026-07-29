import os
import socket
import threading
import time
import json
import sys
import base64
import numpy as np
import pyautogui
from flask_socketio import SocketIO

SERVER_URL = os.environ.get('RELAY_URL', 'https://your-render-app.onrender.com')
SERVER_ID = os.environ.get('SERVER_ID', 'default-server')
SERVER_PASSWORD = os.environ.get('SERVER_PASSWORD', '')

socketio = SocketIO(message_queue_size=100000)


def connect_to_relay():
    try:
        socketio.connect(SERVER_URL, transports=['websocket'], namespaces=['/'])
        socketio.emit('register_server', {
            'server_id': SERVER_ID,
            'name': socket.gethostname(),
            'hostname': socket.gethostname(),
            'address': SERVER_ID,
            'password_protected': bool(SERVER_PASSWORD),
        })
        threading.Thread(target=heartbeat_loop, daemon=True).start()
    except Exception as e:
        print('relay connect error', e)


def heartbeat_loop():
    while True:
        try:
            socketio.emit('server_heartbeat', {'server_id': SERVER_ID})
        except Exception:
            pass
        time.sleep(5)


@socketio.on('request_session')
def handle_request_session(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    if browser_sid:
        socketio.emit('session_ready', {'browser_sid': browser_sid, 'server_id': SERVER_ID}, to=browser_sid)


@socketio.on('relay_command')
def handle_relay_command(data):
    data = data or {}
    cmd = data.get('cmd')
    if cmd:
        print('relay command', cmd)


if __name__ == '__main__':
    connect_to_relay()
    while True:
        time.sleep(1)
