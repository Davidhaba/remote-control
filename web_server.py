import os
import signal
import socket
import threading
import time

ASYNC_MODE = 'threading'
GEVENT_WEBSOCKET_AVAILABLE = False
try:
    from gevent import monkey
    monkey.patch_all()
    ASYNC_MODE = 'gevent'
    from geventwebsocket.handler import WebSocketHandler
    from gevent.pywsgi import WSGIServer
    GEVENT_WEBSOCKET_AVAILABLE = True
except Exception:
    WebSocketHandler = None
    WSGIServer = None

from flask import Flask, render_template, request, jsonify, redirect
from flask_socketio import SocketIO

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=ASYNC_MODE, logger=False, engineio_logger=False)

registered_servers = {}
browser_sessions = {}
state_lock = threading.RLock()
active_socket_ids = set()
SERVER_CLEANUP_INTERVAL = 30
SERVER_DISCONNECT_GRACE = 60
SERVER_RESPONSE_TIMEOUT = 15


def _disconnect_server(host_id, server_info):
    server_sid = server_info.get('sid') if server_info else None
    if not server_sid:
        return

    with state_lock:
        current_server = registered_servers.get(host_id)
        if not current_server or current_server.get('sid') != server_sid:
            return
        session_ids = [
            socket_id for socket_id, session in browser_sessions.items()
            if session.get('host_id') == host_id
        ]
        for socket_id in session_ids:
            browser_sessions.pop(socket_id, None)

    for socket_id in session_ids:
        socketio.emit('session_ended', {'host_id': host_id}, room=socket_id)

    try:
        socketio.server.disconnect(server_sid, namespace='/')
    except Exception:
        pass
    with state_lock:
        active_socket_ids.discard(server_sid)
        registered_servers.pop(host_id, None)


def _signal_host_sessions(host_id, result):
    with state_lock:
        sessions = [
            session for session in browser_sessions.values()
            if session.get('host_id') == host_id
        ]
        for session in sessions:
            session['connect_result'] = result
            event = session.get('connect_event')
            if event:
                event.set()


def _cleanup_sessions():
    while True:
        try:
            now = time.time()
            with state_lock:
                expired_servers = []
                for host_id, server_info in list(registered_servers.items()):
                    server_sid = server_info.get('sid')
                    if server_sid in active_socket_ids:
                        server_info['disconnected_at'] = None
                        continue

                    disconnected_at = server_info.get('disconnected_at')
                    if disconnected_at is None:
                        server_info['disconnected_at'] = now
                        continue
                    if now - disconnected_at > SERVER_DISCONNECT_GRACE:
                        expired_servers.append((host_id, dict(server_info)))
            for host_id, server_info in expired_servers:
                _disconnect_server(host_id, server_info)
        except Exception:
            pass
        time.sleep(SERVER_CLEANUP_INTERVAL)


def _host_snapshot(host_id, server_info):
    return {
        'host_id': host_id,
        'name': server_info.get('name', host_id),
        'status': server_info.get('status', 'online'),
        'hostname': server_info.get('hostname', host_id),
        'address': server_info.get('address', host_id),
        'direct_host': server_info.get('direct_host'),
        'direct_port': server_info.get('direct_port'),
    }


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/download/RemoteControlServer.exe', methods=['GET'])
def download_server_file():
    return redirect(
        'https://github.com/Davidhaba/remote-control/releases/download/latest/RemoteControlServer.exe',
        code=302,
    )


@app.route('/download/RemoteControlCertificate.crt', methods=['GET'])
def download_certificate_file():
    return redirect(
        'https://github.com/Davidhaba/remote-control/releases/download/certificate/RemoteControlCertificate.crt',
        code=302,
    )


@app.route('/api/discover-servers', methods=['GET'])
def discover_servers():
    try:
        with state_lock:
            servers = [_host_snapshot(host_id, server_info) for host_id, server_info in registered_servers.items()]
        return jsonify({'servers': servers})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/connect', methods=['POST'])
