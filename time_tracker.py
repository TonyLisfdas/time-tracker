#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地时间追踪器（自动监控版）v2

核心：monitor 在后台全自动记录活动窗口，无需手动操作——
     开机自启、后台常驻、闲置检测、按程序自动分类。
     数据存入本地 SQLite，跨天自动保留，超期自动汇总。

查询/分析：today / report / html / chart / history
手动补充：  start / stop / status（可选，与自动记录共用一个库）
番茄钟：    pomodoro
守护管理：  daemon start|stop|status
开机自启：  autostart on|off
数据管理：  summarize（手动触发汇总）
"""
import html as html_mod
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

# 数据默认落在脚本同级的 data\ 目录：项目放哪个盘，记录就在哪个盘，
# 重装系统（一般只重装 C 盘）不会丢历史数据。
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
_DATA_DIR = os.path.join(_SCRIPT_DIR, "data")

# 可用环境变量覆盖（便于测试）
DB_PATH = os.environ.get("TIME_TRACKER_DB",
                         os.path.join(_DATA_DIR, "time_tracker.db"))
DB_DIR = os.path.dirname(os.path.abspath(DB_PATH))
PID_PATH = DB_PATH + ".monitor.pid"
STOP_FLAG = DB_PATH + ".monitor.stop"
SERVE_PID_PATH = DB_PATH + ".serve.pid"
SERVE_LOG_PATH = DB_PATH + ".serve.log"
LOG_PATH = DB_PATH + ".monitor.log"
AUTOSTART_NAME = "time_tracker_autostart.vbs"
RULES_PATH = os.environ.get("TIME_TRACKER_RULES",
                            os.path.join(_DATA_DIR, "time_tracker_rules.json"))
CONFIG_PATH = os.environ.get("TIME_TRACKER_CONFIG",
                             os.path.join(_DATA_DIR, "time_tracker_config.json"))

PALETTE = [
    "#4C8DFF", "#FF8A5C", "#5CD6A0", "#FFD75C", "#B07CFF",
    "#FF6B81", "#5CC8FF", "#C0D64F", "#FF9FF3", "#8B9BB4",
]

# ---------- Windows API（ctypes，零第三方依赖） ----------

_user32 = _kernel32 = None
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes
        _user32 = ctypes.windll.user32
        _kernel32 = ctypes.windll.kernel32
        _user32.GetForegroundWindow.restype = wintypes.HWND
        _user32.GetWindowTextLengthW.restype = ctypes.c_int
        _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        _kernel32.OpenProcess.restype = wintypes.HANDLE
    except Exception:
        _user32 = _kernel32 = None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

# 可执行程序 -> 类别 的默认规则（可被 ~/.time_tracker_rules.json 覆盖/补充）
DEFAULT_EXE_RULES = {
    "chrome.exe": "上网浏览", "msedge.exe": "上网浏览", "firefox.exe": "上网浏览",
    "brave.exe": "上网浏览", "opera.exe": "上网浏览", "quark.exe": "上网浏览",
    "vivaldi.exe": "上网浏览", "360se.exe": "上网浏览", "360chrome.exe": "上网浏览",
    "sogouexplorer.exe": "上网浏览", "ucbrowser.exe": "上网浏览",
    "winword.exe": "办公·文档", "wps.exe": "办公·文档", "notepad.exe": "办公·文档",
    "excel.exe": "办公·表格", "et.exe": "办公·表格",
    "powerpnt.exe": "办公·演示", "wpp.exe": "办公·演示",
    "outlook.exe": "办公·邮件", "foxmail.exe": "办公·邮件",
    "code.exe": "编程开发", "cursor.exe": "编程开发", "pycharm64.exe": "编程开发",
    "idea64.exe": "编程开发", "webstorm64.exe": "编程开发", "devenv.exe": "编程开发",
    "wt.exe": "编程开发", "cmd.exe": "编程开发", "powershell.exe": "编程开发", "pwsh.exe": "编程开发",
    "windowsterminal.exe": "编程开发", "alacritty.exe": "编程开发",
    "nvim.exe": "编程开发", "vim.exe": "编程开发", "gvim.exe": "编程开发",
    "sublime_text.exe": "编程开发", "typora.exe": "办公·文档",
    "wechat.exe": "沟通协作", "weixin.exe": "沟通协作", "qq.exe": "沟通协作",
    "tim.exe": "沟通协作", "dingtalk.exe": "沟通协作", "wxwork.exe": "沟通协作",
    "feishu.exe": "沟通协作", "lark.exe": "沟通协作",
    "telegram.exe": "沟通协作", "slack.exe": "沟通协作",
    "explorer.exe": "文件管理", "everything.exe": "文件管理",
    "qqmusic.exe": "娱乐影音", "cloudmusic.exe": "娱乐影音", "spotify.exe": "娱乐影音",
    "potplayermini64.exe": "娱乐影音", "potplayer64.exe": "娱乐影音", "vlc.exe": "娱乐影音",
    "steam.exe": "游戏", "wegame.exe": "游戏",
    "photoshop.exe": "设计创作", "illustrator.exe": "设计创作", "figma.exe": "设计创作",
    "obsidian.exe": "知识管理", "notion.exe": "知识管理",
}

# 标题关键词 -> 细分归属
# 支持负向关键词：以 "!" 开头的表示排除词
# 例如 (["copilot", "!github"], "AI·其他") 表示含 copilot 但不含 github 才匹配
DEFAULT_TITLE_RULES = [
    (["千问", "qwen", "tongyi", "通义"], "AI·千问"),
    (["chatgpt", "openai", "sora"], "AI·ChatGPT"),
    (["claude"], "AI·Claude"),
    (["gemini", "google ai", "notebooklm"], "AI·Gemini"),
    (["deepseek"], "AI·DeepSeek"),
    (["kimi", "moonshot"], "AI·Kimi"),
    (["豆包", "doubao"], "AI·豆包"),
    (["文心", "ernie", "文心一言"], "AI·文心一言"),
    (["智谱", "chatglm", "清言"], "AI·智谱清言"),
    (["讯飞星火", "spark", "iflytek"], "AI·讯飞星火"),
    (["元宝"], "AI·腾讯元宝"),
    (["grok", "x.ai"], "AI·Grok"),
    (["perplexity"], "AI·Perplexity"),
    (["lmarena", "chat arena", "lmsys", "copilot", "midjourney",
      "stable diffusion"], "AI·其他"),
    (["bilibili", "哔哩哔哩", "youtube", "抖音", "爱奇艺", "腾讯视频",
      "优酷", "芒果tv", "netflix", "tiktok"], "视频"),
    (["网易云音乐", "qq音乐", "spotify", "music"], "音乐"),
    (["微博", "小红书", "知乎", "豆瓣", "贴吧", "twitter", "x.com",
      "reddit", "instagram", "facebook"], "社交社区"),
    (["抖店", "妙手", "抖音电商", "电商罗盘", "千牛", "生意参谋",
      "卖家中心", "商家后台", "店铺"], "店铺运营"),
    (["淘宝", "京东", "拼多多", "天猫", "amazon", "1688", "咸鱼",
      "闲鱼", "唯品会", "etsy", "ebay"], "网购"),
    (["新浪新闻", "网易新闻", "腾讯新闻", "澎湃新闻", "人民日报",
      "新华社", "bbc news", "cnn"], "新闻资讯"),
    (["github", "gitee", "stack overflow", "csdn", "掘金", "segmentfault",
      "v2ex"], "技术社区"),
    (["百度", "bing", "google search", "搜索结果", "搜索页",
      "- 搜索"], "搜索"),
    (["gmail", "outlook", "邮箱", "mail"], "邮件"),
    (["在线文档", "docs.qq", "石墨", "飞书", "notion"], "在线文档"),
    (["游戏", "game", "steam", "4399", "wegame", "lol", "原神"], "游戏"),
]


def load_rules():
    exe_rules, title_rules = dict(DEFAULT_EXE_RULES), list(DEFAULT_TITLE_RULES)

    def _warn(msg):
        # 告警本身绝不能把程序搞崩（GBK 控制台下打印 emoji 会 UnicodeEncodeError）
        try:
            print(msg)
        except Exception:
            pass

    try:
        # utf-8-sig：兼容记事本 / PowerShell 另存为时附加的 BOM
        with open(RULES_PATH, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        exe_rules.update({k.lower(): v for k, v in cfg.get("exe", {}).items()})
        for kws, cat in cfg.get("title", []):
            title_rules.append((kws, cat))
    except FileNotFoundError:
        pass  # 规则文件不存在是正常的
    except Exception as e:
        _warn(f"⚠️ 规则文件加载失败（{e}），使用默认规则")
    return exe_rules, title_rules


# 子类 -> 主类别 提升映射（浏览器窗口识别到明确性质时，主类别跟着改）
SUB_TO_CAT = {
    "视频": "娱乐影音", "音乐": "娱乐影音", "社交社区": "社交媒体",
    "网购": "网购", "店铺运营": "店铺运营", "新闻资讯": "新闻阅读",
    "技术社区": "编程开发", "在线文档": "办公·文档", "邮件": "办公·邮件",
    "游戏": "游戏",
}


def _promote(sub):
    if sub and sub.startswith("AI·"):
        return "AI 工具"
    return SUB_TO_CAT.get(sub)


def _title_matches(title_lower, keywords):
    """检查标题是否匹配关键词列表，支持负向关键词（! 前缀）"""
    has_positive = False
    for kw in keywords:
        if kw.startswith("!"):
            if kw[1:].lower() in title_lower:
                return False  # 排除词命中，不匹配
        else:
            if kw.lower() in title_lower:
                has_positive = True
    return has_positive


def subcategory_of(title, exe_rules, title_rules):
    """根据标题规则返回细分归属（如 视频/网购/具体AI产品），无命中返回 None"""
    t = (title or "").lower()
    for kws, cat in title_rules:
        if _title_matches(t, kws):
            return cat
    return None


def classify(exe, title, exe_rules, title_rules):
    """返回 (主类别, 子类别)。子类别是更细的归属（视频/网购/具体AI产品等）。
    对浏览器和「其他」类别都尝试 title_rules 提升。"""
    sub = subcategory_of(title, exe_rules, title_rules)
    cat = exe_rules.get(exe.lower(), "其他")
    if sub:
        if cat == "上网浏览":
            promo = _promote(sub)
            if promo:
                cat = promo
        elif cat == "其他":
            promo = _promote(sub)
            if promo:
                cat = promo
    return cat, sub


def win_info():
    """返回 (窗口标题, 进程名)，失败返回 (None, None)"""
    if not _user32:
        return None, None
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return None, None
        n = _user32.GetWindowTextLengthW(hwnd) + 1
        tbuf = ctypes.create_unicode_buffer(n)
        _user32.GetWindowTextW(hwnd, tbuf, n)
        title = tbuf.value.strip()
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ""
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if handle:
            size = wintypes.DWORD(260)
            ebuf = ctypes.create_unicode_buffer(260)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, ebuf, ctypes.byref(size)):
                exe = os.path.basename(ebuf.value)
            _kernel32.CloseHandle(handle)
        return (title or None), exe
    except Exception:
        return None, None


class _LastInputInfo(ctypes.Structure if _user32 else object):
    if _user32:
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds():
    """距离上次键鼠操作的秒数"""
    if not _user32:
        return 0
    try:
        lii = _LastInputInfo()
        lii.cbSize = ctypes.sizeof(_LastInputInfo)
        if _user32.GetLastInputInfo(ctypes.byref(lii)):
            return ((_kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        pass
    return 0


def pid_alive(pid):
    if not _kernel32:
        return False
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    code = wintypes.DWORD()
    alive = bool(_kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                 and code.value == STILL_ACTIVE)
    _kernel32.CloseHandle(handle)
    return alive


# ---------- 数据库 ----------

_DB_SCHEMA_VERSION = 2  # 每次 schema 变更时递增


def get_conn():
    """获取数据库连接。启用 WAL 模式，schema 迁移仅在版本变化时执行。"""
    # 确保父目录存在
    os.makedirs(DB_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    # 检查并执行 schema 迁移
    try:
        cur_ver = conn.execute("PRAGMA user_version").fetchone()[0]
    except Exception:
        cur_ver = 0

    if cur_ver < 1:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT DEFAULT '未分类',
                start_time TEXT NOT NULL,
                end_time TEXT,
                duration_sec INTEGER DEFAULT 0,
                subcategory TEXT DEFAULT NULL
            )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
        if "subcategory" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN subcategory TEXT DEFAULT NULL")
        # 回填旧记录
        exe_rules, title_rules = load_rules()
        rows = conn.execute(
            "SELECT id, name, category FROM events WHERE subcategory IS NULL").fetchall()
        for rid, name, cat in rows:
            sub = subcategory_of(name, exe_rules, title_rules)
            new_cat = cat
            if sub and cat == "上网浏览":
                promo = _promote(sub)
                if promo:
                    new_cat = promo
            conn.execute("UPDATE events SET subcategory=?, category=? WHERE id=?",
                         (sub, new_cat, rid))
        conn.execute("PRAGMA user_version=1")

    if cur_ver < _DB_SCHEMA_VERSION:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_summaries (
                day TEXT NOT NULL,
                category TEXT NOT NULL,
                total_sec INTEGER NOT NULL DEFAULT 0,
                event_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, category)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS monthly_summaries (
                month TEXT NOT NULL,
                category TEXT NOT NULL,
                total_sec INTEGER NOT NULL DEFAULT 0,
                day_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (month, category)
            )
        """)
        conn.execute(f"PRAGMA user_version={_DB_SCHEMA_VERSION}")

    conn.commit()
    return conn


