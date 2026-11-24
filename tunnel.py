r"""
tunnel.py — one-command launcher.

Starts:
  1. FastAPI server (server.py) on port 8000
  2. A free public HTTPS tunnel (cloudflared quick tunnel)
  3. The Telegram bot (bot.py) once the public URL is ready

The tunnel URL is written to webapp_url.txt, which config.py reads
automatically. Result: the Mini App is served directly by the backend,
so API calls are same-origin and no Netlify/API_BASE setup is needed.

Run:  venv\Scripts\python.exe tunnel.py
Stop: Ctrl+C
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).parent
PY = ROOT / "venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)
CF = ROOT / "tools" / "cloudflared.exe"
URL_FILE = ROOT / "webapp_url.txt"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)

processes: list[subprocess.Popen] = []
found: dict[str, str] = {}


def _pump(proc: subprocess.Popen, label: str) -> None:
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, ""):
        line = raw.strip()
        if not line:
            continue
        if label == "server" or label == "bot":
            print(f"[{label}] {line}", flush=True)
        if "cloudflared" in label:
            match = URL_RE.search(line)
            if match and "url" not in found:
                found["url"] = match.group(0)
                URL_FILE.write_text(found["url"], encoding="utf-8")
                print("\n" + "=" * 64, flush=True)
                print("  PUBLIC MINI APP URL READY:", found["url"], flush=True)
                print("  Saved to webapp_url.txt", flush=True)
                print("=" * 64 + "\n", flush=True)
                _start_bot()
        if "ERR" in line and label == "cloudflared":
            print(f"[cloudflared] {line}", flush=True)


def _start() -> None:
    server = subprocess.Popen(
        [str(PY), "server.py"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    processes.append(server)
    threading.Thread(target=_pump, args=(server, "server"), daemon=True).start()

    if not CF.exists():
        raise SystemExit("tools/cloudflared.exe missing. Re-run setup.")

    tunnel = subprocess.Popen(
        [str(CF), "tunnel", "--url", "http://127.0.0.1:8000"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    processes.append(tunnel)
    threading.Thread(target=_pump, args=(tunnel, "cloudflared"), daemon=True).start()


def _start_bot() -> None:
    if found.get("bot_started") or os.getenv("SKIP_BOT") == "1":
        return
    found["bot_started"] = "1"
    time.sleep(2)
    bot = subprocess.Popen(
        [str(PY), "bot.py"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    processes.append(bot)
    threading.Thread(target=_pump, args=(bot, "bot"), daemon=True).start()
    print("[launcher] Telegram bot started.\n", flush=True)


def main() -> None:
    print("Starting FluxProxy backend + public tunnel...", flush=True)
    print("Waiting for a public HTTPS URL (usually 5-15 seconds)...\n", flush=True)
    _start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping everything...", flush=True)
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        print("Stopped.", flush=True)


if __name__ == "__main__":
    main()
