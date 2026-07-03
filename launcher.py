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
import re
import json
import threading
import time
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


def _show_claude_instructions():
    """Claude Code 起動手順をポップアップで表示する"""
    popup = tk.Toplevel(root)
    popup.title("Claude Code 起動手順")
    popup.resizable(False, False)
    popup.configure(bg=BG)
    popup.grab_set()

    # 中央寄せ
    popup.update_idletasks()
    pw, ph = 420, 300
    rx = root.winfo_x() + (root.winfo_width() - pw) // 2
    ry = root.winfo_y() + (root.winfo_height() - ph) // 2
    popup.geometry(f"{pw}x{ph}+{rx}+{ry}")

    SEP = "━" * 24

    def copy_text(text, btn):
        popup.clipboard_clear()
        popup.clipboard_append(text)
        orig = btn.cget("text")
        btn.config(text="✓ コピー済み", bg="#1a6a1a")
        popup.after(1500, lambda: btn.config(text=orig, bg="#0f3460"))

    tk.Label(popup, text=SEP, font=("Consolas", 11), bg=BG, fg=ACCENT).pack(pady=(14, 0))
    tk.Label(popup, text="Claude Code 起動手順", font=("Segoe UI", 13, "bold"), bg=BG, fg=FG).pack()
    tk.Label(popup, text=SEP, font=("Consolas", 11), bg=BG, fg=ACCENT).pack(pady=(0, 6))

    tk.Label(popup, text="VSCode のターミナルで以下を順番に入力：",
             font=("Segoe UI", 10), bg=BG, fg=STATUS_FG).pack(pady=(0, 10))

    for step, cmd in [("Step 1", r".\venv\Scripts\activate"), ("Step 2", "claude")]:
        row = tk.Frame(popup, bg=BG)
        row.pack(fill="x", padx=30, pady=4)
        tk.Label(row, text=f"{step}:", font=("Segoe UI", 10, "bold"), bg=BG, fg=FG,
                 width=7, anchor="w").pack(side="left")
        tk.Label(row, text=cmd, font=("Consolas", 11), bg=PANEL, fg="#7ec8e3",
                 padx=8, pady=4, relief="flat").pack(side="left", expand=True, fill="x")
        copy_btn = tk.Button(row, text="コピー", font=("Segoe UI", 9),
                             bg="#0f3460", fg=FG, relief="flat", bd=0,
                             padx=10, cursor="hand2")
        copy_btn.config(command=lambda t=cmd, b=copy_btn: copy_text(t, b))
        copy_btn.pack(side="left", padx=(6, 0))

    tk.Label(popup, text=SEP, font=("Consolas", 11), bg=BG, fg=ACCENT).pack(pady=(10, 6))

    tk.Button(popup, text="OK", font=("Segoe UI", 10),
              bg=ACCENT, fg=FG, relief="flat", bd=0, padx=20, pady=6,
              cursor="hand2", command=popup.destroy).pack()

    return popup


def run_claude():
    """VS Code を起動すると同時に起動手順ポップアップを表示する"""
    from urllib.parse import quote

    # ① VSCode をプロジェクトフォルダで起動
    try:
        subprocess.Popen(["cmd.exe", "/c", "code", PROJECT_DIR])
        set_status("VSCode を起動しました。claude.ai を開いています...")
    except Exception:
        set_status("VSCode が見つかりません（PATH を確認してください）")
        return

    # ② バックグラウンドで待機してから Simple Browser を開く
    def _open_simple_browser():
        time.sleep(3)
        try:
            target_url = "https://claude.ai"
            vscode_uri = f"vscode://vscode.simple-browser/open?url={quote(target_url, safe='')}"
            subprocess.Popen(["cmd.exe", "/c", "code", "--open-url", vscode_uri])
        except Exception:
            pass

    threading.Thread(target=_open_simple_browser, daemon=True).start()
    set_status("VS Code を開きました")

    # ③ 起動手順ポップアップを表示（OK で閉じるだけ）
    _show_claude_instructions()


def open_admin():
    chrome = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    if chrome:
        subprocess.Popen([chrome, "http://localhost:5000"])
    else:
        webbrowser.open("http://localhost:5000")
    set_status("管理画面を開きました")


