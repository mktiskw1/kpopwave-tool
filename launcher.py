"""
KPOPwave Tool Launcher — デスクトップランチャー GUI
起動方法: launcher.bat をダブルクリック
"""

import tkinter as tk
from tkinter import messagebox
import subprocess
import shutil
import os
import webbrowser
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe")

if not os.path.exists(VENV_PYTHON):
    VENV_PYTHON = sys.executable


def _find_code():
    """code.cmd のフルパスを返す（CLI フラグを処理するバッチラッパー）"""
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd"),
        r"C:\Program Files\Microsoft VS Code\bin\code.cmd",
        r"C:\Program Files (x86)\Microsoft VS Code\bin\code.cmd",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return shutil.which("code")


VSCODE_CODE = _find_code()


def _open_vscode_with_profile(profile: str):
    """
    VS Code でプロジェクトを開き、ターミナルプロファイルを起動する。

    1回の code.cmd 呼び出しで folder + --command を同時指定することで、
    コマンドが必ず同じウィンドウ（プロジェクトウィンドウ）に届く。
    ターミナルプロファイル側でコマンドが自動実行されるため sendSequence 不要。
    """
    subprocess.Popen(
        ["cmd.exe", "/c", VSCODE_CODE,
         PROJECT_DIR,
         "--command", "workbench.action.terminal.newWithProfile",
         profile]
    )


def run_tool():
    """VS Code のターミナルプロファイル kpopwave-run で run.py を起動"""
    if not VSCODE_CODE:
        messagebox.showerror(
            "VS Code が見つかりません",
            "%LOCALAPPDATA%\\Programs\\Microsoft VS Code\\bin\\ を確認してください。",
        )
        return
    _open_vscode_with_profile("kpopwave-run")
    set_status("VS Code でツールを起動しました")


def run_claude():
    """VS Code のターミナルプロファイル kpopwave-claude で claude を起動"""
    if not VSCODE_CODE:
        messagebox.showerror(
            "VS Code が見つかりません",
            "%LOCALAPPDATA%\\Programs\\Microsoft VS Code\\bin\\ を確認してください。",
        )
        return
    _open_vscode_with_profile("kpopwave-claude")
    set_status("VS Code で Claude Code を起動しました")


def git_push():
    """変更を GitHub に保存 (add → commit → push)"""
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
    """GitHub から最新版を取得"""
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


def open_admin():
    """ブラウザで管理画面 (localhost:5000) を開く"""
    webbrowser.open("http://localhost:5000")
    set_status("管理画面をブラウザで開きました")


def set_status(msg):
    status_var.set(msg)


# ── ウィンドウ設定 ──────────────────────────────────────────
root = tk.Tk()
root.title("KPOPwave ランチャー")
root.resizable(False, False)

BG        = "#1a1a2e"
PANEL     = "#16213e"
ACCENT    = "#e94560"
FG        = "#ffffff"
STATUS_FG = "#a0a0c0"

root.configure(bg=BG)

# ── ヘッダー ────────────────────────────────────────────────
header = tk.Frame(root, bg=ACCENT, pady=8)
header.pack(fill="x")
tk.Label(
    header, text="🌊  KPOPwave Tool",
    font=("Segoe UI", 18, "bold"),
    bg=ACCENT, fg=FG,
).pack()

# ── ボタン定義 ──────────────────────────────────────────────
BUTTONS = [
    ("① ツール起動",            "▶  VS Code ターミナルで run.py を起動",  run_tool,   "#0f3460"),
    ("② Claude Code 起動",     "🤖  VS Code ターミナルで claude を起動",  run_claude, "#0f3460"),
    ("③ GitHub に保存",         "⬆  add → commit → push",               git_push,   "#1a472a"),
    ("④ GitHub から最新版取得",  "⬇  git pull",                           git_pull,   "#1a472a"),
    ("⑤ 管理画面を開く",        "🌐  localhost:5000",                     open_admin, "#2d1b69"),
]

frame = tk.Frame(root, bg=BG, padx=20, pady=15)
frame.pack(fill="both")

for title, subtitle, cmd, color in BUTTONS:
    btn_frame = tk.Frame(frame, bg=color)
    btn_frame.pack(fill="x", pady=5)

    btn = tk.Button(
        btn_frame,
        text=f"  {title}\n  {subtitle}  ",
        font=("Segoe UI", 11),
        anchor="w",
        justify="left",
        bg=color,
        fg=FG,
        activebackground="#3a3a5c",
        activeforeground=FG,
        relief="flat",
        bd=0,
        padx=16,
        pady=10,
        cursor="hand2",
        command=cmd,
    )
    btn.pack(fill="x")

    def _on_enter(e, b=btn, c=color): b.configure(bg="#3a3a5c")
    def _on_leave(e, b=btn, c=color): b.configure(bg=c)
    btn.bind("<Enter>", _on_enter)
    btn.bind("<Leave>", _on_leave)

# ── ステータスバー ──────────────────────────────────────────
status_var = tk.StringVar(value="準備完了")
tk.Label(
    root,
    textvariable=status_var,
    font=("Segoe UI", 9),
    bg=PANEL, fg=STATUS_FG,
    anchor="w", padx=10, pady=4,
).pack(fill="x", side="bottom")

root.mainloop()