def reclassify():
    """用最新规则重新评估全部历史记录的子类与主类别（改规则后执行）"""
    conn = get_conn()
    exe_rules, title_rules = load_rules()
    rows = conn.execute("SELECT id, name, category FROM events").fetchall()
    for rid, name, cat in rows:
        sub = subcategory_of(name, exe_rules, title_rules)
        new_cat = cat
        if sub and cat == "上网浏览":
            promo = _promote(sub)
            if promo:
                new_cat = promo
        conn.execute("UPDATE events SET subcategory=?, category=? WHERE id=?",
                     (sub, new_cat, rid))
    conn.commit()
    print(f"✅ 已用最新规则重新评估 {len(rows)} 条记录")


def format_duration(sec):
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return f"{h}时{m:02d}分{s:02d}秒"
    elif m > 0:
        return f"{m}分{s:02d}秒"
    else:
        return f"{s}秒"


def _close_open(conn, now):
    """关闭所有未结束的事件记录，返回最后一条的 (name, duration)"""
    rows = conn.execute(
        "SELECT id, name, start_time FROM events WHERE end_time IS NULL ORDER BY start_time").fetchall()
    if not rows:
        return None
    last = None
    for eid, name, start_str in rows:
        try:
            start_dt = datetime.fromisoformat(start_str)
        except ValueError:
            continue
        duration = max(0, int((now - start_dt).total_seconds()))
        conn.execute("UPDATE events SET end_time=?, duration_sec=? WHERE id=?",
                     (now.isoformat(timespec="seconds"), duration, eid))
        last = (name, duration)
    if last:
        conn.commit()
    return last


def _open_event(conn, name, category, now, sub=None):
    conn.execute(
        "INSERT INTO events (name, category, start_time, subcategory) "
        "VALUES (?, ?, ?, ?)",
        (name, category, now.isoformat(timespec="seconds"), sub))
    conn.commit()


def fetch_day(conn, day):
    cur = conn.execute(
        """SELECT name, category, start_time, end_time, duration_sec, subcategory
           FROM events WHERE date(start_time) = ? ORDER BY start_time""", (day,))
    items = []
    for name, cat, st, et, dur, sub in cur.fetchall():
        if et is None:
            dur = max(0, int((datetime.now() - datetime.fromisoformat(st)).total_seconds()))
        items.append({"name": name, "category": cat, "start": st,
                      "end": et, "duration": max(0, dur), "sub": sub})
    return items


# ---------- 手动命令 ----------

def start_event(name, category="未分类"):
    conn = get_conn()
    now = datetime.now()
    closed = _close_open(conn, now)
    if closed:
        print(f"⏹ 自动结束上一个事件：{closed[0]}（{format_duration(closed[1])}）")
    _open_event(conn, name, category, now)
    print(f"✅ 开始记录：[{category}] {name} @ {now.strftime('%H:%M:%S')}")


def stop_event():
    conn = get_conn()
    closed = _close_open(conn, datetime.now())
    if not closed:
        print("️ 当前没有进行中的事件")
        return
    print(f"⏹ 结束记录：{closed[0]}，持续 {format_duration(closed[1])}")


def status():
    conn = get_conn()
    row = conn.execute(
        "SELECT name, category, start_time FROM events WHERE end_time IS NULL ORDER BY start_time LIMIT 1").fetchone()
    if not row:
        print("💤 当前没有正在记录的事件")
        return
    name, cat, start_str = row
    elapsed = max(0, int((datetime.now() - datetime.fromisoformat(start_str)).total_seconds()))
    print(f"🔴 进行中：[{cat}] {name}，已进行 {format_duration(elapsed)}")


def today_records(day=None):
    day = day or date.today().isoformat()
    items = fetch_day(get_conn(), day)
    if not items:
        print(f" {day} 还没有记录")
        return
    total = 0
    print(f"\n📅 当日记录 ({day})")
    print("=" * 60)
    for it in items:
        et_str = it["end"][11:16] if it["end"] else "进行中"
        total += it["duration"]
        sub_tip = f"·{it['sub']}" if it.get("sub") else ""
        print(f"  {it['start'][11:16]} - {et_str}  [{it['category']}{sub_tip}] "
              f"{it['name']}  ({format_duration(it['duration'])})")
    print("=" * 60)
    print(f"总记录时长：{format_duration(total)}")


def _build_analysis(items):
    total = sum(i["duration"] for i in items)
    cat_dur = {}
    for it in items:
        cat_dur[it["category"]] = cat_dur.get(it["category"], 0) + it["duration"]
    sub_dur = {}
    for it in items:
        if it.get("sub"):
            key = f"{it['category']} › {it['sub']}"
            sub_dur[key] = sub_dur.get(key, 0) + it["duration"]
    event_dur, event_cnt = {}, {}
    for it in items:
        key = f"[{it['category']}] {it['name']}"
        event_dur[key] = event_dur.get(key, 0) + it["duration"]
        event_cnt[key] = event_cnt.get(key, 0) + 1
    return {"total": total,
            "sorted_cats": sorted(cat_dur.items(), key=lambda x: -x[1]),
            "sorted_subs": sorted(sub_dur.items(), key=lambda x: -x[1]),
            "sorted_events": sorted(event_dur.items(), key=lambda x: -x[1]),
            "event_cnt": event_cnt,
            "longest": max(items, key=lambda x: x["duration"]),
            "avg": total / len(items) if items else 0}


