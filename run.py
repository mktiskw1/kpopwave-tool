"""
run.py — ファイル変更を検知して Flask を自動再起動するウォッチャー

使い方:
    .\venv\Scripts\python run.py

監視対象: *.py / templates/**/*.html (サブディレクトリ含む)
除外:     __pycache__ / instance / .git / venv
デバウンス: 2.0 秒（複数ファイル同時保存時の多重再起動を防ぐ）
ポーリング: 1.0 秒ごとにファイル変更を確認（PollingObserver で確実に検知）
"""

import os
import platform
import subprocess
import sys
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCH_EXTENSIONS = {".py", ".html"}
IGNORE_DIRS = {"__pycache__", "instance", ".git", "venv", ".venv"}
DEBOUNCE = 2.0       # 秒：同一トリガーを無視する時間
POLL_INTERVAL = 1.0  # 秒：PollingObserver のポーリング間隔
IS_WINDOWS = platform.system() == "Windows"


class _FlaskProcess:
    def __init__(self):
        self._proc = None

    def start(self):
        self._stop_existing()
        time.sleep(0.5)  # ポート解放を待つ
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
        if not (self._proc and self._proc.poll() is None):
            return
        try:
            if IS_WINDOWS:
                # taskkill でプロセスツリー全体を強制終了
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                    capture_output=True,
                )
            else:
                self._proc.terminate()
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

    def _trigger(self, path: str):
        now = time.monotonic()
        if now - self._last_trigger < DEBOUNCE:
            return
        self._last_trigger = now
        self._flask.restart(path)

    def on_modified(self, event):
        if event.is_directory:
            return
        if self._should_ignore(event.src_path):
            return
        self._trigger(event.src_path)

    def on_created(self, event):
        if event.is_directory:
            return
        if self._should_ignore(event.src_path):
            return
        self._trigger(event.src_path)

    def on_moved(self, event):
        # エディタがアトミック書き込み（temp ファイル → rename）をする場合の対応
        if event.is_directory:
            return
        if self._should_ignore(event.dest_path):
            return
        self._trigger(event.dest_path)


def main():
    flask = _FlaskProcess()
    flask.start()

    handler = _ChangeHandler(flask)
    observer = PollingObserver(timeout=POLL_INTERVAL)
    observer.schedule(handler, path=BASE_DIR, recursive=True)
    observer.start()

    print(f"[watcher] 監視開始: {BASE_DIR}", flush=True)
    print(f"[watcher] ポーリング間隔: {POLL_INTERVAL}秒 / デバウンス: {DEBOUNCE}秒", flush=True)
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
