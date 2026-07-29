import os
import socket
import threading
import time
import subprocess
import shutil
import json
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

registered_servers = {}
browser_sessions = {}


def _server_snapshot(server_id, server_info):
    return {
        'server_id': server_id,
        'name': server_info.get('name', server_id),
        'status': server_info.get('status', 'online'),
        'hostname': server_info.get('hostname', server_id),
        'last_seen': server_info.get('last_seen', 0),
        'address': server_info.get('address', server_id),
    }


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/discover-servers', methods=['GET'])
def discover_servers():
    try:
        now = time.time()
        servers = []
        for server_id, server_info in registered_servers.items():
            if now - server_info.get('last_seen', now) > 20:
                continue
            servers.append(_server_snapshot(server_id, server_info))
        return jsonify({'servers': servers})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/connect', methods=['POST'])
def connect():
    data = request.json or {}
    server_id = data.get('server_id') or data.get('address')
    socket_id = data.get('socket_id')
    password = data.get('password')
    if not server_id or not socket_id:
        return jsonify({'error': 'Missing server selection'}), 400

    if not password or not str(password).strip():
        return jsonify({'error': 'Password is required'}), 400

    browser_sessions[socket_id] = {
        'server_id': server_id,
        'socket_id': socket_id,
        'password': password,
        'type': data.get('type', 'desktop'),
    }

    server = registered_servers.get(server_id)
    if not server or not server.get('sid'):
        return jsonify({'error': 'Selected server is not available'}), 404

    socketio.emit('request_session', {
        'browser_sid': socket_id,
        'server_id': server_id,
        'password': password,
        'type': data.get('type', 'desktop'),
    }, room=server['sid'])
    return jsonify({'status': 'connected'})


@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    data = request.get_json(silent=True) or {}
    socket_id = data.get('socket_id')
    if socket_id in browser_sessions:
        session = browser_sessions.pop(socket_id)
        server_id = session.get('server_id')
        server = registered_servers.get(server_id)
        if server and server.get('sid'):
            socketio.emit('end_session', {'browser_sid': socket_id, 'server_id': server_id}, room=server['sid'])
    return jsonify({'status': 'disconnected'})


@app.route('/api/discover-android', methods=['GET'])
def discover_android():
    try:
        adb = shutil.which('adb')
        if not adb:
            return jsonify({'devices': []})
        result = subprocess.run([adb, 'devices', '-l'], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return jsonify({'devices': []})
        devices = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if not parts or parts[0].startswith('List'):
                continue
            device_id = parts[0]
            if device_id.endswith('device'):
                device_id = device_id[:-len('device')]
            if device_id:
                props = {}
                for part in parts[1:]:
                    if ':' in part:
                        k, v = part.split(':', 1)
                        props[k] = v
                devices.append({
                    'id': device_id,
                    'manufacturer': props.get('manufacturer', 'Unknown'),
                    'model': props.get('model', 'Unknown'),
                    'android_version': props.get('ro.build.version.release', 'Unknown'),
                })
        return jsonify({'devices': devices})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@socketio.on('connect')
def handle_socket_connect():
    return None


@socketio.on('register_server')
def handle_register_server(data):
    data = data or {}
    server_id = data.get('server_id') or f"server-{request.sid[:8]}"
    registered_servers[server_id] = {
        'sid': request.sid,
        'name': data.get('name', server_id),
        'hostname': data.get('hostname', socket.gethostname()),
        'address': data.get('address', server_id),
        'status': 'online',
        'last_seen': time.time(),
        'password_protected': bool(data.get('password_protected')),
    }


@socketio.on('server_heartbeat')
def handle_server_heartbeat(data):
    server_id = (data or {}).get('server_id')
    if server_id in registered_servers:
        registered_servers[server_id]['last_seen'] = time.time()


@socketio.on('server_frame')
def handle_server_frame(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    if not browser_sid:
        return
    payload = {
        'frame': data.get('frame'),
        'cursorImage': data.get('cursorImage'),
        'cursorHotspotX': data.get('cursorHotspotX', 0),
        'cursorHotspotY': data.get('cursorHotspotY', 0),
        'cursorFormat': data.get('cursorFormat', 'raw'),
    }
    socketio.emit('frame', payload, room=browser_sid)


@socketio.on('session_ready')
def handle_session_ready_from_server(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    if not browser_sid:
        return
    try:
        if browser_sid in browser_sessions:
            browser_sessions[browser_sid]['authorized'] = True
    except Exception:
        pass
    socketio.emit('session_ready', {'server_id': data.get('server_id')}, room=browser_sid)


@socketio.on('session_denied')
def handle_session_denied_from_server(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    if not browser_sid:
        return
    try:
        if browser_sid in browser_sessions:
            browser_sessions[browser_sid].pop('authorized', None)
    except Exception:
        pass
    socketio.emit('session_denied', {'server_id': data.get('server_id'), 'reason': data.get('reason')}, room=browser_sid)


@socketio.on('command')
def handle_command(data):
    data = data or {}
    cmd_data = data.get('cmd')
    browser_sid = request.sid
    if not cmd_data:
        return

    session = browser_sessions.get(browser_sid)
    if not session:
        return

    server_id = session.get('server_id')
    server = registered_servers.get(server_id)
    if not server or not server.get('sid'):
        return

    if isinstance(cmd_data, dict) and cmd_data.get('type') == 'disconnect_request':
        socketio.emit('end_session', {'browser_sid': browser_sid, 'server_id': server_id}, room=server['sid'])
        browser_sessions.pop(browser_sid, None)
        return

    if not session.get('authorized'):
        try:
            socketio.emit('session_denied', {'server_id': server_id, 'reason': 'not_authorized'}, room=browser_sid)
        except Exception:
            pass
        return

    socketio.emit('relay_command', {'browser_sid': browser_sid, 'server_id': server_id, 'cmd': cmd_data}, room=server['sid'])


@socketio.on('disconnect')
def handle_socket_disconnect():
    browser_sid = request.sid
    if browser_sid in browser_sessions:
        session = browser_sessions.pop(browser_sid)
        server_id = session.get('server_id')
        server = registered_servers.get(server_id)
        if server and server.get('sid'):
            socketio.emit('end_session', {'browser_sid': browser_sid, 'server_id': server_id}, room=server['sid'])

    for server_id, server_info in list(registered_servers.items()):
        if server_info.get('sid') == browser_sid:
            del registered_servers[server_id]
            break


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
