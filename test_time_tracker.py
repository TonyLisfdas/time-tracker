#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时间追踪器单元测试
"""
import os
import sys
import sqlite3
import tempfile
import unittest
import unittest.mock
from datetime import datetime, date

# 将项目目录加入 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time_tracker as tt


class TestFormatDuration(unittest.TestCase):
    """测试时长格式化"""

    def test_seconds_only(self):
        self.assertEqual(tt.format_duration(30), "30秒")

    def test_minutes_and_seconds(self):
        self.assertEqual(tt.format_duration(90), "1分30秒")

    def test_hours_minutes_seconds(self):
        self.assertEqual(tt.format_duration(3661), "1时01分01秒")

    def test_zero(self):
        self.assertEqual(tt.format_duration(0), "0秒")

    def test_negative(self):
        self.assertEqual(tt.format_duration(-10), "0秒")


class TestTitleMatching(unittest.TestCase):
    """测试标题匹配逻辑（含负向关键词）"""

    def test_positive_match(self):
        self.assertTrue(tt._title_matches("chatgpt 对话", ["chatgpt"]))

    def test_negative_keyword_excludes(self):
        # 含 copilot 但含 github → 不匹配
        self.assertFalse(tt._title_matches("github copilot", ["copilot", "!github"]))

    def test_negative_keyword_allows(self):
        # 含 copilot 但不含 github → 匹配
        self.assertTrue(tt._title_matches("copilot chat", ["copilot", "!github"]))

    def test_no_positive_no_match(self):
        self.assertFalse(tt._title_matches("hello world", ["chatgpt"]))


class TestClassify(unittest.TestCase):
    """测试分类逻辑"""

    def setUp(self):
        self.exe_rules, self.title_rules = tt.load_rules()

    def test_browser_with_ai_title(self):
        cat, sub = tt.classify("chrome.exe", "chatgpt 对话", self.exe_rules, self.title_rules)
        self.assertEqual(cat, "AI 工具")
        self.assertEqual(sub, "AI·ChatGPT")

    def test_browser_with_video_title(self):
        cat, sub = tt.classify("chrome.exe", "bilibili 视频", self.exe_rules, self.title_rules)
        self.assertEqual(cat, "娱乐影音")
        self.assertEqual(sub, "视频")

    def test_unknown_exe_with_title(self):
        # 未知 exe 但标题匹配 AI → 应提升为 AI 工具
        cat, sub = tt.classify("unknown.exe", "qwen 对话", self.exe_rules, self.title_rules)
        self.assertEqual(cat, "AI 工具")

    def test_unknown_exe_no_title_match(self):
        cat, sub = tt.classify("unknown.exe", "随机窗口", self.exe_rules, self.title_rules)
        self.assertEqual(cat, "其他")
        self.assertIsNone(sub)


class TestDatabase(unittest.TestCase):
    """测试数据库操作"""

    def setUp(self):
        # 使用临时数据库
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        tt.DB_PATH = self.db_path
        tt.PID_PATH = self.db_path + ".pid"
        tt.STOP_FLAG = self.db_path + ".stop"
        tt.LOG_PATH = self.db_path + ".log"
        tt.SERVE_PID_PATH = self.db_path + ".serve.pid"
        tt.SERVE_LOG_PATH = self.db_path + ".serve.log"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_get_conn_creates_tables(self):
        conn = tt.get_conn()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        self.assertIn("events", tables)
        self.assertIn("daily_summaries", tables)
        self.assertIn("monthly_summaries", tables)
        conn.close()

    def test_wal_mode_enabled(self):
        conn = tt.get_conn()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")
        conn.close()

    def test_close_open_all_unclosed(self):
        conn = tt.get_conn()
        now = datetime.now()
        # 插入两条未关闭记录
        conn.execute(
            "INSERT INTO events (name, category, start_time) VALUES (?, ?, ?)",
            ("事件1", "测试", (now.replace(hour=10)).isoformat()))
        conn.execute(
            "INSERT INTO events (name, category, start_time) VALUES (?, ?, ?)",
            ("事件2", "测试", (now.replace(hour=11)).isoformat()))
        conn.commit()

        # 关闭所有
        result = tt._close_open(conn, now.replace(hour=12))
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "事件2")  # 返回最后一条

        # 验证都被关闭
        unclosed = conn.execute(
            "SELECT COUNT(*) FROM events WHERE end_time IS NULL").fetchone()[0]
        self.assertEqual(unclosed, 0)
        conn.close()

    def test_duration_non_negative(self):
        conn = tt.get_conn()
        # 模拟时钟跳变：start_time 在未来
        future = (datetime.now().replace(year=2030)).isoformat()
        conn.execute(
            "INSERT INTO events (name, category, start_time) VALUES (?, ?, ?)",
            ("未来事件", "测试", future))
        conn.commit()

        result = tt._close_open(conn, datetime.now())
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result[1], 0)  # duration 应为非负
        conn.close()


class TestSummarize(unittest.TestCase):
    """测试汇总逻辑"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        tt.DB_PATH = self.db_path
        tt.PID_PATH = self.db_path + ".pid"
        tt.STOP_FLAG = self.db_path + ".stop"
        tt.LOG_PATH = self.db_path + ".log"
        tt.SERVE_PID_PATH = self.db_path + ".serve.pid"
        tt.SERVE_LOG_PATH = self.db_path + ".serve.log"
        # 临时缩短保留期以便测试
        tt.SUMMARY_RETAIN_DAYS = 3
        tt.SUMMARY_RETAIN_MONTHS = 1

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_summarize_old_events(self):
        conn = tt.get_conn()
        old_date = (date.today() - tt.timedelta(days=5)).isoformat()
        conn.execute(
            "INSERT INTO events (name, category, start_time, end_time, duration_sec) "
            "VALUES (?, ?, ?, ?, ?)",
            ("旧事件", "测试", old_date + "T10:00:00", old_date + "T11:00:00", 3600))
        conn.commit()

        result = tt.summarize(verbose=False)
        self.assertGreater(result["days_summarized"], 0)
        self.assertGreater(result["events_deleted"], 0)

        # 原始事件应被清理
        events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE date(start_time) = ?", (old_date,)).fetchone()[0]
        self.assertEqual(events, 0)

        # 每日汇总应有数据
        summaries = conn.execute(
            "SELECT total_sec FROM daily_summaries WHERE day=?", (old_date,)).fetchone()
        self.assertIsNotNone(summaries)
        self.assertEqual(summaries[0], 3600)
        conn.close()


