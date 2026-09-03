# Remote Control

Remote Control consists of a web panel and a Windows host for remotely controlling a computer.

- `web_server.py` runs the web interface, Socket.IO relay, and WebRTC signaling.
- `remote_host.py` runs on the computer being controlled. It registers with the relay, streams video/system audio, and receives mouse and keyboard commands.
- `templates/index.html` and `static/style.css` contain the panel interface.

## Installation

Windows and Python 3.13 or a compatible version are required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements_server.txt
python -m pip install -r requirements_web_service.txt
```

On Windows, the host may require permissions for screen capture, audio capture, and mouse/keyboard control. If PowerShell blocks environment activation:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## Running

1. Start the relay and web panel:

   ```powershell
   python web_server.py
   ```

   Open `http://localhost:5000`. You can change the port with `PORT`:

   ```powershell
   $env:PORT = "8080"
   python web_server.py
   ```

2. On the Windows computer you want to control, start the host:

   ```powershell
   python remote_host.py
   ```

   The host prompts for a password when one is not provided as an argument and automatically generates an ID from the computer name.

3. On the web page, click `Scan for hosts`, select the required computer, and enter its password.

The web panel and host can run on different computers. In that case, both must be able to reach the configured relay over the network.

## Host Options

```text
--password PASSWORD    password for connecting
--relay URL             Socket.IO relay address
--id ID                 custom host ID
--debug                enable verbose logging
```

Example:

```powershell
python remote_host.py --id office-pc --password "your-password"
```

The default relay is `https://remote-control-ee7w.onrender.com`. You can change it with `--relay` or the `RELAY_URL` environment variable.

## Web Panel

After connecting, the following features are available:

- remote screen and system audio;
- mouse and keyboard control;
- separate `Mouse`, `Keyboard`, and `Navigation` panels;
- `Touchpad` and `Touch control` modes for mobile browsers;
- `Low`, `Medium`, and `High` quality profiles with 15/30/60 FPS;
- separate controls for host data and browser commands;
- fullscreen mode, reconnection, and a list of recent computers.

To end a session, click `Back` or close the connection through the controls panel.

## Logs and Security

Host logs are written to `logs/`. Use long, unique passwords, do not expose the web panel publicly without additional protection, and do not share passwords with others. The relay can access connection metadata, so evaluate the trust model and infrastructure configuration separately before deploying in sensitive environments.