def _print_tips(a, total):
    print("\n【小建议】")
    tips = 0
    if a["avg"] < 600:
        print("  ⚠️ 平均单事件时长不足10分钟，碎片化较严重。")
        tips += 1
    if a["sorted_cats"] and a["sorted_cats"][0][1] / total > 0.6:
        print(f"  ⚠️ {a['sorted_cats'][0][0]} 占比超过60%，注意劳逸结合。")
        tips += 1
    if tips == 0:
        print("  👍 节奏不错，继续保持！")


def report(day=None):
    day = day or date.today().isoformat()
    items = fetch_day(get_conn(), day)
    if not items:
        print(f"📭 {day} 没有数据可分析")
        return
    a = _build_analysis(items)
    total = a["total"]
    if total == 0:
        print("️ 总时长为0，无法分析")
        return

    print(f"\n📊 分析报告 ({day})")
    print("=" * 60)
    print(f"总记录时长：{format_duration(total)}\n")
    print("【用时占比（按类别）】")
    for cat, d in a["sorted_cats"]:
        pct = d / total * 100
        bar = "█" * max(1, int(pct / 2))
        print(f"  {cat:<10} {format_duration(d):>12}  {pct:5.1f}%  {bar}")
    if a["sorted_subs"]:
        print("\n【细分明细（上网/AI 等大类内部）】")
        for key, d in a["sorted_subs"]:
            print(f"  {key:<26} {format_duration(d):>12}  {d/total*100:5.1f}%")
    print("\n【事件详情】")
    for ev, d in a["sorted_events"]:
        pct = d / total * 100
        print(f"  {ev:<30} {format_duration(d):>12}  {pct:5.1f}%  ×{a['event_cnt'][ev]}")
    top_cat, top_cat_dur = a["sorted_cats"][0]
    top_ev, top_ev_dur = a["sorted_events"][0]
    print("\n【事件倾向分析】")
    print(f"   最耗时类别：{top_cat}（{format_duration(top_cat_dur)}，{top_cat_dur/total*100:.1f}%）")
    print(f"  ⏰ 最耗时事件：{top_ev}（{format_duration(top_ev_dur)}）")
    print(f"   最长专注：{a['longest']['name']}（{format_duration(a['longest']['duration'])}）")
    print(f"  🔄 事件切换次数：{len(items)} 次")
    print(f"  ⏱ 平均单事件时长：{format_duration(int(a['avg']))}")
    print(f"  📂 涉及类别数：{len(a['sorted_cats'])}")
    _print_tips(a, total)


# ---------- 数据汇总与清理 ----------

def load_config():
    """读 data\\time_tracker_config.json。数值 0 = 永久保留（只汇总，不删除）。"""
    cfg = {
        "events_retain_days": 0,    # 原始明细保留天数，0=永久
        "daily_retain_months": 0,   # 每日汇总保留月数，0=永久
        "aggregate_after_days": 1,  # 过完的整天即汇总（与保留期无关）
    }
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            user = json.load(f)
        for k in cfg:
            if k in user:
                cfg[k] = int(user[k])
    except FileNotFoundError:
        pass  # 没有配置文件 = 全部走默认（永久保留）
    except Exception as e:
        try:
            print(f"⚠️ 配置读取失败（{e}），按默认策略执行：明细永久保留")
        except Exception:
            pass
    return cfg


_CFG = load_config()
SUMMARY_RETAIN_DAYS = _CFG["events_retain_days"]      # 0 = 原始明细永不删除
SUMMARY_RETAIN_MONTHS = _CFG["daily_retain_months"]   # 0 = 每日汇总永不删除
AGGREGATE_AFTER_DAYS = max(1, _CFG["aggregate_after_days"])


def summarize(verbose=True):
    """逐级汇总：原始事件→每日汇总→每月汇总；仅在保留期 > 0 时才清理超期数据。

    保留期来自 data\\time_tracker_config.json，默认 0 = 永久保留，
    即每天明细一直留着，随时可按任意区间做周/月/年统计。
    """
    conn = get_conn()
    today = date.today()

    # 汇总口径：过完的整天都要汇总，与保留期无关（否则保留期=0 时永远不出汇总）
    agg_day_cutoff = (today - timedelta(days=AGGREGATE_AFTER_DAYS)).isoformat()
    # 每月汇总只处理已经过完的自然月，避免把当月不完整快照永久冻结
    last_month = today.replace(day=1) - timedelta(days=1)
    agg_month_cutoff = f"{last_month.year:04d}-{last_month.month:02d}"

    # 删除口径：0（或负数）表示永久保留，不删
    del_day_cutoff = ((today - timedelta(days=SUMMARY_RETAIN_DAYS)).isoformat()
                      if SUMMARY_RETAIN_DAYS > 0 else None)
    if SUMMARY_RETAIN_MONTHS > 0:
        ym = today.year * 12 + today.month - SUMMARY_RETAIN_MONTHS
        del_month_cutoff = f"{ym // 12:04d}-{ym % 12:02d}"
    else:
        del_month_cutoff = None

    # 1) 找出需要汇总的天（原始事件存在且未汇总）
    days_to_sum = conn.execute(
        "SELECT DISTINCT date(start_time) AS d FROM events "
        "WHERE date(start_time) <= ? "
        "AND date(start_time) NOT IN (SELECT DISTINCT day FROM daily_summaries)",
        (agg_day_cutoff,)).fetchall()
    days_to_sum = [r[0] for r in days_to_sum]

    daily_rows = 0
    for d in days_to_sum:
        rows = conn.execute(
            "SELECT category, SUM(duration_sec), COUNT(*) FROM events "
            "WHERE date(start_time) = ? GROUP BY category", (d,)).fetchall()
        for cat, total, cnt in rows:
            conn.execute(
                "INSERT OR REPLACE INTO daily_summaries "
                "(day, category, total_sec, event_count) VALUES (?,?,?,?)",
                (d, cat, total, cnt))
            daily_rows += 1

    # 2) 找出需要汇总的月（每日汇总存在且未汇总）
    months_to_sum = conn.execute(
        "SELECT DISTINCT substr(day,1,7) AS m FROM daily_summaries "
        "WHERE substr(day,1,7) <= ? "
        "AND substr(day,1,7) NOT IN (SELECT DISTINCT month FROM monthly_summaries)",
        (agg_month_cutoff,)).fetchall()
    months_to_sum = [r[0] for r in months_to_sum]

    monthly_rows = 0
    for m in months_to_sum:
        rows = conn.execute(
            "SELECT category, SUM(total_sec), COUNT(DISTINCT day) FROM daily_summaries "
            "WHERE substr(day,1,7) = ? GROUP BY category", (m,)).fetchall()
        for cat, total, dcnt in rows:
            conn.execute(
                "INSERT OR REPLACE INTO monthly_summaries "
                "(month, category, total_sec, day_count) VALUES (?,?,?,?)",
                (m, cat, total, dcnt))
            monthly_rows += 1

    # 3) 清理超期原始事件（仅当保留天数 > 0）
    deleted_events = 0
    if del_day_cutoff:
        deleted_events = conn.execute(
            "DELETE FROM events WHERE date(start_time) < ?",
            (del_day_cutoff,)).rowcount

    # 4) 清理超期每日汇总（仅当保留月数 > 0）
    deleted_daily = 0
    if del_month_cutoff:
        deleted_daily = conn.execute(
            "DELETE FROM daily_summaries WHERE day < ?",
            (del_month_cutoff + "-01",)).rowcount

    conn.commit()

    if verbose:
        print(f"📊 汇总完成：{len(days_to_sum)} 天→每日汇总（{daily_rows} 条），"
              f"{len(months_to_sum)} 月→每月汇总（{monthly_rows} 条）")
        if deleted_events or deleted_daily:
            print(f" 清理完成：删除 {deleted_events} 条原始事件，"
                  f"{deleted_daily} 条每日汇总")
        else:
            print(" 保留策略：永久保留，本次未删除任何数据")
    return {
        "days_summarized": len(days_to_sum),
        "months_summarized": len(months_to_sum),
        "events_deleted": deleted_events,
        "daily_deleted": deleted_daily,
    }


# ---------- 保留策略 ----------

_RETAIN_KEYS = {
    "events": "events_retain_days",      # 原始明细保留天数
    "daily": "daily_retain_months",      # 每日汇总保留月数
    "aggregate": "aggregate_after_days",  # 过完几天后汇总
}

_ROW_BYTES = 151   # 实测：含 B-tree 页开销的平均每行字节数（1k/1w 行对照一致）


def _retain_str(v, unit):
    return "永久保留（不删）" if v <= 0 else f"{v} {unit}"


def _parse_pairs(args):
    out = {}
    for a in args:
        if "=" not in a:
            print(f"⚠️ 跳过无法理解的参数：{a}（应为 键=数值）")
            continue
        k, _, v = a.partition("=")
        k = k.strip().lower()
        if k not in _RETAIN_KEYS:
            print(f"⚠️ 未知项 {k}=，可用键：{', '.join(_RETAIN_KEYS)}")
            continue
        try:
            out[_RETAIN_KEYS[k]] = int(v)
        except ValueError:
            print(f"⚠️ {k} 的值必须是整数（0=永久保留），收到：{v}")
    return out


