import socket
import threading
import json
import base64
import time
import queue
import subprocess
import os
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

remote_server_socket = None
remote_server_cmd_socket = None
remote_server_host = None
remote_server_port = None
is_connected = False
frame_queue = queue.Queue(maxsize=2)
last_frame_time = 0
current_latency = 50
latency_sample_time = 0

def connect_to_remote_server(host, port):
    global remote_server_socket, remote_server_cmd_socket, remote_server_host, remote_server_port, is_connected
    try:
        remote_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        remote_server_socket.connect((host, port))
        remote_server_host = host
        remote_server_port = port
        is_connected = True
        cmd_port_msg = remote_server_socket.recv(1024).decode()
        if cmd_port_msg.startswith("CMD_PORT:"):
            cmd_port = int(cmd_port_msg.split(":")[1])
            remote_server_cmd_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            remote_server_cmd_socket.connect((host, cmd_port))
        threading.Thread(target=receive_frames, daemon=True).start()
        threading.Thread(target=broadcast_frames, daemon=True).start()
        return True
    except Exception:
        is_connected = False
        return False

def receive_frames():
    global remote_server_socket, is_connected
    global last_frame_time, current_latency, latency_sample_time
    try:
        while is_connected and remote_server_socket:
            try:
                size_data = remote_server_socket.recv(4)
                if not size_data:
                    is_connected = False
                    break
                size = int.from_bytes(size_data, 'big')
                buffer = bytearray()
                while len(buffer) < size:
                    packet = remote_server_socket.recv(min(size - len(buffer), 65536))
                    if not packet:
                        break
                    buffer.extend(packet)
                frame_b64 = base64.b64encode(buffer).decode('utf-8')
                meta = {}
                try:
                    meta_size_data = remote_server_socket.recv(4)
                    if meta_size_data:
                        meta_size = int.from_bytes(meta_size_data, 'big')
                        meta_buf = bytearray()
                        while len(meta_buf) < meta_size:
                            packet = remote_server_socket.recv(min(meta_size - len(meta_buf), 65536))
                            if not packet:
                                break
                            meta_buf.extend(packet)
                        try:
                            meta = json.loads(meta_buf.decode('utf-8'))
                        except Exception:
                            meta = {}
                except Exception:
                    meta = {}
                payload = {'frame': frame_b64}
                if meta:
                    payload.update(meta)
                try:
                    now = time.time()
                    if now - latency_sample_time >= 1.0:
                        if last_frame_time > 0:
                            frame_interval = (now - last_frame_time) * 1000
                            current_latency = int(round(current_latency * 0.6 + frame_interval * 0.4))
                            current_latency = max(10, min(10000, current_latency))
                        latency_sample_time = now
                    last_frame_time = now
                except Exception:
                    pass
                try:
                    frame_queue.put_nowait(payload)
                except queue.Full:
                    try:
                        frame_queue.get_nowait()
                        frame_queue.put_nowait(payload)
                    except Exception:
                        pass
            except Exception:
                break
    except Exception:
        is_connected = False

def broadcast_frames():
    while True:
        try:
            payload = frame_queue.get()
            frame_data = {'frame': payload['frame']}
            if 'cursorImage' in payload:
                    if 'cursorImage' in payload:
                        frame_data['cursorImage'] = payload['cursorImage']
                        frame_data['cursorHotspotX'] = payload.get('cursorHotspotX', 0)
                        frame_data['cursorHotspotY'] = payload.get('cursorHotspotY', 0)
            socketio.emit('frame', frame_data, to=None, skip_sid=None)
        except Exception:
            pass

def send_command_to_server(cmd_data):
    global remote_server_cmd_socket, is_connected, current_latency
    if not is_connected or not remote_server_cmd_socket:
        return False
    try:
        cmd_json = json.dumps(cmd_data)
        message = f"CMD:{cmd_json}\nLATENCY:{int(current_latency)}\n"
        remote_server_cmd_socket.sendall(message.encode())
        return True
    except Exception:
        is_connected = False
        return False


def disconnect_remote_server():
    global remote_server_cmd_socket, remote_server_socket, is_connected, remote_server_host, remote_server_port
    try:
        if remote_server_cmd_socket:
            try:
                remote_server_cmd_socket.sendall(b"DISCONNECT\n")
            except Exception:
                pass
            try:
                remote_server_cmd_socket.close()
            except Exception:
                pass
            remote_server_cmd_socket = None

        if remote_server_socket:
            try:
                remote_server_socket.close()
            except Exception:
                pass
            remote_server_socket = None

        is_connected = False
        remote_server_host = None
        remote_server_port = None

        try:
            while not frame_queue.empty():
                frame_queue.get_nowait()
        except Exception:
            pass

        return True
    except Exception:
        is_connected = False
        return False

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/discover-servers', methods=['GET'])
def discover_servers():
    try:
        import socket as socket_module
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        client.settimeout(2)
        message = "DISCOVER_SERVER".encode()
        client.sendto(message, ('<broadcast>', 45))
        servers = []
        try:
            while True:
                data, server_address = client.recvfrom(1024)
                server_info = data.decode().strip()
                host_part = server_info.split(':', 1)[0] if ':' in server_info else server_info
                try:
                    hostname = socket_module.gethostbyaddr(host_part)[0]
                except Exception:
                    hostname = host_part
                servers.append({"address": server_info, "ip": host_part, "hostname": hostname})
        except socket.timeout:
            pass
        finally:
            client.close()
        return jsonify({"servers": servers})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _get_android_devices():
    try:
        adb = shutil.which('adb')
        if not adb:
            return []
        result = subprocess.run([adb, 'devices', '-l'], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return []
        devices = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if not parts or parts[0].startswith('List'):
                continue
            device_id = parts[0]
            if device_id.endswith('device'):
                device_id = device_id[:-len('device')]
            if not device_id:
                continue
            props = {}
            for part in parts[1:]:
                if ':' in part:
                    k, v = part.split(':', 1)
                    props[k] = v
            manufacturer = props.get('manufacturer', 'Unknown')
            model = props.get('model', 'Unknown')
            android_version = props.get('ro.build.version.release', 'Unknown')
            devices.append({
                'id': device_id,
                'manufacturer': manufacturer,
                'model': model,
                'android_version': android_version,
            })
        return devices
    except Exception:
        return []

@app.route('/api/discover-android', methods=['GET'])
def discover_android():
    try:
        devices = _get_android_devices()
        return jsonify({'devices': devices})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/connect', methods=['POST'])
def connect():
    data = request.json
    host_port = data.get('address')
    if not host_port:
        return jsonify({"error": "No address provided"}), 400
    try:
        host, port = host_port.rsplit(':', 1)
        port = int(port)
        if connect_to_remote_server(host, port):
            return jsonify({"status": "connected"})
        else:
            return jsonify({"error": "Failed to connect"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@socketio.on('command')
def handle_command(data):
    cmd_data = data.get('cmd')
    if cmd_data:
        try:
            if isinstance(cmd_data, dict) and cmd_data.get('type') == 'disconnect_request':
                disconnect_remote_server()
                return
        except Exception:
            pass
        send_command_to_server(cmd_data)


@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    try:
        success = disconnect_remote_server()
        if success:
            return jsonify({"status": "disconnected"})
        else:
            return jsonify({"error": "failed to disconnect"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

threading.Thread(target=broadcast_frames, daemon=True).start()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