def connect():
    data = request.get_json(silent=True) or {}
    host_id = data.get('host_id') or data.get('address')
    socket_id = data.get('socket_id')
    password = data.get('password')
    if not host_id or not socket_id:
        return jsonify({'error': 'Missing server selection'}), 400

    if not password or not str(password).strip():
        return jsonify({'error': 'Password is required'}), 400

    with state_lock:
        server = registered_servers.get(host_id)
        if not server or not server.get('sid'):
            return jsonify({'error': 'Selected server is not available'}), 404
        session = {
            'host_id': host_id,
            'socket_id': socket_id,
            'password': password,
            'authorized': False,
            'connect_event': threading.Event(),
            'connect_result': None,
        }
        browser_sessions[socket_id] = session
        active_socket_ids.add(socket_id)
        server_sid = server['sid']

    socketio.emit('request_session', {
        'browser_sid': socket_id,
        'host_id': host_id,
        'password': password,
    }, room=server_sid)

    if not session['connect_event'].wait(timeout=SERVER_RESPONSE_TIMEOUT):
        with state_lock:
            if browser_sessions.get(socket_id) is session:
                browser_sessions.pop(socket_id, None)
        socketio.emit('session_ended', {
            'browser_sid': socket_id,
            'host_id': host_id,
        }, room=server_sid)
        return jsonify({'error': 'Server unavailable'}), 503

    with state_lock:
        if browser_sessions.get(socket_id) is not session:
            return jsonify({'error': 'Server unavailable'}), 503
        connect_result = session.get('connect_result')

    if connect_result == 'denied':
        with state_lock:
            if browser_sessions.get(socket_id) is session:
                browser_sessions.pop(socket_id, None)
        return jsonify({'error': 'Authentication failed'}), 401
    if connect_result != 'ready':
        return jsonify({'error': 'Server unavailable'}), 503

    response = {'status': 'connected'}
    if server.get('direct_host') and server.get('direct_port'):
        response['direct_host'] = server['direct_host']
        response['direct_port'] = server['direct_port']
    return jsonify(response)


@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    data = request.get_json(silent=True) or {}
    socket_id = data.get('socket_id')
    with state_lock:
        active_socket_ids.discard(socket_id)
        session = browser_sessions.pop(socket_id, None)
        server = registered_servers.get(session.get('host_id')) if session else None
    if session:
        host_id = session.get('host_id')
        if server and server.get('sid'):
            socketio.emit('session_ended', {'browser_sid': socket_id, 'host_id': host_id}, room=server['sid'])
    return jsonify({'status': 'disconnected'})


@socketio.on('connect')
def handle_socket_connect():
    with state_lock:
        active_socket_ids.add(request.sid)
    return None


@socketio.on('register_server')
def handle_register_server(data):
    data = data or {}

    host_id = data.get('host_id') or f"host-{request.sid[:8]}"
    print(f'[web_server] register_host {host_id} sid={request.sid}')
    with state_lock:
        existing = registered_servers.get(host_id)
        if existing and existing.get('sid') != request.sid and existing.get('sid') in active_socket_ids:
            return

        registered_servers[host_id] = {
            'sid': request.sid,
            'name': data.get('name', host_id),
            'hostname': data.get('hostname', socket.gethostname()),
            'address': data.get('address', host_id),
            'direct_host': data.get('direct_host'),
            'direct_port': data.get('direct_port'),
            'status': 'online',
            'disconnected_at': None,
        }