class TestFetchDay(unittest.TestCase):
    """测试数据查询"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        tt.DB_PATH = self.db_path
        tt.PID_PATH = self.db_path + ".pid"
        tt.STOP_FLAG = self.db_path + ".stop"
        tt.LOG_PATH = self.db_path + ".log"
        tt.SERVE_PID_PATH = self.db_path + ".serve.pid"
        tt.SERVE_LOG_PATH = self.db_path + ".serve.log"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fetch_day_returns_items(self):
        conn = tt.get_conn()
        today = date.today().isoformat()
        conn.execute(
            "INSERT INTO events (name, category, start_time, end_time, duration_sec) "
            "VALUES (?, ?, ?, ?, ?)",
            ("测试事件", "测试", today + "T10:00:00", today + "T11:00:00", 3600))
        conn.commit()

        items = tt.fetch_day(conn, today)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "测试事件")
        self.assertEqual(items[0]["duration"], 3600)
        conn.close()

    def test_fetch_day_empty(self):
        conn = tt.get_conn()
        items = tt.fetch_day(conn, "2020-01-01")
        self.assertEqual(len(items), 0)
        conn.close()

    def test_fetch_day_splits_cross_midnight(self):
        # 23:00→次日01:00 应两天各计 3600，而不是全额记在开始日
        conn = tt.get_conn()
        d0 = (date.today() - tt.timedelta(days=1)).isoformat()
        d1 = date.today().isoformat()
        conn.execute(
            "INSERT INTO events (name, category, start_time, end_time, duration_sec) "
            "VALUES (?, ?, ?, ?, ?)",
            ("跨天", "测试", d0 + "T23:00:00", d1 + "T01:00:00", 7200))
        conn.commit()
        self.assertEqual([i["duration"] for i in tt.fetch_day(conn, d0)], [3600])
        self.assertEqual([i["duration"] for i in tt.fetch_day(conn, d1)], [3600])
        conn.close()


class _TempDbTestCase(unittest.TestCase):
    """用 patch 隔离全局 DB 路径，结束自动恢复（旧测试类未恢复是坑）。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self._patchers = []
        for attr, val in (
                ("DB_PATH", self.db_path),
                ("PID_PATH", self.db_path + ".monitor.pid"),
                ("STOP_FLAG", self.db_path + ".monitor.stop"),
                ("LOG_PATH", self.db_path + ".monitor.log"),
                ("SERVE_PID_PATH", self.db_path + ".serve.pid"),
                ("SERVE_LOG_PATH", self.db_path + ".serve.log")):
            p = unittest.mock.patch.object(tt, attr, val)
            p.start()
            self._patchers.append(p)

    def tearDown(self):
        for p in reversed(self._patchers):
            p.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestBugFixes202610(_TempDbTestCase):
    """2026-10 一轮高优先级修复的回归测试。"""

    def test_get_conn_creates_nested_dir(self):
        nested = os.path.join(self.tmpdir, "a", "b", "deep.db")
        with unittest.mock.patch.object(tt, "DB_PATH", nested):
            conn = tt.get_conn()
            conn.execute("SELECT 1")
            conn.close()
        self.assertTrue(os.path.exists(nested))

    def test_retention_show_with_only_open_event(self):
        import io, contextlib
        conn = tt.get_conn()
        conn.execute(
            "INSERT INTO events (name, category, start_time) VALUES (?, ?, ?)",
            ("open", "测试", datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        conn.close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tt.retention_show()  # 旧版在 SUM=NULL 时 TypeError 崩溃
        self.assertIn("数据保留策略", buf.getvalue())

    def test_summarize_refreshes_stale_summary(self):
        old = (date.today() - tt.timedelta(days=3)).isoformat()
        conn = tt.get_conn()
        for st, et in (("T10:00:00", "T11:00:00"),):
            conn.execute(
                "INSERT INTO events (name, category, start_time, end_time, duration_sec) "
                "VALUES (?, ?, ?, ?, ?)", ("a", "C", old + st, old + et, 3600))
        conn.commit()
        conn.close()
        with unittest.mock.patch.object(tt, "SUMMARY_RETAIN_DAYS", 0), \
                unittest.mock.patch.object(tt, "SUMMARY_RETAIN_MONTHS", 0):
            tt.summarize(verbose=False)
            # 同天补录一条后再次汇总，旧版会因 NOT IN 判定而冻结在 3600
            conn = tt.get_conn()
            conn.execute(
                "INSERT INTO events (name, category, start_time, end_time, duration_sec) "
                "VALUES (?, ?, ?, ?, ?)",
                ("b", "C", old + "T12:00:00", old + "T13:00:00", 3600))
            conn.commit()
            conn.close()
            tt.summarize(verbose=False)
        conn = tt.get_conn()
        row = conn.execute(
            "SELECT total_sec, event_count FROM daily_summaries WHERE day=?",
            (old,)).fetchone()
        conn.close()
        self.assertEqual(row, (7200, 2))

    def test_months_ago_no_month_zero(self):
        # 旧公式在整除时会生成 2026-00
        self.assertEqual(tt._months_ago_ym(date(2026, 1, 15), 1), "2025-12")
        self.assertEqual(tt._months_ago_ym(date(2026, 2, 15), 2), "2025-12")
        self.assertEqual(tt._months_ago_ym(date(2026, 3, 15), 1), "2026-02")
        self.assertEqual(tt._months_ago_ym(date(2026, 12, 15), 12), "2025-12")

    def test_status_shows_newest_open_event(self):
        import io, contextlib
        conn = tt.get_conn()
        now = datetime.now()
        conn.execute(
            "INSERT INTO events (name, category, start_time) VALUES (?, ?, ?)",
            ("OLD", "x", (now - tt.timedelta(hours=2)).isoformat(timespec="seconds")))
        conn.execute(
            "INSERT INTO events (name, category, start_time) VALUES (?, ?, ?)",
            ("NEW", "x", (now - tt.timedelta(minutes=5)).isoformat(timespec="seconds")))
        conn.commit()
        conn.close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tt.status()
        self.assertIn("NEW", buf.getvalue())

    def test_classify_accepts_none_exe(self):
        er, tr = tt.load_rules()
        cat, sub = tt.classify(None, "chatgpt 对话", er, tr)
        self.assertEqual(cat, "AI 工具")


class TestCustomRulesOverride(_TempDbTestCase):
    def test_custom_title_rule_overrides_default(self):
        rules = os.path.join(self.tmpdir, "rules.json")
        import json as _json
        with open(rules, "w", encoding="utf-8") as f:
            _json.dump({"title": [[["chatgpt"], "我的AI"]]}, f)
        with unittest.mock.patch.object(tt, "RULES_PATH", rules):
            er, tr = tt.load_rules()
        self.assertEqual(tt.subcategory_of("chatgpt 对话", er, tr), "我的AI")

    def test_malformed_title_entry_does_not_kill_others(self):
        rules = os.path.join(self.tmpdir, "rules.json")
        import json as _json
        with open(rules, "w", encoding="utf-8") as f:
            _json.dump({"exe": {"foo.exe": "我的类"},
                        "title": [["only-one"], [["ok"], "好类"]]}, f)
        with unittest.mock.patch.object(tt, "RULES_PATH", rules):
            er, tr = tt.load_rules()
        self.assertEqual(er.get("foo.exe"), "我的类")
        self.assertEqual(tt.subcategory_of("ok 窗口", er, tr), "好类")


class TestDayReportData(_TempDbTestCase):
    """原生窗口所用数据接口：不经浏览器/HTML 即可拿到全部展示数据。"""

    def test_day_report_data_shape(self):
        today = date.today().isoformat()
        conn = tt.get_conn()
        conn.execute(
            "INSERT INTO events (name, category, start_time, end_time, duration_sec, subcategory) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("写代码", "编程开发", today + "T10:00:00", today + "T11:00:00",
             3600, None))
        conn.commit()
        conn.close()
        data = tt.day_report_data(today)
        self.assertEqual(data["total"], 3600)
        self.assertEqual(data["sorted_cats"], [("编程开发", 3600)])
        self.assertEqual(len(data["trend"]), 7)
        self.assertEqual(data["trend"][-1]["day"], today)
        self.assertEqual(data["trend"][-1]["total"], 3600)
        self.assertFalse(data["is_summarized"])

    def test_day_report_data_empty_day(self):
        data = tt.day_report_data("2020-01-02")
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["items"], [])
        self.assertIsNone(data["longest"])


class TestPidFileVerification(_TempDbTestCase):
    def test_pid_file_roundtrip_and_garbage(self):
        path = os.path.join(self.tmpdir, "x.pid")
        tt._write_pid_file(path, 999999)  # 不存在的进程：读回应为 None
        self.assertIsNone(tt._read_pid_file(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write("not-a-pid")
        self.assertIsNone(tt._read_pid_file(path))
        self.assertIsNone(tt._read_pid_file(path + ".missing"))


if __name__ == "__main__":
    unittest.main()