def _ensure_git_config():
    """git user.email / user.name が未設定なら自動で設定する"""
    email = subprocess.run(
        ["git", "config", "--global", "user.email"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not email:
        subprocess.run(["git", "config", "--global", "user.email", "mktiskw1@gmail.com"])
        subprocess.run(["git", "config", "--global", "user.name", "mktis"])


def git_push():
    try:
        _ensure_git_config()
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=PROJECT_DIR,
        )
        if not result.stdout.strip():
            messagebox.showinfo("GitHub に保存", "変更はありません。")
            set_status("変更なし")
            return
        subprocess.run(["git", "add", "-A"], cwd=PROJECT_DIR, check=True)
        subprocess.run(
            ["git", "commit", "-m", "chore: auto-save from launcher"],
            cwd=PROJECT_DIR, check=True,
        )
        subprocess.run(["git", "push"], cwd=PROJECT_DIR, check=True)
        messagebox.showinfo("GitHub に保存", "プッシュが完了しました。")
        set_status("GitHub へのプッシュ完了")
    except subprocess.CalledProcessError as e:
        messagebox.showerror("エラー", f"Git 操作に失敗:\n{e}")
        set_status("Git エラー")


def git_pull():
    try:
        result = subprocess.run(
            ["git", "pull"],
            capture_output=True, text=True, cwd=PROJECT_DIR,
        )
        msg = result.stdout.strip() or result.stderr.strip() or "完了"
        messagebox.showinfo("GitHub から取得", msg)
        set_status("GitHub から取得完了")
    except Exception as e:
        messagebox.showerror("エラー", f"Git pull に失敗:\n{e}")
        set_status("Git pull エラー")


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


# ── スリープ制御 ────────────────────────────────────────────
_sleep_disabled = False
_original_ac_min = None
_original_dc_min = None


def _get_sleep_timeouts():
    """現在のスリープタイムアウト（分）を取得して返す"""
    try:
        result = subprocess.run(
            ["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"],
            capture_output=True, text=True,
        )
        ac_m = re.search(r"Current AC Power Setting Index: 0x([0-9a-fA-F]+)", result.stdout)
        dc_m = re.search(r"Current DC Power Setting Index: 0x([0-9a-fA-F]+)", result.stdout)
        ac = int(ac_m.group(1), 16) // 60 if ac_m else 30
        dc = int(dc_m.group(1), 16) // 60 if dc_m else 15
        return ac, dc
    except Exception:
        return 30, 15


def toggle_sleep():
    global _sleep_disabled, _original_ac_min, _original_dc_min
    COLOR_ON  = "#1e3a1e"   # 有効時（通常）
    COLOR_OFF = "#4a1a6a"   # 無効中（紫）

    if not _sleep_disabled:
        if _original_ac_min is None:
            _original_ac_min, _original_dc_min = _get_sleep_timeouts()
        try:
            subprocess.run(["powercfg", "/change", "standby-timeout-ac", "0"], check=True)
            subprocess.run(["powercfg", "/change", "standby-timeout-dc", "0"], check=True)
            _sleep_disabled = True
            _sleep_outer.config(bg=COLOR_OFF)
            _sleep_btn.config(
                text="  スリープ無効中🌙\n  クリックでスリープを有効に戻す  ",
                bg=COLOR_OFF,
            )
            _sleep_btn.bind("<Leave>", lambda e: _sleep_btn.configure(bg=COLOR_OFF))
            _sleep_btn.bind("<Enter>", lambda e: _sleep_btn.configure(bg="#6a2a8a"))
            set_status("スリープを無効にしました")
        except subprocess.CalledProcessError as e:
            messagebox.showerror("エラー", f"スリープ設定の変更に失敗:\n{e}\n※管理者権限が必要な場合があります")
    else:
        ac = _original_ac_min if _original_ac_min else 30
        dc = _original_dc_min if _original_dc_min else 15
        try:
            subprocess.run(["powercfg", "/change", "standby-timeout-ac", str(ac)], check=True)
            subprocess.run(["powercfg", "/change", "standby-timeout-dc", str(dc)], check=True)
            _sleep_disabled = False
            _sleep_outer.config(bg=COLOR_ON)
            _sleep_btn.config(
                text="  スリープ有効\n  クリックでスリープを無効にする  ",
                bg=COLOR_ON,
            )
            _sleep_btn.bind("<Leave>", lambda e: _sleep_btn.configure(bg=COLOR_ON))
            _sleep_btn.bind("<Enter>", lambda e: _sleep_btn.configure(bg="#3a3a5c"))
            set_status("スリープを有効に戻しました")
        except subprocess.CalledProcessError as e:
            messagebox.showerror("エラー", f"スリープ設定の変更に失敗:\n{e}")


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
    ("② Claude Code 起動", "🤖  VS Code + claude.ai を開く",          run_claude, "#0f3460"),
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
    btn.bind("<Enter>", lambda e, b=btn, c=color: b.configure(bg="#3a3a5c"))
    btn.bind("<Leave>", lambda e, b=btn, c=color: b.configure(bg=c))

# ── スリープトグルボタン ────────────────────────────────────
_SLEEP_COLOR = "#1e3a1e"
_sleep_outer = tk.Frame(frame, bg=_SLEEP_COLOR)
_sleep_outer.pack(fill="x", pady=5)
_sleep_btn = tk.Button(
    _sleep_outer,
    text="  スリープ有効\n  クリックでスリープを無効にする  ",
    font=("Segoe UI", 11),
    anchor="w", justify="left",
    bg=_SLEEP_COLOR, fg=FG,
    activebackground="#3a3a5c", activeforeground=FG,
    relief="flat", bd=0,
    padx=16, pady=10,
    cursor="hand2",
    command=toggle_sleep,
)
_sleep_btn.pack(fill="x")
_sleep_btn.bind("<Enter>", lambda e: _sleep_btn.configure(bg="#3a3a5c"))
_sleep_btn.bind("<Leave>", lambda e: _sleep_btn.configure(bg=_SLEEP_COLOR))

status_var = tk.StringVar(value="準備完了")
tk.Label(
    root,
    textvariable=status_var,
    font=("Segoe UI", 9),
    bg=PANEL, fg=STATUS_FG,
    anchor="w", padx=10, pady=4,
).pack(fill="x", side="bottom")

root.mainloop()