@socketio.on('webrtc_offer')
def handle_webrtc_offer(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    print(f'[web_server] webrtc_offer browser_sid={browser_sid} host_id={data.get("host_id")}')
    if not browser_sid:
        return
    with state_lock:
        session = browser_sessions.get(browser_sid)
        if not session:
            print('[web_server] no browser session for offer', browser_sid)
            return
        host_id = session.get('host_id')
        server = registered_servers.get(host_id)
    if not server or not server.get('sid'):
        print('[web_server] no registered host for offer', host_id)
        return
    socketio.emit('webrtc_offer', data, room=server['sid'])


@socketio.on('webrtc_answer')
def handle_webrtc_answer(data):
    print(f'[web_server] webrtc_answer browser_sid={data.get("browser_sid")}')
    data = data or {}
    browser_sid = data.get('browser_sid')
    if browser_sid:
        socketio.emit('webrtc_answer', data, room=browser_sid)
    return


@socketio.on('webrtc_candidate')
def handle_webrtc_candidate(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    if not browser_sid:
        return
    target = (data.get('target') or 'browser').lower()
    print(f'[web_server] webrtc_candidate browser_sid={browser_sid} target={target}')
    if target == 'server':
        with state_lock:
            session = browser_sessions.get(browser_sid)
            if not session:
                print('[web_server] no browser session for candidate', browser_sid)
                return
            host_id = session.get('host_id')
            server = registered_servers.get(host_id)
        if server and server.get('sid'):
            socketio.emit('webrtc_candidate', data, room=server['sid'])
        return
    socketio.emit('webrtc_candidate', data, room=browser_sid)


@socketio.on('session_ready')
def handle_session_ready_from_server(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    if not browser_sid:
        return
    with state_lock:
        session = browser_sessions.get(browser_sid)
        if session:
            session['authorized'] = True
            session['connect_result'] = 'ready'
            if session.get('connect_event'):
                session['connect_event'].set()
    socketio.emit('session_ready', {'host_id': data.get('host_id')}, room=browser_sid)


@socketio.on('session_ended')
def handle_session_ended_from_server(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    host_id = data.get('host_id')
    if not browser_sid:
        return

    with state_lock:
        registered_server = registered_servers.get(host_id)
        if not registered_server or registered_server.get('sid') != request.sid:
            return
        session = browser_sessions.pop(browser_sid, None)
        if session:
            active_socket_ids.discard(browser_sid)

    socketio.emit('session_ended', {
        'browser_sid': browser_sid,
        'host_id': host_id,
        'reason': data.get('reason', 'server_ended'),
    }, room=browser_sid)


@socketio.on('session_denied')
def handle_session_denied_from_server(data):
    data = data or {}
    browser_sid = data.get('browser_sid')
    if not browser_sid:
        return
    with state_lock:
        session = browser_sessions.get(browser_sid)
        if session:
            session['authorized'] = False
            session['connect_result'] = 'denied'
            if session.get('connect_event'):
                session['connect_event'].set()
    socketio.emit('session_denied', {
        'browser_sid': browser_sid,
        'host_id': data.get('host_id'),
        'reason': data.get('reason'),
    }, room=browser_sid)


@socketio.on('disconnect')
def handle_socket_disconnect():
    browser_sid = request.sid
    with state_lock:
        active_socket_ids.discard(browser_sid)
        session = browser_sessions.pop(browser_sid, None)
        host_id = session.get('host_id') if session else None
        server = registered_servers.get(host_id) if host_id else None
        host_id_for_disconnect = next(
            (host_id for host_id, server_info in registered_servers.items()
             if server_info.get('sid') == browser_sid),
            None,
        )
        if host_id_for_disconnect:
            registered_servers[host_id_for_disconnect]['disconnected_at'] = time.time()

    if host_id_for_disconnect:
        _signal_host_sessions(host_id_for_disconnect, 'unavailable')

    if session and server and server.get('sid'):
        socketio.emit('session_ended', {'browser_sid': browser_sid, 'host_id': host_id}, room=server['sid'])


if __name__ == '__main__':
    threading.Thread(target=_cleanup_sessions, daemon=True).start()
    port = int(os.environ.get('PORT', 5000))
    if ASYNC_MODE == 'gevent' and GEVENT_WEBSOCKET_AVAILABLE and WSGIServer is not None and WebSocketHandler is not None:
        print('[web_server] starting gevent websocket server on 0.0.0.0:%s' % port)
        server = WSGIServer(('0.0.0.0', port), app, handler_class=WebSocketHandler)
        shutdown_requested = threading.Event()

        def _handle_gevent_signal(signum, frame=None):
            print(f'[web_server] received signal {signum}; shutting down')
            shutdown_requested.set()
            try:
                server.stop()
            except Exception:
                pass
            try:
                server.close()
            except Exception:
                pass

        try:
            import gevent.signal as gevent_signal
            gevent_signal.signal(signal.SIGINT, _handle_gevent_signal)
            gevent_signal.signal(signal.SIGTERM, _handle_gevent_signal)
        except Exception:
            try:
                signal.signal(signal.SIGINT, _handle_gevent_signal)
                signal.signal(signal.SIGTERM, _handle_gevent_signal)
            except Exception:
                pass

        try:
            server.start()
            print('[web_server] you can access the web interface at http://localhost:%s' % port)
            while not shutdown_requested.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            print('[web_server] KeyboardInterrupt received; shutting down')
            _handle_gevent_signal(signal.SIGINT)
        except Exception as e:
            if not shutdown_requested.is_set():
                print('[web_server] gevent server error:', str(e))
        finally:
            try:
                server.stop()
            except Exception:
                pass
            try:
                server.close()
            except Exception:
                pass
    else:
        socketio.run(
            app,
            host='0.0.0.0',
            port=port,
            debug=False,
            use_reloader=False,
            allow_unsafe_werkzeug=False,
        )
