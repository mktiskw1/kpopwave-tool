"""
KPOPwave Tool Launcher
起動方法: launcher.bat をダブルクリック
"""

import tkinter as tk
from tkinter import messagebox
import subprocess
import os
import webbrowser
import sys
import re

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe")
if not os.path.exists(VENV_PYTHON):
    VENV_PYTHON = sys.executable


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


def run_claude():
    """VS Code をプロジェクトフォルダで開き、手順ポップアップを表示する"""
    try:
        subprocess.Popen(["cmd.exe", "/c", "code", PROJECT_DIR])
    except Exception:
        pass  # VS Code が PATH にない場合は無視してポップアップだけ表示

    # ── ポップアップ ──────────────────────────────────────────
    popup = tk.Toplevel(root)
    popup.title("Claude Code 起動手順")
    popup.resizable(False, False)
    popup.configure(bg=BG)
    popup.grab_set()  # モーダル

    tk.Label(
        popup,
        text="VSCode のターミナルで順番に入力してください",
        font=("Segoe UI", 11, "bold"),
        bg=BG, fg=FG,
        padx=20, pady=14,
    ).pack()

    STEPS = [
        ("① ディレクトリ移動", r"cd C:\Users\mktis\kpopwave-tool"),
        ("② Claude Code 起動", "claude"),
    ]

    def copy_to_clipboard(text, lbl):
        popup.clipboard_clear()
        popup.clipboard_append(text)
        lbl.config(text="コピーしました ✓")
        popup.after(1500, lambda: lbl.config(text="📋 コピー"))

    for step_title, cmd_text in STEPS:
        row = tk.Frame(popup, bg=PANEL, padx=16, pady=10)
        row.pack(fill="x", padx=20, pady=4)

        tk.Label(
            row, text=step_title,
            font=("Segoe UI", 9),
            bg=PANEL, fg=STATUS_FG,
            anchor="w",
        ).pack(anchor="w")

        cmd_row = tk.Frame(row, bg=PANEL)
        cmd_row.pack(fill="x", pady=(4, 0))

        tk.Label(
            cmd_row, text=cmd_text,
            font=("Consolas", 12, "bold"),
            bg=PANEL, fg="#7ec8e3",
            anchor="w",
        ).pack(side="left", fill="x", expand=True)

        copy_lbl = tk.Label(
            cmd_row, text="📋 コピー",
            font=("Segoe UI", 9),
            bg="#0f3460", fg=FG,
            padx=8, pady=2,
            cursor="hand2",
        )
        copy_lbl.pack(side="right")
        copy_lbl.bind("<Button-1>", lambda e, t=cmd_text, l=copy_lbl: copy_to_clipboard(t, l))

    tk.Button(
        popup,
        text="閉じる",
        font=("Segoe UI", 10),
        bg=ACCENT, fg=FG,
        activebackground="#c73652", activeforeground=FG,
        relief="flat", padx=20, pady=6,
        cursor="hand2",
        command=popup.destroy,
    ).pack(pady=14)

    # ポップアップを画面中央に配置
    popup.update_idletasks()
    x = root.winfo_x() + (root.winfo_width() - popup.winfo_width()) // 2
    y = root.winfo_y() + (root.winfo_height() - popup.winfo_height()) // 2
    popup.geometry(f"+{x}+{y}")

    set_status("VS Code を開きました")


def open_admin():
    chrome = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    if chrome:
        subprocess.Popen([chrome, "http://localhost:5000"])
    else:
        webbrowser.open("http://localhost:5000")
    set_status("管理画面を開きました")


def git_push():
    try:
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
root.title("KPOPwave ランチャー")
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
    text="🌊  KPOPwave Tool",
    font=("Segoe UI", 18, "bold"),
    bg=ACCENT, fg=FG,
).pack()

BUTTONS = [
    ("① ツール起動",            "▶  コマンドプロンプトで run.py を実行",  run_tool,   "#0f3460"),
    ("② Claude Code 起動",     "🤖  VS Code を開いて手順を表示",         run_claude, "#0f3460"),
    ("③ 管理画面を開く",        "🌐  localhost:5000",                    open_admin, "#2d1b69"),
    ("④ GitHub に保存",         "⬆  add → commit → push",              git_push,   "#1a472a"),
    ("⑤ GitHub から最新版取得",  "⬇  git pull",                          git_pull,   "#1a472a"),
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
