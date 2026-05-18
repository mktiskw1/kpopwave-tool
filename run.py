"""
run.py — ファイル変更を検知して Flask を自動再起動するウォッチャー

使い方:
    .\venv\Scripts\python run.py

監視対象: *.py / templates/**/*.html (サブディレクトリ含む)
除外:     __pycache__ / *.pyc / instance/ / .git/
デバウンス: 1.5 秒（複数ファイル同時保存時の多重再起動を防ぐ）
"""

import os
import subprocess
import sys
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCH_EXTENSIONS = {".py", ".html"}
IGNORE_DIRS = {"__pycache__", "instance", ".git", "venv", ".venv"}
DEBOUNCE = 1.5  # 秒


class _FlaskProcess:
    def __init__(self):
        self._proc = None

    def start(self):
        self._stop_existing()
        print("[watcher] 起動: app.py", flush=True)
        self._proc = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=BASE_DIR,
        )

    def restart(self, changed_path: str):
        rel = os.path.relpath(changed_path, BASE_DIR)
        print(f"\n[watcher] 変更検知: {rel} → 再起動します", flush=True)
        self.start()

    def _stop_existing(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()

    def stop(self):
        self._stop_existing()
        print("[watcher] 終了しました", flush=True)

    def check_alive(self):
        """プロセスが予期せず死んでいたら再起動する。"""
        if self._proc and self._proc.poll() is not None:
            print("[watcher] プロセスが停止しました。再起動します...", flush=True)
            self.start()


class _ChangeHandler(FileSystemEventHandler):
    def __init__(self, flask: _FlaskProcess):
        super().__init__()
        self._flask = flask
        self._last_trigger = 0.0

    def _should_ignore(self, path: str) -> bool:
        parts = set(path.replace("\\", "/").split("/"))
        if parts & IGNORE_DIRS:
            return True
        _, ext = os.path.splitext(path)
        return ext not in WATCH_EXTENSIONS

    def on_modified(self, event):
        if event.is_directory:
            return
        if self._should_ignore(event.src_path):
            return
        now = time.monotonic()
        if now - self._last_trigger < DEBOUNCE:
            return
        self._last_trigger = now
        self._flask.restart(event.src_path)

    # 新規作成も対象（テンプレート追加など）
    on_created = on_modified


def main():
    flask = _FlaskProcess()
    flask.start()

    handler = _ChangeHandler(flask)
    observer = Observer()
    observer.schedule(handler, path=BASE_DIR, recursive=True)
    observer.start()

    print(f"[watcher] 監視開始: {BASE_DIR}", flush=True)
    print("[watcher] 終了するには Ctrl+C を押してください\n", flush=True)

    try:
        while True:
            time.sleep(2)
            flask.check_alive()
    except KeyboardInterrupt:
        print("\n[watcher] Ctrl+C を受信。終了します...", flush=True)
    finally:
        observer.stop()
        observer.join()
        flask.stop()


if __name__ == "__main__":
    main()
