"""
run.py — launch bot + mini-app server together.

    python run.py

Ctrl+C stops both. Individual processes: `python bot.py`, `python server.py`.
"""
from __future__ import annotations

import multiprocessing
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent


def _spawn(script: str) -> None:
    subprocess.run([sys.executable, str(HERE / script)], cwd=str(HERE), check=False)


def main() -> None:
    jobs = [
        multiprocessing.Process(target=_spawn, args=("bot.py",), name="telegram-bot"),
        multiprocessing.Process(target=_spawn, args=("server.py",), name="miniapp-server"),
    ]
    for job in jobs:
        job.start()
        print(f"started {job.name} (pid {job.pid})", flush=True)

    try:
        while all(j.is_alive() for j in jobs):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping…", flush=True)
    finally:
        for job in jobs:
            if job.is_alive():
                job.terminate()
        for job in jobs:
            job.join(timeout=5)
        print("stopped.", flush=True)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
