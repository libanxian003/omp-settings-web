# -*- coding: utf-8 -*-
"""omp 额度桌面小窗：置顶、无边框、暗色，数据来自 `omp usage --json`。

用 pythonw 运行（无控制台）。拖动任意位置移动，位置自动记住；
右键菜单：立即刷新 / 强制刷新 / 打开网页面板 / 退出。纯标准库。
"""
import ctypes
import json
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path

HERE = Path(__file__).parent
STATE = HERE / "widget-state.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
REFRESH_MS = 60_000
TICK_MS = 30_000
WEB_URL = "http://127.0.0.1:8788"

BG = "#16181d"
EDGE = "#2a2e37"
FG = "#d7dae0"
DIM = "#7d8590"
ACCENT = "#9fc3ff"
TRACK = "#262a33"
STATUS_COLOR = {"ok": "#3fb950", "warning": "#d29922", "exhausted": "#f85149"}
UNKNOWN_COLOR = "#58a6ff"
FONT = "Microsoft YaHei UI"
NUM_FONT = "Segoe UI"
WIN_TAG = {"weekly": "7d", "daily": "1d", "monthly": "月"}


def single_instance():
    """命名互斥量防双开（计划任务与手动启动撞车时后者直接退出）。"""
    if sys.platform != "win32":
        return True
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW(None, False, "omp-quota-widget")
    return k32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def run_omp(args, timeout):
    return subprocess.run(["omp", *args, "--no-extensions"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=NO_WINDOW)


def fetch_usage(invalidate):
    try:
        if invalidate:
            run_omp(["usage", "invalidate"], 60)
        p = run_omp(["usage", "--json"], 120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"omp 调用失败：{e}"
    try:
        return json.loads(p.stdout), None
    except json.JSONDecodeError:
        err = (p.stderr or "").strip().splitlines()
        return None, err[0] if err else "omp usage 输出无法解析"


def win_tag(w):
    wid = w.get("id") or ""
    if wid:
        return WIN_TAG.get(wid, wid)
    ms = w.get("durationMs")
    if ms:
        h = ms / 3_600_000
        return f"{h:g}h" if h < 24 else f"{h / 24:g}d"
    return ""


def provider_rows(rep):
    """同一共享窗口（sharedGroup）只取一次；同名限额重复出现时加窗口标签区分。"""
    limits, seen = [], set()
    for lim in rep.get("limits") or []:
        w = lim.get("window") or {}
        key = f"{(lim.get('scope') or {}).get('sharedGroup') or lim.get('id') or ''}|{w.get('id') or ''}"
        if key not in seen:
            seen.add(key)
            limits.append(lim)
    labels = [lim.get("label") or "" for lim in limits]
    repeated = len(set(labels)) < len(labels)
    rows = []
    for lim in limits:
        w, a = lim.get("window") or {}, lim.get("amount") or {}
        frac = a.get("usedFraction")
        if frac is None and a.get("limit"):
            frac = (a.get("used") or 0) / a["limit"]
        tag = win_tag(w)
        label = (lim.get("label") or "").replace(" (shared)", "")
        rows.append({
            "name": f"{label} {tag}".strip() if repeated else (tag or label),
            "frac": frac,
            "status": lim.get("status") or "unknown",
            "resets": w.get("resetsAt"),
        })
    return rows


def time_left(ts):
    if not ts:
        return ""
    s = ts / 1000 - time.time()
    if s <= 60:
        return "即将重置"
    d, m = divmod(int(s // 60), 1440)
    h, m = divmod(m, 60)
    if d:
        return f"{d}天{h}时"
    return f"{h}时{m}分" if h else f"{m}分"


def virtual_screen(root):
    try:
        u = ctypes.windll.user32
        x, y = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
        return x, y, x + u.GetSystemMetrics(78), y + u.GetSystemMetrics(79)
    except (AttributeError, OSError):
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


class Widget:
    def __init__(self):
        self.root = root = tk.Tk()
        root.title("额度")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.95)
        root.configure(bg=EDGE)
        self.k = root.winfo_fpixels("1i") / 96  # 像素尺寸随 DPI 缩放

        card = tk.Frame(root, bg=BG, padx=self.px(12), pady=self.px(10))
        card.pack(padx=1, pady=1)
        head = tk.Frame(card, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="额度", font=(FONT, 10, "bold"), fg=FG, bg=BG).pack(side="left")
        close = tk.Label(head, text="×", font=(NUM_FONT, 11), fg=DIM, bg=BG, cursor="hand2")
        close.pack(side="right", padx=(self.px(6), 0))
        close.bind("<Button-1>", lambda e: self.quit())
        again = tk.Label(head, text="⟳", font=(NUM_FONT, 10), fg=DIM, bg=BG, cursor="hand2")
        again.pack(side="right", padx=(self.px(6), 0))
        again.bind("<Button-1>", lambda e: self.refresh())
        self.stamp = tk.Label(head, text="", font=(FONT, 8), fg=DIM, bg=BG, width=8, anchor="e")
        self.stamp.pack(side="right")
        self.body = tk.Frame(card, bg=BG)
        self.body.pack(fill="x", pady=(self.px(4), 0))

        self.menu = tk.Menu(root, tearoff=0, bg="#1f232b", fg=FG, activebackground="#2d6cdf",
                            activeforeground="#fff", bd=0, font=(FONT, 9))
        self.menu.add_command(label="立即刷新", command=self.refresh)
        self.menu.add_command(label="强制刷新（重新抓取）", command=lambda: self.refresh(True))
        self.menu.add_command(label="打开网页面板", command=lambda: webbrowser.open(WEB_URL))
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.quit)

        # 子控件的 bindtags 含所属顶层窗口，绑在 root 上即可全窗拖动
        root.bind("<ButtonPress-1>", self.drag_start)
        root.bind("<B1-Motion>", self.drag_move)
        root.bind("<ButtonRelease-1>", lambda e: self.save_state())
        root.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        self.data, self.err, self.fetched = None, None, None
        self.loading, self.placed = False, False
        self.result: tuple | None = None  # 工作线程写入 (data, err)，主线程 collect() 取走
        root.after(50, self.round_corners)
        self.render()
        self.auto_refresh()
        root.after(TICK_MS, self.tick)

    def px(self, n):
        return int(round(n * self.k))

    # ---------- 数据 ----------
    def auto_refresh(self):
        self.refresh()
        self.root.after(REFRESH_MS, self.auto_refresh)

    def refresh(self, invalidate=False):
        if self.loading:
            return
        self.loading = True
        self.stamp.config(text="刷新中…")

        def work():
            self.result = fetch_usage(invalidate)

        threading.Thread(target=work, daemon=True).start()
        self.root.after(300, self.collect)

    def collect(self):
        if self.result is None:
            self.root.after(300, self.collect)
            return
        (data, err), self.result = self.result, None
        self.loading = False
        if data is not None:
            self.data, self.err, self.fetched = data, None, time.time()
        else:
            self.err = err
        self.render()

    def tick(self):
        if not self.loading:
            self.render()  # 只为更新重置倒计时
        self.root.after(TICK_MS, self.tick)

    # ---------- 绘制 ----------
    def render(self):
        """结构（供应商/行/角标/错误）不变时原地改文字和进度条，避免整窗重建抖动。"""
        self.stamp.config(text=time.strftime("%H:%M", time.localtime(self.fetched)) if self.fetched else "")
        reports = [(r, provider_rows(r)) for r in (self.data or {}).get("reports") or []]
        reports = [(r, rows) for r, rows in reports if rows]
        sig = (tuple((r.get("provider"), (r.get("metadata") or {}).get("planType"),
                      (r.get("resetCredits") or {}).get("availableCount"),
                      tuple(x["name"] for x in rows)) for r, rows in reports),
               self.err, self.data is None)
        if sig == getattr(self, "sig", None):
            for cells, r in zip(self.cells, (x for _, rows in reports for x in rows)):
                self.fill_row(cells, r)
            return
        self.sig, self.cells = sig, []
        for w in self.body.winfo_children():
            w.destroy()
        if not reports:
            msg = self.err or ("加载中…" if self.data is None else "没有可显示的额度数据")
            tk.Label(self.body, text=msg, font=(FONT, 9), fg=DIM, bg=BG,
                     wraplength=self.px(240), justify="left").grid(row=0, column=0, sticky="w")
        line = 0
        for i, (rep, rows) in enumerate(reports):
            self.header(rep, line, top=self.px(8) if i else self.px(2))
            line += 1
            for r in rows:
                self.cells.append(self.limit_row(r, line))
                line += 1
        if self.err and reports:
            tk.Label(self.body, text=self.err, font=(FONT, 8), fg=STATUS_COLOR["exhausted"], bg=BG,
                     wraplength=self.px(260), justify="left").grid(
                row=line, column=0, columnspan=4, sticky="w", pady=(self.px(6), 0))
        self.root.update_idletasks()
        if not self.placed:
            self.place()

    def header(self, rep, line, top):
        f = tk.Frame(self.body, bg=BG)
        f.grid(row=line, column=0, columnspan=4, sticky="we", pady=(top, self.px(2)))
        tk.Label(f, text=rep.get("provider") or "?", font=(FONT, 9, "bold"),
                 fg=ACCENT, bg=BG).pack(side="left")
        plan = (rep.get("metadata") or {}).get("planType")
        if plan:
            tk.Label(f, text=plan, font=(FONT, 8), fg=DIM, bg=BG).pack(side="left", padx=(self.px(6), 0))
        n = (rep.get("resetCredits") or {}).get("availableCount")
        if n:
            tk.Label(f, text=f"券×{n}", font=(FONT, 8), fg="#e3b341", bg="#2b2616",
                     padx=self.px(5)).pack(side="right")

    def limit_row(self, r, line):
        tk.Label(self.body, text=r["name"], font=(FONT, 9), fg=FG, bg=BG).grid(
            row=line, column=0, sticky="w", padx=(self.px(8), self.px(10)))
        w, h = self.px(110), self.px(6)
        bar = tk.Canvas(self.body, width=w, height=h, bg=BG, highlightthickness=0)
        bar.grid(row=line, column=1)
        c = h / 2
        bar.create_line(c, c, w - c, c, width=h, capstyle="round", fill=TRACK)
        fill = bar.create_line(c, c, c, c, width=h, capstyle="round", state="hidden")
        pct = tk.Label(self.body, font=(NUM_FONT, 9), fg=FG, bg=BG, width=4, anchor="e")
        pct.grid(row=line, column=2, padx=(self.px(6), 0))
        left = tk.Label(self.body, font=(FONT, 8), fg=DIM, bg=BG, width=8, anchor="e")
        left.grid(row=line, column=3, sticky="e", padx=(self.px(8), 0))
        cells = (bar, fill, pct, left)
        self.fill_row(cells, r)
        return cells

    def fill_row(self, cells, r):
        bar, fill, pct, left = cells
        w, h = int(bar["width"]), int(bar["height"])
        c, frac = h / 2, r["frac"]
        if frac:
            end = c + (w - 2 * c) * min(1.0, max(0.0, frac))
            bar.coords(fill, c, c, max(end, c + 0.1), c)
            bar.itemconfig(fill, state="normal", fill=STATUS_COLOR.get(r["status"], UNKNOWN_COLOR))
        else:
            bar.itemconfig(fill, state="hidden")
        pct.config(text="—" if frac is None else f"{round(frac * 100)}%")
        left.config(text=time_left(r["resets"]))

    # ---------- 窗口 ----------
    def round_corners(self):
        """Win11 圆角（DWMWA_WINDOW_CORNER_PREFERENCE=33 → DWMWCP_ROUND）；旧系统静默跳过。"""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except (AttributeError, OSError):
            pass

    def place(self):
        self.placed = True
        x0, y0, x1, y1 = virtual_screen(self.root)
        width = self.root.winfo_reqwidth()
        try:
            st = json.loads(STATE.read_text(encoding="utf-8"))
            x, y = int(st["x"]), int(st["y"])
        except (OSError, ValueError, KeyError, TypeError):
            x, y = self.root.winfo_screenwidth() - width - self.px(24), self.px(80)
        x = min(max(x, x0), x1 - self.px(60))  # 显示器变动后别落在屏幕外
        y = min(max(y, y0), y1 - self.px(40))
        self.root.geometry(f"+{x}+{y}")

    def drag_start(self, e):
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        dx, dy = self.drag
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def save_state(self):
        try:
            STATE.write_text(json.dumps({"x": self.root.winfo_x(), "y": self.root.winfo_y()}),
                             encoding="utf-8")
        except OSError:
            pass

    def quit(self):
        self.save_state()
        self.root.destroy()


def main():
    if not single_instance():
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 高分屏不发虚
    except (AttributeError, OSError):
        pass
    Widget().root.mainloop()


if __name__ == "__main__":
    main()
