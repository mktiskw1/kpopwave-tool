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
import re
import subprocess
import sys
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCH_EXTENSIONS = {".py", ".html"}
IGNORE_DIRS = {"__pycache__", "instance", ".git", "venv", ".venv", "static"}
DEBOUNCE = 2.0       # 秒：同一トリガーを無視する時間
POLL_INTERVAL = 1.0  # 秒：PollingObserver のポーリング間隔
IS_WINDOWS = platform.system() == "Windows"


class _FlaskProcess:
    """app.py の起動・停止を管理する。

    start()/stop()/check_alive() は、ファイル変更検知スレッド(watchdogのディスパッチャー)と
    メインスレッド(main() の check_alive ポーリングループ)の両方から self._proc に非同期に
    アクセスする。ロックなしだと、taskkill で古いプロセスを止めている最中に check_alive() が
    「プロセスが予期せず停止した」と誤検知して独自に再起動を割り込ませ、app.py が二重起動する
    競合状態が発生するため、_lock で全操作を排他化する。
    """

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            self._start_locked()

    def _start_locked(self):
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
        with self._lock:
            self._stop_existing()
        print("[watcher] 終了しました", flush=True)

    def check_alive(self):
        """プロセスが予期せず死んでいたら再起動する。"""
        with self._lock:
            if self._proc and self._proc.poll() is not None:
                print("[watcher] プロセスが停止しました。再起動します...", flush=True)
                self._start_locked()


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


def _schedule_watches(observer: PollingObserver, handler: FileSystemEventHandler) -> None:
    """BASE_DIR 直下を非再帰で、IGNORE_DIRS 以外のサブディレクトリのみ再帰で監視登録する。

    IGNORE_DIRS は on_modified/on_created 内のフィルタにしか使われず、PollingObserver.schedule()
    に BASE_DIR を丸ごと recursive=True で渡すと venv/（数千ファイル）や static/（動画、数十GB）
    まで毎秒スキャン対象に含まれてしまう（IGNORE_DIRS では除外されない）。個別にスケジュール登録
    することでスキャン範囲そのものを絞り込む。
    """
    observer.schedule(handler, path=BASE_DIR, recursive=False)
    with os.scandir(BASE_DIR) as entries:
        for entry in entries:
            if entry.is_dir() and entry.name not in IGNORE_DIRS:
                observer.schedule(handler, path=entry.path, recursive=True)


def _ensure_sleep_disabled():
    """PCのスリープ(AC/DCとも)を無効化する。既に無効ならAPIコールを行わず即座に戻る(冪等)。

    以前はランチャーの手動トグルボタン(⑦)で有効/無効を切り替えていたが、無効化し忘れた
    状態が続くと日次ジョブ(track_post_stats等)がスリープ中に丸ごと欠落する
    (2026-09-10に実際に発生・確認済み)。ボタン削除後の恒久対策として、ツール起動の
    たびに自動で確認・設定する。失敗しても(権限不足等)起動自体は継続する。"""
    if not IS_WINDOWS:
        return
    try:
        result = subprocess.run(
            ["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"],
            capture_output=True, text=True, timeout=10,
        )
        # powercfgの出力ラベルはOSの表示言語によって変わる(例: 日本語環境では
        # "現在の AC 電源設定のインデックス: 0x..." のようにローカライズされる)が、
        # "AC"/"DC" というトークン自体はどの言語でも変わらないため、それを手掛かりに
        # 同一行内のhex値を拾う(行をまたいで誤マッチしないよう[^\n]*で制限する)。
        ac_m = re.search(r"AC[^\n]*0x([0-9a-fA-F]+)", result.stdout)
        dc_m = re.search(r"DC[^\n]*0x([0-9a-fA-F]+)", result.stdout)
        ac = int(ac_m.group(1), 16) if ac_m else None
        dc = int(dc_m.group(1), 16) if dc_m else None

        if ac == 0 and dc == 0:
            print("[watcher] スリープは既に無効化されています", flush=True)
            return

        subprocess.run(["powercfg", "/change", "standby-timeout-ac", "0"], check=True, capture_output=True)
        subprocess.run(["powercfg", "/change", "standby-timeout-dc", "0"], check=True, capture_output=True)
        print("[watcher] スリープを無効化しました(日次ジョブの欠落防止)", flush=True)
    except Exception as exc:
        print(f"[watcher] スリープ無効化の確認/設定に失敗しました(起動は継続します): {exc}", flush=True)


def main():
    _ensure_sleep_disabled()

    flask = _FlaskProcess()
    flask.start()

    handler = _ChangeHandler(flask)
    observer = PollingObserver(timeout=POLL_INTERVAL)
    _schedule_watches(observer, handler)
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
