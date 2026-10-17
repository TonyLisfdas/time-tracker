#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地时间追踪器 —— 原生桌面报告窗口（不依赖浏览器）

用 Python 标准库 tkinter 渲染日报，替代原先「生成 HTML → 浏览器打开」
和「本地 HTTP 服务 → 浏览器刷新」两条路：
- 不起服务、不占端口、不写 HTML 文件；
- 数据直接经 time_tracker.day_report_data() 从 SQLite 只读获取，
  刷新不触发 summarize 写事务，不与后台监控抢锁；
- 原 HTML 报告（html / serve 命令）保留为可选导出，未删除。

用法：
    py time_tracker.py gui [--day YYYY-MM-DD]
    py time_tracker_gui.py [--day YYYY-MM-DD]
"""
import sys
from datetime import date, timedelta

import time_tracker as tt

AUTO_REFRESH_MS = 30_000


def _parse_day_or_today(text):
    """YYYY-MM-DD 文本 → 规范日期字符串，非法返回 None。"""
    try:
        return date.fromisoformat((text or "").strip()).isoformat()
    except ValueError:
        return None


def _shift_day(day, delta):
    return (date.fromisoformat(day) + timedelta(days=delta)).isoformat()


class TimeTrackerApp:
    """日报主窗口：概览（饼图+时间线）/ 明细 / 近7天趋势 / 细分。"""

    def __init__(self, root, day=None):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.current_day = _parse_day_or_today(day) or date.today().isoformat()
        self.data = None
        self.cat_color = {}
        self._trend_rows = []  # (y0, y1, day) 趋势条点击跳转用

        root.title("⏱ 时间追踪日报（原生窗口）")
        root.geometry("1080x720")
        root.minsize(900, 600)
        try:
            root.option_add("*Font", "Microsoft\\ YaHei 10")
        except Exception:
            pass

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("KPI.TLabel", font=("Microsoft YaHei", 15, "bold"),
                        foreground="#33415c")
        style.configure("KPICap.TLabel", font=("Microsoft YaHei", 9),
                        foreground="#8a93a3")
        style.configure("Title.TLabel", font=("Microsoft YaHei", 14, "bold"))

        self._build_header()
        self._build_kpis()
        self._build_notebook()
        self._build_footer()

        root.bind("<Left>", lambda e: self._goto(_shift_day(self.current_day, -1)))
        root.bind("<Right>", lambda e: self._goto(_shift_day(self.current_day, 1)))
        self.refresh()
        self._schedule_auto_refresh()

    # ---------- 布局 ----------

    def _build_header(self):
        tk, ttk = self.tk, self.ttk
        bar = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="时间追踪日报", style="Title.TLabel").pack(side="left")
        ttk.Button(bar, text="← 前一天",
                   command=lambda: self._goto(_shift_day(self.current_day, -1))
                   ).pack(side="left", padx=(18, 4))
        self.day_var = tk.StringVar(value=self.current_day)
        entry = ttk.Entry(bar, textvariable=self.day_var, width=12,
                          justify="center")
        entry.pack(side="left", padx=2)
        entry.bind("<Return>", lambda e: self._goto(self.day_var.get()))
        ttk.Button(bar, text="跳转", command=lambda: self._goto(self.day_var.get())
                   ).pack(side="left", padx=2)
        ttk.Button(bar, text="后一天 →",
                   command=lambda: self._goto(_shift_day(self.current_day, 1))
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="回到今天",
                   command=lambda: self._goto(date.today().isoformat())
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="刷新", command=self.refresh).pack(side="right")
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="30秒自动刷新", variable=self.auto_var
                        ).pack(side="right", padx=8)
        ttk.Label(bar, text="（←/→ 方向键可切天）",
                  style="KPICap.TLabel").pack(side="right")

    def _build_kpis(self):
        ttk = self.ttk
        row = ttk.Frame(self.root, padding=(12, 4))
        row.pack(fill="x")
        self.kpi_vars = {}
        for key, cap in (("total", "总记录时长"), ("count", "事件切换"),
                         ("avg", "平均单事件时长"), ("cats", "涉及类别")):
            box = ttk.LabelFrame(row, text=cap, padding=(10, 4))
            box.pack(side="left", fill="x", expand=True, padx=4)
            var = self.tk.StringVar(value="—")
            ttk.Label(box, textvariable=var, style="KPI.TLabel").pack()
            self.kpi_vars[key] = var

    def _build_notebook(self):
        tk, ttk = self.tk, self.ttk
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=12, pady=6)
        self.nb = nb

        # --- 概览页：饼图 + 图例 + 时间线 + 建议
        ov = ttk.Frame(nb, padding=10)
        nb.add(ov, text=" 概览 ")
        top = ttk.Frame(ov)
        top.pack(fill="x")
        self.pie = tk.Canvas(top, width=250, height=250, highlightthickness=0)
        self.pie.pack(side="left", padx=(6, 18))
        self.legend = tk.Canvas(top, height=250, highlightthickness=0)
        self.legend.pack(side="left", fill="both", expand=True)
        ttk.Label(ov, text="🕐 24 小时时间线").pack(anchor="w", pady=(12, 2))
        self.timeline = tk.Canvas(ov, height=56, highlightthickness=0,
                                  bg="#eef1f6")
        self.timeline.pack(fill="x")
        self.timeline.bind("<Configure>", lambda e: self._draw_timeline())
        self.tip_var = tk.StringVar(value="")
        ttk.Label(ov, textvariable=self.tip_var, wraplength=980,
                  foreground="#8a6116").pack(anchor="w", pady=(10, 0))

        # --- 明细页
        dt = ttk.Frame(nb, padding=6)
        nb.add(dt, text=" 事件明细 ")
        cols = ("time", "category", "sub", "name", "duration")
        self.tree = ttk.Treeview(dt, columns=cols, show="headings")
        for col, text, w in (("time", "时间", 150), ("category", "类别", 120),
                             ("sub", "细分", 120), ("name", "事件", 520),
                             ("duration", "时长", 110)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, stretch=(col == "name"))
        vs = ttk.Scrollbar(dt, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        # --- 趋势页
        tr = ttk.Frame(nb, padding=10)
        nb.add(tr, text=" 近 7 天趋势 ")
        ttk.Label(tr, text="条长 = 当天总量，颜色 = 类别构成；点击某一天可跳转到那天",
                  style="KPICap.TLabel").pack(anchor="w")
        self.trend = tk.Canvas(tr, height=290, highlightthickness=0)
        self.trend.pack(fill="x", pady=6)
        self.trend.bind("<Configure>", lambda e: self._draw_trend())
        self.trend.bind("<Button-1>", self._on_trend_click)
        self.trend_table = ttk.Treeview(
            tr, columns=("day", "total", "top"), show="headings", height=7)
        for col, text, w in (("day", "日期", 160), ("total", "总时长", 180),
                             ("top", "主力类别", 300)):
            self.trend_table.heading(col, text=text)
            self.trend_table.column(col, width=w)
        self.trend_table.pack(fill="x")

        # --- 细分页面
        sb = ttk.Frame(nb, padding=10)
        nb.add(sb, text=" 细分拆解 ")
        ttk.Label(sb, text="上网 / AI 等大类内部的细分归属（浏览器标题识别）",
                  style="KPICap.TLabel").pack(anchor="w")
        self.subs = tk.Canvas(sb, highlightthickness=0)
        self.subs.pack(fill="both", expand=True, pady=6)
        self.subs.bind("<Configure>", lambda e: self._draw_subs())

    def _build_footer(self):
        self.status_var = self.tk.StringVar(value="")
        self.ttk.Label(self.root, textvariable=self.status_var,
                       style="KPICap.TLabel",
                       padding=(12, 0, 12, 8)).pack(fill="x")

    # ---------- 数据与刷新 ----------

    def _goto(self, day_text):
        day = _parse_day_or_today(str(day_text))
        if day is None:
            self.status_var.set(f"⚠️ 日期格式应为 YYYY-MM-DD，收到：{day_text}")
            self.day_var.set(self.current_day)
            return
        self.current_day = day
        self.refresh()

    def refresh(self):
        try:
            self.data = tt.day_report_data(self.current_day)
        except Exception as e:
            self.status_var.set(f"⚠️ 读取数据失败：{e}")
            return
        d = self.data
        self.day_var.set(self.current_day)
        self.root.title(f"⏱ 时间追踪日报 · {self.current_day}")

        # 类别配色：当天顺序定色，趋势里多出的类别顺次补色
        self.cat_color = {cat: tt.PALETTE[i % len(tt.PALETTE)]
                          for i, (cat, _) in enumerate(d["sorted_cats"])}
        extra = sorted({c for t in d["trend"] for c in t["cats"]}
                       - set(self.cat_color))
        for i, c in enumerate(extra):
            self.cat_color[c] = tt.PALETTE[(len(d["sorted_cats"]) + i)
                                           % len(tt.PALETTE)]

        self.kpi_vars["total"].set(tt.format_duration(d["total"]))
        self.kpi_vars["count"].set(f"{len(d['items'])} 次")
        self.kpi_vars["avg"].set(tt.format_duration(int(d["avg"])))
        self.kpi_vars["cats"].set(f"{len(d['sorted_cats'])} 个")

        self._draw_pie()
        self._draw_legend()
        self._draw_timeline()
        self._draw_trend()
        self._draw_subs()
        self._fill_tree()
        self._fill_trend_table()
        self._update_tips()

        from datetime import datetime as _dt
        note = "（原始明细已归档，仅汇总）" if d["is_summarized"] else ""
        self.status_var.set(
            f"{self.current_day} · 更新于 {_dt.now().strftime('%H:%M:%S')} {note}"
            " · 数据来自本地 SQLite 只读查询，不经过浏览器/网络")

    def _schedule_auto_refresh(self):
        if self.auto_var.get():
            self.refresh()
        self.root.after(AUTO_REFRESH_MS, self._schedule_auto_refresh)

    # ---------- 绘制 ----------

    def _draw_pie(self):
        c = self.pie
        c.delete("all")
        d = self.data
        if not d or d["total"] <= 0:
            c.create_oval(25, 25, 225, 225, fill="#eef1f6", outline="#dfe5ee")
            c.create_text(125, 125, text="暂无数据", fill="#8a93a3")
            return
        start = 90.0
        for cat, dur in d["sorted_cats"]:
            extent = dur / d["total"] * 360.0
            # tkinter arc 的 extent 为逆时针方向，用负值顺时针铺开
            c.create_arc(15, 15, 235, 235, start=start, extent=-extent,
                         fill=self.cat_color[cat], outline="white", width=2)
            start -= extent
        c.create_text(125, 110, text=tt.format_duration(d["total"]),
                      font=("Microsoft YaHei", 12, "bold"), fill="#33415c")
        c.create_text(125, 134, text="总时长", fill="#5b6472",
                      font=("Microsoft YaHei", 9))

    def _draw_legend(self):
        c = self.legend
        c.delete("all")
        d = self.data
        if not d:
            return
        y = 8
        for cat, dur in d["sorted_cats"]:
            pct = dur / d["total"] * 100 if d["total"] else 0
            c.create_rectangle(4, y + 2, 16, y + 14, fill=self.cat_color[cat],
                               outline="")
            c.create_text(24, y + 8, anchor="w", text=cat,
                          font=("Microsoft YaHei", 10))
            c.create_text(190, y + 8, anchor="w",
                          text=f"{tt.format_duration(dur)}（{pct:.1f}%）",
                          font=("Microsoft YaHei", 10), fill="#5b6472")
            y += 26
        if not d["sorted_cats"]:
            c.create_text(4, 12, anchor="w", text="这一天还没有记录。",
                          fill="#8a93a3")

    def _draw_timeline(self):
        c = self.timeline
        if not c.winfo_exists():
            return
        c.delete("all")
        d = self.data
        if not d:
            return
        w = max(c.winfo_width(), 200)
        h = 34
        c.create_rectangle(0, 4, w, 4 + h, fill="#eef1f6", outline="")
        from datetime import datetime as _dt
        day_start = _dt.fromisoformat(self.current_day + "T00:00:00")
        now = _dt.now()
        for it in d["items"]:
            try:
                s = _dt.fromisoformat(it["start"])
                e = _dt.fromisoformat(it["end"]) if it["end"] else now
            except ValueError:
                continue
            s_off = max(0.0, (s - day_start).total_seconds())
            e_off = min(86400.0, (e - day_start).total_seconds())
            if e_off <= s_off:
                continue
            x0 = s_off / 86400 * w
            x1 = max(e_off / 86400 * w, x0 + 2)
            c.create_rectangle(x0, 4, x1, 4 + h,
                               fill=self.cat_color.get(it["category"], "#8B9BB4"),
                               outline="")
        for hh in (0, 6, 12, 18, 24):
            x = hh / 24 * w
            if hh == 0:
                c.create_text(2, 46, anchor="w", text="00:00",
                              fill="#9aa3b2", font=("Microsoft YaHei", 8))
            elif hh == 24:
                c.create_text(w - 2, 46, anchor="e", text="24:00",
                              fill="#9aa3b2", font=("Microsoft YaHei", 8))
            else:
                c.create_text(x, 46, text=f"{hh:02d}:00", fill="#9aa3b2",
                              font=("Microsoft YaHei", 8))

    def _draw_trend(self):
        c = self.trend
        if not c.winfo_exists():
            return
        c.delete("all")
        self._trend_rows = []
        d = self.data
        if not d:
            return
        w = max(c.winfo_width(), 300)
        max_t = max((t["total"] for t in d["trend"]), default=0) or 1
        label_w, val_w = 64, 170
        bar_w_max = max(w - label_w - val_w, 60)
        y = 6
        for t in d["trend"]:
            tot = t["total"]
            bold = "bold" if t["day"] == self.current_day else "normal"
            c.create_text(2, y + 11, anchor="w", text=t["day"][5:],
                          font=("Microsoft YaHei", 9, bold),
                          fill="#33415c" if bold == "bold" else "#8a93a3")
            x = label_w
            full = tot / max_t * bar_w_max
            cats = sorted(t["cats"].items(), key=lambda kv: -kv[1])
            for cat, sec in cats:
                if sec <= 0 or tot <= 0:
                    continue
                seg = sec / tot * full
                c.create_rectangle(x, y + 3, x + seg, y + 19,
                                   fill=self.cat_color.get(cat, "#8B9BB4"),
                                   outline="")
                x += seg
            top = cats[0][0] if cats else ""
            label = f"{tt.format_duration(tot)} · {top}" if top \
                else tt.format_duration(tot)
            c.create_text(label_w + bar_w_max + 8, y + 11, anchor="w",
                          text=label,
                          font=("Microsoft YaHei", 9, bold),
                          fill="#33415c" if bold == "bold" else "#5b6472")
            self._trend_rows.append((y, y + 22, t["day"]))
            y += 36

    def _on_trend_click(self, event):
        for y0, y1, day in self._trend_rows:
            if y0 <= event.y <= y1:
                self._goto(day)
                return

    def _draw_subs(self):
        c = self.subs
        if not c.winfo_exists():
            return
        c.delete("all")
        d = self.data
        if not d or not d["sorted_subs"]:
            c.create_text(6, 14, anchor="w",
                          text="暂无细分数据（细分来自浏览器窗口标题识别）。",
                          fill="#8a93a3")
            return
        w = max(c.winfo_width(), 300)
        groups = {}
        for key, dur in d["sorted_subs"]:
            cat, _, sub = key.partition(" › ")
            groups.setdefault(cat, []).append((sub, dur))
        y = 8
        for cat, rows in groups.items():
            cat_total = sum(v for _, v in rows)
            c.create_text(4, y + 8, anchor="w",
                          text=f"{cat}（{tt.format_duration(cat_total)}）",
                          font=("Microsoft YaHei", 10, "bold"), fill="#33415c")
            y += 24
            for sub, dur in rows:
                bar_max = max(w - 340, 60)
                bw = dur / cat_total * bar_max if cat_total else 0
                c.create_text(16, y + 7, anchor="w", text=sub,
                              font=("Microsoft YaHei", 9))
                c.create_rectangle(140, y + 3, 140 + bar_max, y + 12,
                                   fill="#eef1f6", outline="")
                c.create_rectangle(140, y + 3, 140 + bw, y + 12,
                                   fill=self.cat_color.get(cat, "#8B9BB4"),
                                   outline="")
                pct = dur / d["total"] * 100 if d["total"] else 0
                c.create_text(148 + bar_max, y + 7, anchor="w",
                              text=f"{tt.format_duration(dur)} · {pct:.1f}%",
                              font=("Microsoft YaHei", 9), fill="#5b6472")
                y += 20
            y += 8

    # ---------- 表格与建议 ----------

    def _fill_tree(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        d = self.data
        for it in reversed(d["items"]):
            t = f"{it['start'][11:16]} - {it['end'][11:16] if it['end'] else '进行中'}"
            self.tree.insert("", "end", values=(
                t, it["category"], it.get("sub") or "", it["name"],
                tt.format_duration(it["duration"])))

    def _fill_trend_table(self):
        for row in self.trend_table.get_children():
            self.trend_table.delete(row)
        for t in self.data["trend"]:
            cats = sorted(t["cats"].items(), key=lambda kv: -kv[1])
            self.trend_table.insert("", "end", values=(
                t["day"], tt.format_duration(t["total"]),
                cats[0][0] if cats else "—"))

    def _update_tips(self):
        d = self.data
        tips = []
        if d["total"] > 0:
            if d["avg"] < 600:
                tips.append("平均单事件时长不足 10 分钟，碎片化较严重，可尝试合并同类任务。")
            if d["sorted_cats"] and d["sorted_cats"][0][1] / d["total"] > 0.6:
                tips.append(f"{d['sorted_cats'][0][0]} 占比超过 60%，注意劳逸结合。")
            if len(d["items"]) > 15:
                tips.append("今天切换事件较频繁，可能影响专注度，试试番茄工作法。")
            if d["longest"]:
                tips.append(f"最长专注：{d['longest']['name'][:40]}"
                            f"（{tt.format_duration(d['longest']['duration'])}）")
        self.tip_var.set("建议：" + " ".join(tips) if tips else "暂无建议。")


def launch_gui(day=None):
    """打开原生报告窗口。无图形环境时给出明确提示而不是裸崩。"""
    try:
        import tkinter as tk
    except Exception as e:
        print(f"⚠️ 当前 Python 没有可用的 tkinter（{e}），"
              "可用 today / report 命令在终端查看。")
        return
    try:
        root = tk.Tk()
    except Exception as e:  # 无显示器 / 无图形会话
        print(f"⚠️ 无法创建窗口（{e}）。请在有桌面的会话里运行，"
              "或用 today / report 命令在终端查看。")
        return
    TimeTrackerApp(root, day=day)
    root.mainloop()


if __name__ == "__main__":
    _day = None
    if "--day" in sys.argv:
        i = sys.argv.index("--day")
        if i + 1 < len(sys.argv):
            _day = sys.argv[i + 1]
    elif len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        _day = sys.argv[1]
    launch_gui(_day)
