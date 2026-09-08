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
    btn.bind("<Enter>", lambda e, b=btn, c=color: b.configure(bg="#3a3a5c"))
    btn.bind("<Leave>", lambda e, b=btn, c=color: b.configure(bg=c))

status_var = tk.StringVar(value="準備完了")
tk.Label(
    root,
    textvariable=status_var,
    font=("Segoe UI", 9),
    bg=PANEL, fg=STATUS_FG,
    anchor="w", padx=10, pady=4,
).pack(fill="x", side="bottom")

root.mainloop()
