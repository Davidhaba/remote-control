import subprocess
from pathlib import Path

APP_NAME = "RemoteControlServer"
MAJOR_MINOR_VERSION = "1.1.6"
RELEASE_DIR = Path("release")
BUILD_COUNTER_FILE = RELEASE_DIR / "build_number.txt"

RELEASE_DIR.mkdir(parents=True, exist_ok=True)

if BUILD_COUNTER_FILE.exists():
    build_num = int(BUILD_COUNTER_FILE.read_text(encoding="utf-8").strip()) + 1
else:
    build_num = 1

BUILD_COUNTER_FILE.write_text(str(build_num), encoding="utf-8")

build_str = f"{build_num:03d}"
exe_filename = f"{APP_NAME}_v{MAJOR_MINOR_VERSION}_b{build_str}_win64.exe"
file_version = f"{MAJOR_MINOR_VERSION}.{build_num}"

cmd = [
    ".venv313/Scripts/python.exe",
    "-m",
    "nuitka",
    "--onefile",
    "--standalone",
    "--assume-yes-for-downloads",
    "--remove-output",
    "--output-dir=release",
    f"--output-filename={exe_filename}",
    f"--file-version={file_version}",
    f"--product-version={file_version}",
    "--product-name=Remote Control Server",
    "--file-description=Remote Control Server",
    "--company-name=Remote Control",
    "--copyright=Copyright © 2026 RemoteControl",
    "--windows-console-mode=force",
    "--include-module=av.utils",
    "--follow-import-to=av",
    "--follow-import-to=aiortc",
    "--follow-import-to=socketio",
    "--follow-import-to=engineio",
    "--follow-import-to=pyautogui",
    "--follow-import-to=pyaudiowpatch",
    "--follow-import-to=pystray",
    "--follow-import-to=PIL",
    "--follow-import-to=mss",
    "--follow-import-to=cv2",
    "remote_client.py",
]

print(f"=== Старт збірки: {exe_filename} ===")
print("Команда:")
print(" ".join(cmd))
print("===========")
try:
    subprocess.run(cmd, check=True)
    output_path = RELEASE_DIR / exe_filename

    print("\n=== Підписання EXE сертифікатом ===")
    sign_script = f"""
    $cert = Get-ChildItem Cert:\\CurrentUser\\My -CodeSigningCert | Where-Object {{ $_.Subject -match 'Remote Control Server' }} | Select-Object -First 1
    if ($cert) {{
        $result = Set-AuthenticodeSignature -FilePath '{output_path.resolve()}' -Certificate $cert
        if ($result.Status -eq 'Valid') {{
            Write-Host '[УСПІХ] EXE успішно підписано' -ForegroundColor Green
        }} else {{
            Write-Warning "[УВАГА] Статус підпису: $($result.StatusMessage)"
        }}
    }} else {{
        Write-Error '[ПОМИЛКА] Сертифікат "Remote Control Server" не знайдено в Cert:\\CurrentUser\\My'
    }}
    """
    subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-Command", sign_script],
        check=True,
    )
    print(f"\n[УСПІХ] EXE зібрано в: {output_path}")

except subprocess.CalledProcessError as exc:
    print(f"\n[ПОМИЛКА] Збірка перервалася з кодом {exc.returncode}.")
    raise SystemExit(exc.returncode) from exc

except Exception as e:
    print(f"\n[ПОМИЛКА] Виникла помилка: {e}")

input("\nPress Enter to exit...")
