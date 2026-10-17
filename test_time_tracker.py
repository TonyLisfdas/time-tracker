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


if __name__ == "__main__":
    unittest.main()