def retention_show():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    dsum = conn.execute("SELECT COUNT(*) FROM daily_summaries").fetchone()[0]
    msum = conn.execute("SELECT COUNT(*) FROM monthly_summaries").fetchone()[0]
    span = conn.execute(
        "SELECT MIN(date(start_time)), MAX(date(start_time)) FROM events").fetchone()

    print("🗃  数据保留策略")
    print("=" * 60)
    print(f"  原始明细 events          : "
          f"{_retain_str(SUMMARY_RETAIN_DAYS, '天')}")
    print(f"  每日汇总 daily_summaries : "
          f"{_retain_str(SUMMARY_RETAIN_MONTHS, '个月')}")
    print(f"  过完 {AGGREGATE_AFTER_DAYS} 天后自动逐级汇总（明细→每日→每月）")
    print(f"  配置文件：{CONFIG_PATH}"
          + ("" if os.path.exists(CONFIG_PATH) else "  （尚未创建，当前走默认值）"))
    print()
    print(f"  库文件占用：主库 {os.path.getsize(DB_PATH)/1024:.0f} KB"
          + (f" + WAL {os.path.getsize(DB_PATH + '-wal')/1024:.0f} KB"
             if os.path.exists(DB_PATH + '-wal') else ""))
    if n:
        rec_hours = conn.execute(
            "SELECT SUM(duration_sec) FROM events WHERE end_time IS NOT NULL"
        ).fetchone()[0] / 3600.0
        rate = n / rec_hours if rec_hours > 0.2 else None
        head = f"  明细 {n} 条（{span[0]} ~ {span[1]}）"
        if rate:
            print(head + f"，实测 {rate:.0f} 条/记录小时")
            # 外推前提：每天活跃 12 小时（不是墙上 24 小时）
            per_day = rate * 12
            print(f"  按 {_ROW_BYTES} B/条、每天活跃 12 小时外推："
                  f"约 {per_day:.0f} 条/天 = {per_day*_ROW_BYTES/1024:.0f} KB/天"
                  f"，{per_day*_ROW_BYTES*30/1048576:.1f} MB/月"
                  f"，{per_day*_ROW_BYTES*365/1048576:.0f} MB/年")
        else:
            print(head)
            print("  （记录时长太短，暂不外推体积）")
    print(f"  每日汇总 {dsum} 条，每月汇总 {msum} 条")
    print()
    print("  改策略：  py time_tracker.py retention set events=0 daily=0")
    print("  立即清一次：py time_tracker.py retention clean events=90")
    print("  说明：0 = 永久保留。改完需重启监控才生效：")
    print("        py time_tracker.py daemon stop && py time_tracker.py daemon start")


def retention_set(pairs):
    if not pairs:
        print("⚠️ 至少要给一项，例如：retention set events=0 daily=12")
        return
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"⚠️ 现有配置无法解析（{e}），将重建")
            cfg = {}
    cfg["_说明"] = ("0 = 永久保留（只汇总不删除）。events: 原始明细保留天数; "
                   "daily: 每日汇总保留月数; aggregate: 过完几天后汇总。")
    cfg.update(pairs)
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"✅ 已写入 {CONFIG_PATH}")
    for k, v in pairs.items():
        print(f"   {k} = {v}")
    print("   ⚠️ 正在运行的监控仍持旧策略，需 daemon stop / daemon start 才生效。")


def retention_clean(pairs):
    """不改配置，按给定口径立即清理一次。"""
    eff = {k: v for k, v in pairs.items() if k in _RETAIN_KEYS.values()}
    if not eff:
        print("⚠️ 没有可执行的清理项，例如：retention clean events=90 daily=12")
        return
    # 监控在并发写库，用 SQLite 的在线备份 API 先存一份
    bak_path = DB_PATH + ".before-clean-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    src = get_conn()
    dst = sqlite3.connect(bak_path)
    with dst:
        src.backup(dst)
    dst.close()
    print(f"💾 已备份到 {bak_path}")

    g = globals()
    saved = {k: g[k] for k in ("SUMMARY_RETAIN_DAYS", "SUMMARY_RETAIN_MONTHS")}
    try:
        g["SUMMARY_RETAIN_DAYS"] = eff.get("events_retain_days", 0)
        g["SUMMARY_RETAIN_MONTHS"] = eff.get("daily_retain_months", 0)
        r = summarize(verbose=False)
    finally:
        g.update(saved)
    print(f"🧹 本次清理：删除原始事件 {r['events_deleted']} 条，"
          f"每日汇总 {r['daily_deleted']} 条；"
          f"顺带汇总了 {r['days_summarized']} 天 / {r['months_summarized']} 月")
    if not r["events_deleted"] and not r["daily_deleted"]:
        print("   没有超期数据，未删除任何内容。")
    print("   注意：这只是临时清理，配置里的保留期没有被改动。")


def retention_cmd(args):
    sub = args[0] if args else "show"
    if sub == "set":
        retention_set(_parse_pairs(args[1:]))
    elif sub == "clean":
        retention_clean(_parse_pairs(args[1:]))
    elif sub in ("show", "status"):
        retention_show()
    else:
        print(f"⚠️ 未知子命令：{sub}（可用：show / set / clean）")


# ---------- 跨天查询 ----------

