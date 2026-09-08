"""
ContentWave Launcher
起動方法: launcher.bat をダブルクリック
"""

import tkinter as tk
from tkinter import messagebox
import subprocess
import os
import webbrowser
import sys
import json
import queue
import threading
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe")
if not os.path.exists(VENV_PYTHON):
    VENV_PYTHON = sys.executable

DB_PATH = os.path.join(PROJECT_DIR, "instance", "rock_metal.db")


def run_tool():
    try:
        subprocess.Popen(
            f'cmd.exe /k "{VENV_PYTHON}" run.py',
            cwd=PROJECT_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        set_status("ツールを起動しました")
    except Exception as e:
        messagebox.showerror("エラー", f"起動に失敗しました:\n{e}")


CHROME_PATHS = [
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


# Claude Desktop (Microsoft Store / MSIX 版) の AppUserModelID
CLAUDE_DESKTOP_AUMID = "Claude_pzs8sxrjxfjjc!Claude"


def run_claude():
    """Claude Desktop アプリを起動する"""
    # ① Store アプリ (AppUserModelID) 経由で起動 ― バージョンに依存しない標準的な方法
    try:
        subprocess.Popen(
            ["explorer.exe", f"shell:AppsFolder\\{CLAUDE_DESKTOP_AUMID}"],
            cwd=PROJECT_DIR,
        )
        set_status("Claude Desktop を起動しました")
        return
    except Exception:
        pass

    # ② フォールバック: インストール済みの実行ファイルを直接探して起動
    candidates = []
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    windows_apps = os.path.join(program_files, "WindowsApps")
    try:
        for name in os.listdir(windows_apps):
            if name.lower().startswith("claude_"):
                exe = os.path.join(windows_apps, name, "app", "Claude.exe")
                if os.path.exists(exe):
                    candidates.append(exe)
    except Exception:
        pass
    candidates += [
        os.path.expandvars(r"%LOCALAPPDATA%\AnthropicClaude\claude.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\claude\Claude.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Claude\Claude.exe"),
    ]
    for exe in candidates:
        if os.path.exists(exe):
            try:
                subprocess.Popen([exe])
                set_status("Claude Desktop を起動しました")
                return
            except Exception:
                continue

    messagebox.showerror(
        "エラー",
        "Claude Desktop が見つかりませんでした。\n"
        "アプリがインストールされているか確認してください。",
    )
    set_status("Claude Desktop が見つかりません")


def open_admin():
    chrome = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    if chrome:
        subprocess.Popen([chrome, "http://localhost:5000"])
    else:
        webbrowser.open("http://localhost:5000")
    set_status("管理画面を開きました")


# ── Git 共通ヘルパー ───────────────────────────────────────
_git_lock = threading.Lock()
git_buttons = []          # ⑤⑥ のボタン widget（GUI 構築時に登録）
_ui_queue = queue.Queue()  # ワーカースレッド → メインスレッドの UI 更新キュー


def _git_env():
    """認証プロンプトでフリーズしないよう非対話モードを強制した環境変数"""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"   # ターミナルでの資格情報入力を無効化（ハング防止）
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _git(args, timeout=180):
    """git を PROJECT_DIR で実行して CompletedProcess を返す（出力キャプチャ）"""
    return subprocess.run(
        ["git", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=PROJECT_DIR, env=_git_env(), timeout=timeout,
    )


def _git_output(result):
    return ((result.stdout or "") + "\n" + (result.stderr or "")).strip()


def _ui(func):
    """ワーカースレッドから UI 更新をメインスレッドへ委譲する（tkinter はスレッド非対応のためキュー経由）"""
    _ui_queue.put(func)


def _pump_ui():
    """メインスレッドでキューに溜まった UI 更新を処理する（定期実行）"""
    try:
        while True:
            try:
                func = _ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                func()
            except Exception:
                pass
    finally:
        try:
            root.after(80, _pump_ui)
        except Exception:
            pass


def _status_async(msg):
    _ui(lambda: set_status(msg))


def _info_async(title, msg):
    _ui(lambda: messagebox.showinfo(title, msg))


def _error_async(title, msg):
    _ui(lambda: messagebox.showerror(title, msg))


def _set_git_buttons_state(state):
    def apply():
        for b in git_buttons:
            try:
                b.config(state=state)
            except Exception:
                pass
    _ui(apply)


def _run_git_task(worker, busy_msg):
    """worker を別スレッドで実行し、実行中は ⑤⑥ を無効化してステータス表示する"""
    if not _git_lock.acquire(blocking=False):
        set_status("Git 処理を実行中です。しばらくお待ちください")
        return

    def runner():
        try:
            _set_git_buttons_state("disabled")
            _status_async(busy_msg)
            worker()
        except subprocess.TimeoutExpired:
            _status_async("Git 処理がタイムアウトしました")
            _error_async(
                "Git",
                "処理が時間内に完了しませんでした。\n"
                "ネットワーク接続や GitHub の認証状態を確認してください。",
            )
        except FileNotFoundError:
            _status_async("git が見つかりません")
            _error_async("Git", "git コマンドが見つかりませんでした。Git のインストールを確認してください。")
        except Exception as e:
            _status_async("Git エラー")
            _error_async("Git", f"予期しないエラーが発生しました:\n{e}")
        finally:
            _set_git_buttons_state("normal")
            _git_lock.release()

    threading.Thread(target=runner, daemon=True).start()


def _ensure_git_config():
    """git user.email / user.name が未設定なら自動で設定する"""
    email = subprocess.run(
        ["git", "config", "--global", "user.email"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    if not email:
        subprocess.run(["git", "config", "--global", "user.email", "mktiskw1@gmail.com"], timeout=10)
        subprocess.run(["git", "config", "--global", "user.name", "mktis"], timeout=10)


_AUTH_KEYWORDS = [
    "authentication failed", "could not read username", "could not read password",
    "terminal prompts disabled", "invalid username or password",
    "password authentication", "permission denied", "403 forbidden",
    "remote: support for password authentication", "authentication is required",
]

_NETWORK_KEYWORDS = [
    "could not resolve host", "could not resolve proxy", "failed to connect",
    "connection refused", "connection timed out", "operation timed out",
    "network is unreachable", "temporary failure in name resolution",
    "ssl certificate problem", "gnutls_handshake",
]


def _classify_common_failure(out, retry_cmd):
    """認証エラー / ネットワークエラーの共通分類。該当すれば (status, dialog) を返す。"""
    low = out.lower()
    if any(k in low for k in _AUTH_KEYWORDS):
        return (
            "認証エラー",
            "GitHub の認証に失敗しました。\n\n"
            "認証情報が無効、または未設定の可能性があります。\n"
            f"コマンドプロンプトで一度\n    {retry_cmd}\n"
            "を実行して認証し直すか、詳しい方に相談してください。\n\n" + out,
        )
    if any(k in low for k in _NETWORK_KEYWORDS):
        return (
            "ネットワークエラー",
            "GitHub に接続できませんでした。\n\n"
            "インターネット接続を確認してから、もう一度お試しください。\n\n" + out,
        )
    return None


# ── ⑤ GitHub 保存（add → commit → push） ───────────────────
def _push_worker():
    _ensure_git_config()

    status = _git(["status", "--porcelain"])
    if not status.stdout.strip():
        _status_async("変更なし")
        _info_async("GitHub に保存", "変更はありません。")
        return

    add = _git(["add", "-A"])
    if add.returncode != 0:
        _status_async("保存失敗：add でエラー")
        _error_async("GitHub に保存", "git add に失敗しました。\n\n" + _git_output(add))
        return

    commit = _git(["commit", "-m", "chore: auto-save from launcher"])
    if commit.returncode != 0:
        _status_async("保存失敗：commit でエラー")
        _error_async("GitHub に保存", "git commit に失敗しました。\n\n" + _git_output(commit))
        return

    push = _git(["push"])
    if push.returncode != 0:
        out = _git_output(push)
        low = out.lower()
        if any(k in low for k in ["rejected", "non-fast-forward", "fetch first", "updates were rejected"]):
            _status_async("保存失敗：リモートが先行しています")
            _error_async(
                "GitHub に保存",
                "リモート（GitHub 側）に未取得の変更があるため保存できませんでした。\n\n"
                "先に ⑥「GitHub 取得」を実行してから、もう一度 ⑤「GitHub 保存」を押してください。\n\n" + out,
            )
            return
        common = _classify_common_failure(out, "git push")
        if common:
            st_msg, dialog = common
            _status_async("保存失敗：" + st_msg)
            _error_async("GitHub に保存", dialog)
            return
        _status_async("保存失敗：push でエラー")
        _error_async("GitHub に保存", "git push に失敗しました。\n\n" + out)
        return

    _status_async("GitHub へのプッシュ完了")
    _info_async("GitHub に保存", "プッシュが完了しました。")


def git_push():
    _run_git_task(_push_worker, "GitHub に保存中... (処理中)")


# ── ⑥ GitHub 取得（git pull） ─────────────────────────────
def _pull_worker():
    # --no-edit: マージコミットでエディタ待ちにならないようにする
    # --no-rebase: 分岐時の "how to reconcile" エラーを避け、マージで取り込む
    pull = _git(["pull", "--no-rebase", "--no-edit"])
    out = _git_output(pull)
    low = out.lower()

    if pull.returncode == 0:
        _status_async("GitHub から取得完了")
        _info_async("GitHub から取得", out or "取得が完了しました。")
        return

    if "conflict" in low or "automatic merge failed" in low or "fix conflicts" in low:
        _status_async("取得失敗：コンフリクトが発生しています")
        _error_async(
            "GitHub から取得",
            "マージコンフリクトが発生しました。\n\n"
            "安全のため、自動での解消は行いません。\n"
            "コマンドプロンプトで\n"
            "    git status\n"
            "を実行して状態を確認するか、詳しい方に相談してください。\n\n" + out,
        )
    elif "would be overwritten" in low or "commit your changes or stash" in low or "local changes to the following" in low:
        _status_async("取得失敗：ローカルに未保存の変更があります")
        _error_async(
            "GitHub から取得",
            "ローカルに未コミットの変更があるため取得できませんでした。\n\n"
            "先に ⑤「GitHub 保存」で変更を保存するか、詳しい方に相談してください。\n\n" + out,
        )
    elif "divergent branches" in low or "need to specify how to reconcile" in low:
        _status_async("取得失敗：履歴が分岐しています")
        _error_async(
            "GitHub から取得",
            "ローカルとリモートの履歴が分岐しています。\n"
            "詳しい方に相談してください。\n\n" + out,
        )
    else:
        common = _classify_common_failure(out, "git pull")
        if common:
            st_msg, dialog = common
            _status_async("取得失敗：" + st_msg)
            _error_async("GitHub から取得", dialog)
        else:
            _status_async("取得失敗")
            _error_async("GitHub から取得", "git pull に失敗しました。\n\n" + out)


def git_pull():
    _run_git_task(_pull_worker, "GitHub から取得中... (処理中)")


def set_status(msg):
    status_var.set(msg)


# ── Cloudflare Tunnel ─────────────────────────────────────────
def run_tunnel():
    bat_path = os.path.join(PROJECT_DIR, "start_cloudflare.bat")
    if not os.path.exists(bat_path):
        messagebox.showerror("エラー", f"start_cloudflare.bat が見つかりません:\n{bat_path}")
        return
    try:
        subprocess.Popen(
            f'cmd.exe /c "{bat_path}"',
            cwd=PROJECT_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        set_status("Cloudflare Tunnel (contentwave) を起動しました")
    except Exception as e:
        messagebox.showerror("エラー", f"起動に失敗しました:\n{e}")


# ── GUI ────────────────────────────────────────────────────
root = tk.Tk()
root.title("ContentWave ランチャー")
root.resizable(False, False)

BG        = "#1a1a2e"
PANEL     = "#16213e"
ACCENT    = "#e94560"
FG        = "#ffffff"
STATUS_FG = "#a0a0c0"

root.configure(bg=BG)

tk.Frame(root, bg=ACCENT, pady=8).pack(fill="x")
tk.Label(
    root.winfo_children()[-1],
    text="🌊  ContentWave",
    font=("Segoe UI", 18, "bold"),
    bg=ACCENT, fg=FG,
).pack()

BUTTONS = [
    ("① ツール起動",        "▶  コマンドプロンプトで run.py を実行",   run_tool,   "#0f3460"),
    ("② Claude Desktop 起動", "🤖  Claude Desktop アプリを開く",         run_claude, "#0f3460"),
    ("③ トンネル起動",      "🌐  Cloudflare Tunnel → URL を設定に反映", run_tunnel, "#0d4d4d"),
    ("④ 管理画面を開く",    "🌐  Chrome で localhost:5000 を開く",     open_admin, "#2d1b69"),
    ("⑤ GitHub 保存",      "⬆  add → commit → push",                 git_push,   "#1a472a"),
    ("⑥ GitHub 取得",      "⬇  git pull",                            git_pull,   "#1a472a"),
]

frame = tk.Frame(root, bg=BG, padx=20, pady=15)
frame.pack(fill="both")

for title, subtitle, cmd, color in BUTTONS:
    outer = tk.Frame(frame, bg=color)
    outer.pack(fill="x", pady=5)
    btn = tk.Button(
        outer,
        text=f"  {title}\n  {subtitle}  ",
        font=("Segoe UI", 11),
        anchor="w", justify="left",
        bg=color, fg=FG,
        activebackground="#3a3a5c", activeforeground=FG,
        relief="flat", bd=0,
        padx=16, pady=10,
        cursor="hand2",
        command=cmd,
    )
    btn.pack(fill="x")
    btn.bind("<Enter>", lambda e, b=btn, c=color: b.configure(bg="#3a3a5c")
             if str(b["state"]) != "disabled" else None)
    btn.bind("<Leave>", lambda e, b=btn, c=color: b.configure(bg=c)
             if str(b["state"]) != "disabled" else None)
    if cmd in (git_push, git_pull):
        git_buttons.append(btn)

status_var = tk.StringVar(value="準備完了")
tk.Label(
    root,
    textvariable=status_var,
    font=("Segoe UI", 9),
    bg=PANEL, fg=STATUS_FG,
    anchor="w", padx=10, pady=4,
).pack(fill="x", side="bottom")

_pump_ui()   # ワーカースレッドからの UI 更新キューの処理を開始
root.mainloop()