def history(days=7):
    conn = get_conn()
    today = date.today()
    first = today - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT start_time, end_time, duration_sec, category
           FROM events WHERE date(start_time) >= ?""", (first.isoformat(),)).fetchall()

    day_total, day_cnt, day_cat = {}, {}, {}
    for st, et, dur, cat in rows:
        if et is None:
            dur = max(0, int((datetime.now() - datetime.fromisoformat(st)).total_seconds()))
        d = st[:10]
        day_total[d] = day_total.get(d, 0) + dur
        day_cnt[d] = day_cnt.get(d, 0) + 1
        day_cat.setdefault(d, {})
        day_cat[d][cat] = day_cat[d].get(cat, 0) + dur

    # 补充：从每日汇总读取已清理的超期数据
    sum_rows = conn.execute(
        """SELECT day, category, total_sec, event_count
           FROM daily_summaries WHERE day >= ? AND day NOT IN
           (SELECT DISTINCT date(start_time) FROM events WHERE date(start_time) >= ?)""",
        (first.isoformat(), first.isoformat())).fetchall()
    for d, cat, dur, cnt in sum_rows:
        day_total[d] = day_total.get(d, 0) + dur
        day_cnt[d] = day_cnt.get(d, 0) + cnt
        day_cat.setdefault(d, {})
        day_cat[d][cat] = day_cat[d].get(cat, 0) + dur

    print(f"\n📆 最近 {days} 天趋势（{first.isoformat()} ~ {today.isoformat()}）")
    print("=" * 70)
    max_total = max(day_total.values()) if day_total else 0
    for i in range(days):
        d = (first + timedelta(days=i)).isoformat()
        t = day_total.get(d, 0)
        bar = "█" * int(t / max_total * 36) if max_total and t > 0 else ""
        top = ""
        if day_cat.get(d):
            tc = max(day_cat[d].items(), key=lambda x: x[1])
            top = f"主力：{tc[0]}"
        mark = " ←今天" if d == today.isoformat() else ""
        print(f"  {d}  {format_duration(t):>12}  {day_cnt.get(d,0):>3}次  {bar} {top}{mark}")
    print("=" * 70)
    total_all = sum(day_total.values())
    active_days = len(day_total)
    print(f"区间合计：{format_duration(total_all)}｜有记录 {active_days}/{days} 天")
    if active_days:
        print(f"日均（按有记录天数）：{format_duration(total_all // active_days)}")
    cat_all = {}
    for d in day_cat:
        for c, v in day_cat[d].items():
            cat_all[c] = cat_all.get(c, 0) + v
    if cat_all and total_all > 0:
        print("\n【区间类别汇总】")
        for c, v in sorted(cat_all.items(), key=lambda x: -x[1]):
            print(f"  {c:<12} {format_duration(v):>12}  {v/total_all*100:5.1f}%")
    elif not cat_all:
        print("（区间内暂无记录）")


# ---------- HTML 报告 ----------

HTML_STYLE = """
 body{font-family:"Microsoft YaHei","Segoe UI",sans-serif;background:#f5f7fb;margin:0;padding:32px;color:#2b2f36}
 .card{background:#fff;border-radius:14px;box-shadow:0 2px 10px rgba(31,45,70,.08);padding:24px 28px;margin:0 auto 20px;max-width:880px}
 h1{font-size:22px;margin:0 0 4px}
 h2{font-size:16px;margin:0 0 14px}
 .sub{color:#8a93a3;font-size:13px}
 .kpis{display:flex;gap:16px;flex-wrap:wrap;margin-top:18px}
 .kpi{flex:1;min-width:150px;background:#f8faff;border-radius:10px;padding:14px 16px}
 .kpi .v{font-size:20px;font-weight:700;color:#33415c;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
 .kpi .k{font-size:12px;color:#8a93a3;margin-top:4px}
 .flex{display:flex;gap:32px;align-items:center;flex-wrap:wrap}
 .pie{width:200px;height:200px;border-radius:50%;background:conic-gradient(__GRADIENT__);flex:none;box-shadow:0 0 0 6px #fff,0 0 0 7px #eef1f6}
 .legend{font-size:13px}
 .legend .row{display:flex;align-items:center;gap:8px;margin:7px 0}
 .dot{width:12px;height:12px;border-radius:3px;flex:none}
 .timeline{position:relative;height:30px;background:#eef1f6;border-radius:8px;overflow:hidden;margin:14px 0 6px}
 .seg{position:absolute;top:0;bottom:0;opacity:.92}
 .hours{display:flex;justify-content:space-between;font-size:11px;color:#9aa3b2}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #eef1f6}
 th{color:#8a93a3;font-weight:600}
 .tag{display:inline-block;padding:2px 8px;border-radius:10px;color:#fff;font-size:12px}
 .tip{background:#fff8ec;border:1px solid #ffe2b0;color:#8a6116;border-radius:10px;padding:10px 14px;font-size:13px;margin-top:8px}
 .subgroup{margin:4px 0}
 .subgroup .gname{font-size:14px;font-weight:600;color:#33415c;margin:14px 0 6px}
 .subrow{display:flex;align-items:center;gap:10px;margin:6px 0;font-size:12px;color:#5b6472}
 .subbar{height:8px;border-radius:4px;background:#eef1f6;flex:1;overflow:hidden}
 .subbar i{display:block;height:100%;border-radius:4px}
 .subtag{color:#8a93a3;font-size:12px}
 .trend-bar{display:inline-block;height:10px;border-radius:3px;vertical-align:middle;margin-right:6px}
 .cross-day{color:#e74c3c;font-size:11px;margin-left:4px}
 .nav{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin-top:14px;padding-top:12px;border-top:1px solid #eef1f6;font-size:13px}
 .navlnk{color:#33415c;text-decoration:none;padding:4px 10px;border:1px solid #dfe5ee;border-radius:6px;background:#fbfcfe}
 .navlnk:hover{border-color:#4C8DFF;color:#4C8DFF;background:#f2f6ff}
 .navoff{color:#c3ccda;padding:4px 10px;border:1px dashed #e6ebf3;border-radius:6px;cursor:not-allowed}
 .navdate{padding:3px 8px;border:1px solid #dfe5ee;border-radius:6px;font-size:13px;color:#33415c;background:#fff}
 .navhint{color:#a8b2c1;font-size:12px}
 @media print {
   body{background:#fff;padding:0}
   .card{box-shadow:none;break-inside:avoid}
   .nav{display:none}
 }
"""


def _day_href(target_day, live):
    """实时报告走 ?day= 可任意切换；静态 HTML 只能链到已生成的同名文件。"""
    if live:
        return f"/?day={target_day}"
    return f"time_report_{target_day}.html"


def _day_report_exists(target_day, live):
    if live:
        return True
    return os.path.exists(
        os.path.join(_SCRIPT_DIR, f"time_report_{target_day}.html"))


def _build_nav(day, live):
    cur = date.fromisoformat(day)
    today_d = date.today().isoformat()

    def link(label, target, style=""):
        if not _day_report_exists(target, live):
            return (f'<span class="navoff" title="{target} 的报告还没生成，'
                    f'用「启动实时报告.bat」可任意切换日期">'
                    f'{html_mod.escape(label)}</span>')
        return (f'<a class="navlnk" style="{style}" '
                f'href="{_day_href(target, live)}">{html_mod.escape(label)}</a>')

    parts = [
        link("← 前一天", (cur - timedelta(days=1)).isoformat()),
        link("后一天 →", (cur + timedelta(days=1)).isoformat()),
    ]
    if day != today_d:
        parts.append(link("回到今天", today_d, "color:#4C8DFF;font-weight:600"))
    # 原生日期控件自带「清除」按钮，会把 value 变成空串；不判空就跳到
    # time_report_.html 这种不存在的死链。空值时不做任何跳转。
    js = ("if(this.value)location.href='/?day='+this.value" if live
          else "if(this.value)location.href='time_report_'+this.value+'.html'")
    parts.append(f'<input type="date" class="navdate" value="{day}" '
                 f'onchange="{js}">')
    if not live:
        parts.append('<span class="navhint">静态快照只能跳到已生成的日期</span>')
    return f'<div class="nav">📅 {"".join(parts)}</div>'


def _build_html_doc(day=None, live=False):
    day = day or date.today().isoformat()
    # 生成报告前自动汇总
    try:
        summarize(verbose=False)
    except Exception:
        pass
    conn = get_conn()
    items = fetch_day(conn, day)
    is_summarized = False
    if not items:
        # 原始事件已被清理，尝试从每日汇总读取
        rows = conn.execute(
            "SELECT category, total_sec FROM daily_summaries WHERE day=?",
            (day,)).fetchall()
        if not rows:
            print(f"📭 {day} 没有数据，无法生成报告")
            return None, day
        is_summarized = True
        total = sum(r[1] for r in rows)
        items = [{"name": f"[{cat}] 汇总", "category": cat,
                  "start": day + "T00:00:00", "end": day + "T23:59:59",
                  "duration": dur, "sub": None} for cat, dur in rows]
    a = _build_analysis(items)
    total = a["total"]
    if total == 0:
        print("⚠️ 总时长为0，无法生成报告")
        return None, day

    cat_color = {cat: PALETTE[i % len(PALETTE)]
                 for i, (cat, _) in enumerate(a["sorted_cats"])}
    grad_parts, acc = [], 0.0
    for cat, d in a["sorted_cats"]:
        pct = d / total * 100
        grad_parts.append(f"{cat_color[cat]} {acc:.3f}% {acc + pct:.3f}%")
        acc += pct

    legend_rows = [
        f'<div class="row"><span class="dot" style="background:{cat_color[cat]}"></span>'
        f'<span>{html_mod.escape(cat)}</span><b style="margin-left:auto">'
        f'{format_duration(d)}</b><span class="sub">（{d/total*100:.1f}%）</span></div>'
        for cat, d in a["sorted_cats"]]

    day_start = datetime.fromisoformat(day + "T00:00:00")
    now = datetime.now()
    segs = []
    cross_day_hint = ""
    for it in items:
        s = datetime.fromisoformat(it["start"])
        e = datetime.fromisoformat(it["end"]) if it["end"] else now
        s_off = max(0.0, (s - day_start).total_seconds())
        e_off = min(86400.0, (e - day_start).total_seconds())
        if e_off <= s_off:
            continue
        left = s_off / 86400 * 100
        width = max((e_off - s_off) / 86400 * 100, 0.15)
        segs.append(f'<div class="seg" style="left:{left:.3f}%;width:{width:.3f}%;'
                    f'background:{cat_color[it["category"]]}" '
                    f'title="{html_mod.escape(it["name"])}（{format_duration(it["duration"])}）"></div>')
        # 跨天检测
        if it.get("end") and it["end"][:10] != day:
            cross_day_hint = '<span class="cross-day"> 含跨天事件</span>'

    trs = [
        f'<tr><td>{it["start"][11:16]} - {it["end"][11:16] if it["end"] else "进行中"}</td>'
        f'<td><span class="tag" style="background:{cat_color[it["category"]]}">'
        f'{html_mod.escape(it["category"])}</span></td>'
        f'<td title="{html_mod.escape(it["name"])}">{html_mod.escape(it["name"][:60])}</td>'
        f'<td>{format_duration(it["duration"])}</td></tr>'
        for it in reversed(items)]

    tips = []
    if a["avg"] < 600:
        tips.append("平均单事件时长不足10分钟，碎片化较严重，可尝试合并同类任务。")
    if a["sorted_cats"] and a["sorted_cats"][0][1] / total > 0.6:
        tips.append(f"{a['sorted_cats'][0][0]} 占比超过60%，注意劳逸结合。")
    if len(items) > 15:
        tips.append("今天切换事件较频繁，可能影响专注度，试试番茄工作法。")
    tips_html = "".join(f'<div class="tip">💡 {html_mod.escape(t)}</div>' for t in tips) \
        or '<div class="tip">👍 节奏不错，继续保持！</div>'

    # 细分拆解卡片
    subs_html = ""
    if a["sorted_subs"]:
        groups = {}
        for key, d in a["sorted_subs"]:
            cat, _, sub = key.partition(" › ")
            groups.setdefault(cat, []).append((sub, d))
        parts = []
        for cat, rows in groups.items():
            cat_total = sum(d for _, d in rows)
            parts.append(
                f'<div class="subgroup"><div class="gname">'
                f'{html_mod.escape(cat)} <span class="sub">（'
                f'{format_duration(cat_total)}）</span></div>')
            for sub, d in rows:
                w = d / cat_total * 100 if cat_total else 0
                parts.append(
                    f'<div class="subrow"><span style="min-width:110px">'
                    f'{html_mod.escape(sub)}</span>'
                    f'<div class="subbar"><i style="width:{w:.1f}%;'
                    f'background:{cat_color[cat]}"></i></div>'
                    f'<b style="min-width:88px;text-align:right">'
                    f'{format_duration(d)}</b>'
                    f'<span class="sub" style="min-width:48px;text-align:right">'
                    f'{d/total*100:.1f}%</span></div>')
            parts.append('</div>')
        subs_html = ('<div class="card"><h2>🔍 细分拆解'
                     '（上网/AI 等大类内部）</h2>' + "".join(parts) + '</div>')

    # 近期趋势卡片（最近 7 天，按类别堆叠）
    trend_html = ""
    try:
        trend_days = 7
        trend_first = (date.fromisoformat(day) - timedelta(days=trend_days - 1)).isoformat()
        # 明细优先（默认永久保留）；只有原始明细已被清理的天，才回落到每日汇总。
        # 注意不能"汇总表非空就只读汇总表"——刚过完的当天还没进汇总表，会被整条吞掉。
        rows = conn.execute(
            """SELECT d, category, SUM(sec), SUM(cnt) FROM (
                 SELECT date(start_time) AS d, category,
                        SUM(duration_sec) AS sec, COUNT(*) AS cnt
                 FROM events
                 WHERE date(start_time) >= ? AND date(start_time) <= ?
                 GROUP BY d, category
                 UNION ALL
                 SELECT day AS d, category, total_sec AS sec, event_count AS cnt
                 FROM daily_summaries
                 WHERE day >= ? AND day <= ?
                   AND day NOT IN (SELECT DISTINCT date(start_time) FROM events)
               ) GROUP BY d, category ORDER BY d""",
            (trend_first, day, trend_first, day)).fetchall()
        by_day = {}
        for td, cat, sec, cnt in rows:
            by_day.setdefault(td, {})[cat] = by_day.get(td, {}).get(cat, 0) + (sec or 0)
        if by_day:
            day_tot = {d: sum(v.values()) for d, v in by_day.items()}
            max_t = max(day_tot.values()) or 1
            # 配色沿用当天饼图；只在历史里出现过的类别顺次补色
            trend_colors = dict(cat_color)
            extra = sorted(
                {c for v in by_day.values() for c in v} - set(trend_colors),
                key=lambda c: -sum(v.get(c, 0) for v in by_day.values()))
            for i, c in enumerate(extra):
                trend_colors[c] = PALETTE[(len(cat_color) + i) % len(PALETTE)]

            trend_bars = ""
            for td in sorted(by_day):
                tot = day_tot[td]
                cats = sorted(by_day[td].items(), key=lambda kv: -kv[1])
                tip = "、".join(f"{c} {format_duration(s)}" for c, s in cats[:8])
                segs = "".join(
                    f'<div style="width:{s / tot * 100:.3f}%;height:100%;'
                    f'background:{trend_colors[c]}" '
                    f'title="{html_mod.escape(c)} {format_duration(s)}"></div>'
                    for c, s in cats if tot and s > 0)
                bar_w = int(tot / max_t * 100)
                is_cur = "font-weight:700" if td == day else ""
                trend_bars += (
                    f'<div style="display:flex;align-items:center;margin:3px 0;'
                    f'font-size:12px">'
                    f'<span style="min-width:72px;color:#8a93a3;{is_cur}">'
                    f'<a href="{_day_href(td, live)}" title="查看 {td}" '
                    f'style="color:inherit;text-decoration:none;'
                    f'border-bottom:1px dotted #c3ccda">{td[5:]}</a></span>'
                    f'<div style="flex:1;background:#eef1f6;border-radius:3px;'
                    f'height:10px;margin:0 8px;overflow:hidden" '
                    f'title="{html_mod.escape(tip)}">'
                    f'<div style="width:{bar_w}%;height:100%;display:flex">'
                    f'{segs}</div></div>'
                    f'<span style="min-width:168px;text-align:right;{is_cur}">'
                    f'{format_duration(tot)}'
                    f'<span style="color:#8a93a3;font-weight:400">'
                    f' · {html_mod.escape(cats[0][0]) if cats else ""}</span></span>'
                    f'</div>')
            trend_html = (
                f'<div class="card"><h2>📈 近 {trend_days} 天趋势</h2>'
                f'<div class="sub" style="margin-bottom:6px">条长=当天总量，'
                f'颜色=类别构成（悬停看明细），点日期可切到那天</div>'
                f'{trend_bars}</div>')
    except Exception:
        pass

    top_cat, top_cat_dur = a["sorted_cats"][0]
    top_ev, top_ev_dur = a["sorted_events"][0]

    summary_notice = ""
    if is_summarized:
        summary_notice = '<div class="tip">📦 原始事件已归档，以下仅显示分类汇总数据（无事件明细）</div>'

    timeline_section = ""
    if not is_summarized:
        timeline_section = f"""
<div class="card">
  <h2>🕐 24 小时时间线 {cross_day_hint}</h2>
  <div class="timeline">{''.join(segs)}</div>
  <div class="hours"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
</div>"""

    nav_html = _build_nav(day, live)

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>时间追踪日报 {day}</title>
<style>{HTML_STYLE.replace('__GRADIENT__', ', '.join(grad_parts))}</style>
</head>
<body>
<div class="card">
  <h1>⏱ 时间追踪日报</h1>
  <div class="sub">{day} · 由本地时间追踪器自动生成</div>
  <div class="kpis">
    <div class="kpi"><div class="v">{format_duration(total)}</div><div class="k">总记录时长</div></div>
    <div class="kpi"><div class="v">{len(items)} 次</div><div class="k">事件切换</div></div>
    <div class="kpi"><div class="v">{format_duration(int(a['avg']))}</div><div class="k">平均单事件时长</div></div>
    <div class="kpi"><div class="v">{len(a['sorted_cats'])} 个</div><div class="k">涉及类别</div></div>
  </div>
  {nav_html}
</div>
<div class="card">
  <h2> 类别用时占比</h2>
  <div class="flex">
    <div class="pie"></div>
    <div class="legend" style="flex:1;min-width:260px">{''.join(legend_rows)}</div>
  </div>
</div>
{subs_html}
{timeline_section}
{trend_html}
<div class="card">
  <h2> 今日焦点</h2>
  <div class="kpis">
    <div class="kpi"><div class="v" title="{html_mod.escape(top_cat)}">{html_mod.escape(top_cat)}</div><div class="k">最耗时类别（{top_cat_dur/total*100:.1f}%）</div></div>
    <div class="kpi"><div class="v" title="{html_mod.escape(top_ev)}">{html_mod.escape(top_ev[:40])}</div><div class="k">最耗时事件（{format_duration(top_ev_dur)}）</div></div>
    <div class="kpi"><div class="v" title="{html_mod.escape(a['longest']['name'])}">{html_mod.escape(a['longest']['name'][:40])}</div><div class="k">最长专注（{format_duration(a['longest']['duration'])}）</div></div>
  </div>
  {tips_html}
</div>
{summary_notice}
<div class="card">
  <h2>📋 事件明细</h2>
  <table>
    <tr><th>时间</th><th>类别</th><th>事件</th><th>时长</th></tr>
    {''.join(trs)}
  </table>
</div>
</body>
</html>"""

    if live:
        html_doc = html_doc.replace(
            "</title>", '</title>\n<meta http-equiv="refresh" content="30">', 1)
    return html_doc, day


def html_report(day=None, open_it=False):
    html_doc, day = _build_html_doc(day)
    if html_doc is None:
        return
    out = os.path.join(_SCRIPT_DIR, f"time_report_{day}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(f" HTML 报告已生成：{out}")
    if open_it:
        try:
            os.startfile(out)
        except Exception as e:
            print(f"⚠️ 无法自动打开报告：{e}")


# ---------- 实时报告服务 ----------

def _serve_pid():
    try:
        with open(SERVE_PID_PATH, encoding="utf-8") as f:
            pid = int(f.read().strip())
        return pid if pid_alive(pid) else None
    except Exception:
        return None


def serve_stop():
    pid = _serve_pid()
    if not pid:
        print("⚠️ 实时报告服务没在运行")
        if os.path.exists(SERVE_PID_PATH):
            os.remove(SERVE_PID_PATH)
        return
    # 先尝试优雅停止
    try:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, timeout=5)
    except Exception:
        pass
    # 超时后强制停止
    if pid_alive(pid):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    if os.path.exists(SERVE_PID_PATH):
        os.remove(SERVE_PID_PATH)
    print("⏹ 实时报告服务已停止")


def serve_report(port=8765, day=None, background=True):
    """本地实时报告：只绑定 127.0.0.1，仅本机可访问。
    每次手动刷新都从数据库重新读取最新数据；
    页面无操作 30 秒也会自动刷新一次。
    后台模式无命令行窗口，用 serve stop 停止。"""
    import webbrowser
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse, parse_qs

    url = f"http://127.0.0.1:{port}/"

    if background:
        pid = _serve_pid()
        if pid:
            print(f"🟢 实时报告服务已在运行（PID {pid}），正在打开页面……")
            webbrowser.open(url)
            return
        import socket
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            print(f"⚠️ 无法启动服务，端口 {port} 可能被占用")
            return
        finally:
            probe.close()
        args = [sys.executable, os.path.abspath(__file__), "serve", "--fg",
                "--port", str(port)]
        if day:
            args += ["--day", day]
        with open(SERVE_LOG_PATH, "a", encoding="utf-8") as logf:
            proc = subprocess.Popen(
                args, stdout=logf, stderr=subprocess.STDOUT,
                creationflags=0x00000008 | 0x08000000,
                close_fds=True)
        with open(SERVE_PID_PATH, "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
        print(f"✅ 实时报告服务已在后台启动（PID {proc.pid}，无窗口）")
        print(f"   访问 {url}，手动刷新即看最新数据")
        print("   停止方法：py time_tracker.py serve stop")
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            d = q.get("day", [""])[0] or day or None
            if d:
                try:
                    datetime.fromisoformat(d + "T00:00:00")
                except ValueError:
                    d = None
            body, shown = _build_html_doc(d, live=True)
            if body is None:
                body = ("<meta charset='utf-8'>"
                        "<p style=\"font:16px/2 system-ui;padding:40px\">"
                        "📭 {} 没有记录。<a href='/'>回到今天</a></p>"
                        ).format(shown)
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            print(f"   [{datetime.now().strftime('%H:%M:%S')}] " + fmt % args)

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"⚠️ 无法启动服务（端口 {port} 可能被占用）：{e}")
        return
    with open(SERVE_PID_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    print(f"🌐 实时报告服务已启动：{url}")
    print("   手动刷新即可看到最新数据；无操作 30 秒也会自动刷新。")
    print("   停止方法：py time_tracker.py serve stop（或 Ctrl+C）")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 服务已停止。")
    finally:
        server.server_close()
        try:
            os.remove(SERVE_PID_PATH)
        except Exception:
            pass


# ---------- matplotlib 图表 ----------

def chart(day=None):
    day = day or date.today().isoformat()
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("⚠️ 未安装 matplotlib，请先执行：py -m pip install matplotlib")
        return

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "Noto Sans CJK SC"]
    plt.rcParams["axes.unicode_minus"] = False

    items = fetch_day(get_conn(), day)
    if not items:
        print(f"📭 {day} 没有数据，无法绘图")
        return
    a = _build_analysis(items)
    if a["total"] == 0:
        print("️ 总时长为0，无法绘图")
        return

    cats = [c for c, _ in a["sorted_cats"]]
    sizes = [d for _, d in a["sorted_cats"]]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5.5))
    ax1.pie(sizes, labels=cats, autopct="%1.1f%%",
            colors=[PALETTE[i % len(PALETTE)] for i in range(len(cats))],
            startangle=90, wedgeprops={"edgecolor": "white"})
    ax1.set_title(f"{day} 类别用时占比")

    day_start = datetime.fromisoformat(day + "T00:00:00")
    now = datetime.now()
    cat_hours = {c: [0.0] * 24 for c in cats}
    for it in items:
        s = datetime.fromisoformat(it["start"])
        e = datetime.fromisoformat(it["end"]) if it["end"] else now
        s_off = max(0.0, (s - day_start).total_seconds())
        e_off = min(86400.0, (e - day_start).total_seconds())
        for h in range(24):
            overlap = min(e_off, (h + 1) * 3600) - max(s_off, h * 3600)
            if overlap > 0:
                cat_hours[it["category"]][h] += overlap / 60.0

    bottom = [0.0] * 24
    for i, c in enumerate(cats):
        ax2.bar(range(24), cat_hours[c], bottom=bottom, label=c,
                color=PALETTE[i % len(PALETTE)])
        bottom = [b + v for b, v in zip(bottom, cat_hours[c])]
    ax2.set_xlabel("时刻")
    ax2.set_ylabel("分钟")
    ax2.set_title(f"{day} 每小时用时分布（堆叠）")
    ax2.set_xticks(range(0, 24, 2))
    ax2.legend(fontsize=9)

    fig.tight_layout()
    out = os.path.join(_SCRIPT_DIR, f"time_chart_{day}.png")
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"📈 图表已保存：{out}")


# ---------- 番茄钟 ----------

def _beep(times=3):
    try:
        import winsound
        for _ in range(times):
            winsound.Beep(1000, 400)
            time.sleep(0.15)
    except Exception:
        print("\a", end="", flush=True)


def pomodoro(minutes=25.0, name="番茄钟"):
    conn = get_conn()
    now = datetime.now()
    closed = _close_open(conn, now)
    if closed:
        print(f" 自动结束上一个事件：{closed[0]}（{format_duration(closed[1])}）")
    _open_event(conn, name, "番茄钟", now, "专注·深度工作")

    end = now + timedelta(minutes=minutes)
    print(f"🍅 番茄钟开始：{name}，{minutes:g} 分钟后提醒（Ctrl+C 可提前结束）")
    try:
        while True:
            remain = (end - datetime.now()).total_seconds()
            if remain <= 0:
                break
            mm, ss = divmod(int(remain), 60)
            print(f"\r  ⏳ 剩余 {mm:02d}:{ss:02d}", end="", flush=True)
            time.sleep(min(1.0, max(0.05, remain)))
        closed = _close_open(get_conn(), datetime.now())
        print(f"\r🔔 时间到！番茄钟结束：{name}，"
              f"共记录 {format_duration(closed[1]) if closed else format_duration(minutes*60)}      ")
        _beep()
        print("💡 休息 5 分钟，喝口水吧～")
    except KeyboardInterrupt:
        closed = _close_open(get_conn(), datetime.now())
        if closed:
            print(f"\n⏹ 番茄钟提前结束：{closed[0]}，已记录 {format_duration(closed[1])}")


# ---------- 全自动监控（核心） ----------

def _log(msg, max_size_mb=10):
    """写入日志，支持自动轮转（超过 max_size_mb 时备份旧日志）"""
    line = f"{datetime.now().strftime('%m-%d %H:%M:%S')} {msg}"
    try:
        # 检查日志大小，超过阈值时轮转
        if os.path.exists(LOG_PATH):
            size_mb = os.path.getsize(LOG_PATH) / (1024 * 1024)
            if size_mb > max_size_mb:
                backup = LOG_PATH + ".1"
                if os.path.exists(backup):
                    os.remove(backup)
                os.rename(LOG_PATH, backup)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass


def monitor(interval=10.0, duration_min=0.0, idle_threshold=300.0,
            debounce=2, dry_run=False):
    """全自动监控。
    debounce: 连续 N 次采样一致才确认切换（默认 2，即 2*interval 秒）
    """
    if not _user32:
        print("⚠️ 当前系统不支持窗口监控（需要 Windows 图形会话）")
        return
    if not dry_run:
        # 原子化 PID 检查：用文件锁避免竞态
        try:
            import msvcrt
            lock_file = open(PID_PATH + ".lock", "w")
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except Exception:
                # 锁被占用，说明另一个进程正在启动
                print("⚠️ 另一个监控进程正在启动，请稍候")
                lock_file.close()
                return
        except ImportError:
            lock_file = None

        try:
            with open(PID_PATH, encoding="utf-8") as f:
                old = int(f.read().strip())
            if pid_alive(old):
                print(f"⚠️ 监控已在运行中（PID {old}），无需重复启动")
                if lock_file:
                    lock_file.close()
                return
        except Exception:
            pass
        with open(PID_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        if os.path.exists(STOP_FLAG):
            os.remove(STOP_FLAG)
        if lock_file:
            lock_file.close()

    exe_rules, title_rules = load_rules()
    conn = None if dry_run else get_conn()

    # 自动汇总
    if not dry_run:
        try:
            summarize(verbose=False)
        except Exception:
            pass

    end_at = datetime.now() + timedelta(minutes=duration_min) if duration_min > 0 else None
    active_key, pending_key, pending_hits = None, None, 0
    _log(f"▶ 监控启动（间隔 {interval:g}s，闲置阈值 {idle_threshold:g}s，"
         f"防抖 {debounce} 次，{'预览' if dry_run else '记录'}模式）")

    # 注册 SIGTERM handler 做优雅退出
    _shutdown_requested = False

    def _handle_shutdown(signum, frame):
        nonlocal _shutdown_requested
        _shutdown_requested = True
        _log("⏹ 收到终止信号")

    if not dry_run and sys.platform == "win32":
        try:
            signal.signal(signal.SIGTERM, _handle_shutdown)
        except Exception:
            pass

    try:
        while True:
            if _shutdown_requested:
                break
            try:
                if idle_seconds() >= idle_threshold:
                    key = ("离开/闲置（无键鼠操作）", "休息", None)
                else:
                    title, exe = win_info()
                    if title:
                        cat, sub = classify(exe, title, exe_rules, title_rules)
                        key = (title[:80], cat, sub)
                    elif exe:
                        # 空标题但有进程名：用 exe 作为 fallback
                        cat = exe_rules.get(exe.lower(), "其他")
                        key = (f"[{exe}]", cat, None)
                    else:
                        key = None

                if key is not None and key != active_key:
                    if key == pending_key:
                        pending_hits += 1
                    else:
                        pending_key, pending_hits = key, 1
                    if pending_hits >= debounce:
                        now = datetime.now()
                        if dry_run:
                            if active_key:
                                _log(f"⏹ （预览）结束：{active_key[0]}")
                            _log(f"✅ （预览）开始：[{key[1]}] {key[0]}")
                        else:
                            closed = _close_open(conn, now)
                            if closed:
                                _log(f"⏹ 结束：{closed[0]}（{format_duration(closed[1])}）")
                            _open_event(conn, key[0], key[1], now, key[2])
                            sub_tip = f"（{key[2]}）" if key[2] else ""
                            _log(f"✅ 开始：[{key[1]}{sub_tip}] {key[0]}")
                        active_key, pending_key, pending_hits = key, None, 0
                elif key == active_key:
                    pending_key, pending_hits = None, 0

                if os.path.exists(STOP_FLAG):
                    _log(" 收到停止信号")
                    break
                if end_at and datetime.now() >= end_at:
                    _log("⏹ 到达设定时长，自动停止")
                    break
            except Exception as e:
                _log(f"⚠️ 单次迭代异常：{e}（继续监控）")

            # deadline-based sleep 减少漂移
            cycle_start = time.monotonic()
            time.sleep(interval)
            elapsed = time.monotonic() - cycle_start
            if elapsed > interval:
                pass  # 处理耗时超过 interval，不额外 sleep
            else:
                time.sleep(max(0, interval - elapsed))
    except KeyboardInterrupt:
        _log("⏹ 收到 Ctrl+C")
    finally:
        if not dry_run:
            try:
                closed = _close_open(conn, datetime.now())
                if closed:
                    _log(f"⏹ 结束：{closed[0]}（{format_duration(closed[1])}）")
            except Exception as e:
                _log(f"⚠️ 关闭事件时异常：{e}")
            try:
                os.remove(PID_PATH)
            except Exception:
                pass
        _log(" 监控已退出")


def _daemon_pid():
    try:
        with open(PID_PATH, encoding="utf-8") as f:
            pid = int(f.read().strip())
        return pid if pid_alive(pid) else None
    except Exception:
        return None


def daemon_start(interval=10.0, idle_threshold=300.0):
    if _daemon_pid():
        print(f"⚠️ 监控已在运行中（PID {_daemon_pid()}）")
        return
    args = [sys.executable, os.path.abspath(__file__), "monitor",
            "--interval", str(interval), "--idle", str(idle_threshold)]
    with open(LOG_PATH, "a", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            args, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=0x00000008 | 0x08000000,
            close_fds=True)
    print(f"✅ 监控已在后台启动（PID {proc.pid}）")
    print(f"   日志：{LOG_PATH}")
    print("   现在起会自动记录你的活动窗口，用 today / report / html 查看。")


def daemon_stop():
    pid = _daemon_pid()
    if not pid:
        print("⚠️ 后台监控没有在运行")
        for p in (PID_PATH, STOP_FLAG):
            if os.path.exists(p):
                os.remove(p)
        return
    # 优雅停止：先设 STOP_FLAG，等待 monitor 自行退出
    with open(STOP_FLAG, "w", encoding="utf-8") as f:
        f.write("stop")
    # 等待时间基于实际 interval（默认 10s），给 3 个周期 + 余量
    wait_secs = 35
    for _ in range(wait_secs):
        if not pid_alive(pid):
            break
        time.sleep(1)
    else:
        # 超时后尝试发送 CTRL_BREAK_EVENT
        try:
            subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, timeout=5)
            time.sleep(3)
        except Exception:
            pass
        # 仍然存活才强杀
        if pid_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    for p in (STOP_FLAG, PID_PATH):
        if os.path.exists(p):
            os.remove(p)
    print("⏹ 后台监控已停止")


def daemon_status():
    pid = _daemon_pid()
    if pid:
        print(f" 后台监控运行中（PID {pid}）")
    else:
        print(" 后台监控未运行")
    status()
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-5:]
        if lines:
            print("\n最近日志：")
            for ln in lines:
                print("  " + ln.rstrip())
    except Exception:
        pass


# ---------- 开机自启 ----------

def _startup_dir():
    return os.path.join(os.environ.get("APPDATA", ""),
                        r"Microsoft\Windows\Start Menu\Programs\Startup")


def autostart(on):
    vbs = os.path.join(_startup_dir(), AUTOSTART_NAME)
    if on:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pyw):
            pyw = sys.executable
        script = os.path.abspath(__file__)
        content = (
            "Set WshShell = CreateObject(\"WScript.Shell\")\r\n"
            f"WshShell.Run \"\"\"{pyw}\"\" \"\"{script}\"\" monitor\", 0, False\r\n"
        )
        # WSH 只认 ANSI 或 UTF-16 的 .vbs：utf-8-sig 的 EF BB BF 会让 VBScript
        # 在 (1,1) 报“无效字符”直接编译失败（纯 utf-8 则中文路径被按 ANSI 读成乱码）。
        with open(vbs, "w", encoding="utf-16", newline="") as f:
            f.write(content)
        print(f"✅ 已设置开机自启：{vbs}")
        print("   下次登录 Windows 时监控会自动在后台启动。")
    else:
        if os.path.exists(vbs):
            os.remove(vbs)
            print("⏹ 已取消开机自启")


# ---------- 参数解析 ----------

def parse_day(s):
    try:
        date.fromisoformat(s)
        return s
    except ValueError:
        print(f"⚠️ 日期格式应为 YYYY-MM-DD，收到：{s}")
        return None


def parse_flags(args, spec):
    vals, positional, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:]
            if key not in spec:
                print(f"⚠️ 未知参数：{a}")
                return None, None
            if spec[key] is bool:
                vals[key] = True
            else:
                i += 1
                if i >= len(args):
                    print(f"⚠️ 参数 {a} 缺少值")
                    return None, None
                try:
                    vals[key] = spec[key](args[i])
                except ValueError:
                    print(f"⚠️ 参数 {a} 的值无效：{args[i]}")
                    return None, None
        else:
            positional.append(a)
        i += 1
    return positional, vals


def show_rules():
    exe_rules, title_rules = load_rules()
    print(f" 自定义规则文件：{RULES_PATH}")
    print("   文件格式（不存在则全部用内置规则）：")
    print('   {"exe": {"xxx.exe": "类别"}, "title": [["关键词", "类别"], ...]}')
    print('   标题规则支持负向关键词：["copilot", "!github"] 表示含 copilot 但不含 github')
    print("\n内置+自定义合并后的程序规则：")
    for exe, cat in sorted(exe_rules.items()):
        print(f"  {exe:<24} -> {cat}")
    print("\n标题细分规则（子类归属）：")
    for kws, cat in title_rules:
        print(f"  {cat:<14} <- {kws}")


def print_usage():
    print(
        """
 本地时间追踪器（自动监控版）v2

全自动监控（核心）：
  daemon start [--interval 10] [--idle 300]   后台启动自动监控（无窗口常驻）
  daemon stop                                 停止后台监控
  daemon status                               查看监控状态+当前事件+最近日志
  autostart on|off                            设置/取消开机自启
  monitor [...] [--dry-run]                   前台运行监控（调试用）
  监控会自动记录活动窗口、按程序归类，闲置超过阈值记为"休息"，
  数据全部存本地数据库，过完的整天自动逐级汇总（明细→每日→每月）。
  默认永久保留全部明细、不自动删除；策略见 retention 命令。

查询与分析：
  today [YYYY-MM-DD]        某天做了什么、各多久（默认今天）
  report [YYYY-MM-DD]       文字分析：占比/倾向/建议
  html [YYYY-MM-DD] [--open]  生成可视化日报（--open 自动用浏览器打开）
  serve [--port 8765] [--day YYYY-MM-DD]
                              后台启动本地实时报告（无窗口，仅本机访问）
  serve stop                  停止实时报告服务
  serve --fg [...]            前台运行（带窗口，调试用）
  chart [YYYY-MM-DD]        生成饼图+堆叠柱状图 PNG
  history [天数]            最近 N 天趋势（默认 7）
  rules                     查看全部内置+自定义分类规则
  reclassify                修改规则后，用最新规则重算全部历史分类
  summarize                 手动执行逐级汇总（明细→每日→每月）
  retention                 查看保留策略 + 库体积 + 每年增长估算
  retention set 键=数值      改策略（events=保留天数 daily=保留月数，0=永久保留）
  retention clean 键=数值    不改策略，按给定天数立即清理一次（自动先备份）

手动补充（可选）：
  start <事件名> [类别]     手动开始记录（自动结束上一个）
  stop                      手动结束当前记录
  status                    查看当前进行中的事件

番茄钟：
  pomodoro [--minutes 25] [--name 专注]
"""
    )


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    if len(sys.argv) < 2:
        print_usage()
        return
    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "start":
        if not args:
            print("用法：python time_tracker.py start <事件名> [类别]")
            return
        start_event(args[0], args[1] if len(args) > 1 else "未分类")
    elif cmd == "stop":
        stop_event()
    elif cmd == "status":
        status()
    elif cmd in ("today", "report", "chart"):
        day = parse_day(args[0]) if args else None
        if args and day is None:
            return
        {"today": today_records, "report": report, "chart": chart}[cmd](day)
    elif cmd == "html":
        positional, vals = parse_flags(args, {"open": bool})
        if positional is None:
            return
        day = parse_day(positional[0]) if positional else None
        if positional and day is None:
            return
        html_report(day, vals.get("open", False))
    elif cmd == "serve":
        positional, vals = parse_flags(
            args, {"port": int, "day": str, "fg": bool})
        if positional is None:
            return
        if positional and positional[0] == "stop":
            serve_stop()
        else:
            serve_report(vals.get("port", 8765), vals.get("day"),
                         background=not vals.get("fg", False))
    elif cmd == "history":
        try:
            history(max(1, int(args[0])) if args else 7)
        except ValueError:
            print(f"⚠️ 天数应为正整数，收到：{args[0]}")
    elif cmd == "pomodoro":
        positional, vals = parse_flags(args, {"minutes": float, "name": str})
        if positional is None:
            return
        pomodoro(vals.get("minutes", 25.0), vals.get("name", "番茄钟"))
    elif cmd == "monitor":
        positional, vals = parse_flags(
            args, {"interval": float, "duration": float,
                   "idle": float, "debounce": int, "dry-run": bool})
        if positional is None:
            return
        interval = vals.get("interval", 10.0)
        if interval <= 0:
            print("⚠️ --interval 必须大于 0")
            return
        monitor(interval, vals.get("duration", 0.0),
                vals.get("idle", 300.0), vals.get("debounce", 2),
                vals.get("dry-run", False))
    elif cmd == "daemon":
        if not args or args[0] not in ("start", "stop", "status"):
            print("用法：daemon start|stop|status")
            return
        if args[0] == "start":
            positional, vals = parse_flags(args[1:], {"interval": float, "idle": float})
            if positional is None:
                return
            daemon_start(vals.get("interval", 10.0), vals.get("idle", 300.0))
        elif args[0] == "stop":
            daemon_stop()
        else:
            daemon_status()
    elif cmd == "autostart":
        if not args or args[0] not in ("on", "off"):
            print("用法：autostart on|off")
            return
        autostart(args[0] == "on")
    elif cmd == "rules":
        show_rules()
    elif cmd == "reclassify":
        reclassify()
    elif cmd == "summarize":
        summarize(verbose=True)
    elif cmd == "retention":
        retention_cmd(args)
    else:
        print_usage()


if __name__ == "__main__":
    main()
