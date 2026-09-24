#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sms_code_copy 的单元测试。

注意：本文件中的全部短信内容均为虚构的测试样例，不包含任何真实短信。
运行：python3 -m unittest discover -s tests -v
"""

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import sms_code_copy as scc  # noqa: E402

KEYWORDS = ["验证码", "动态密码", "验证密码"]


def extract(text, keywords=None, min_len=4, max_len=8, custom_regex=""):
    """便捷封装：编译关键词正则后提取验证码。"""
    pattern = scc.build_keyword_pattern(keywords or KEYWORDS)
    return scc.extract_code(text, pattern, min_len, max_len, custom_regex)


# ---------------------------------------------------------------------------
# 验证码提取
# ---------------------------------------------------------------------------

class ExtractionTests(unittest.TestCase):
    """验证码提取逻辑（全部使用虚构短信）。"""

    def test_code_right_after_keyword(self):
        text = "【示例服务】您的验证码为654321，10分钟内有效，请勿泄露。"
        self.assertEqual(extract(text), "654321")

    def test_code_with_colon(self):
        text = "【示例银行】您的动态密码：778899，5分钟内有效。"
        self.assertEqual(extract(text), "778899")

    def test_nearest_code_wins(self):
        # 关键词出现两次，验证码离第二个关键词更近
        text = "（验证密码）您正在登录示例邮箱，动态密码：112233，10分钟内有效。"
        self.assertEqual(extract(text), "112233")

    def test_spaced_code_normalized(self):
        text = "【示例平台】验证码 445 566，请勿转发。"
        self.assertEqual(extract(text), "445566")

    def test_dashed_code_normalized(self):
        text = "【示例平台】验证码是 445-566，请查收。"
        self.assertEqual(extract(text), "445566")

    def test_phone_number_not_matched(self):
        text = "您的验证码为998877，如需帮助请联系客服 13800138000。"
        self.assertEqual(extract(text), "998877")

    def test_phone_number_only_no_match(self):
        text = "请拨打客服电话 13800138000 咨询。"
        self.assertIsNone(extract(text))

    def test_date_not_preferred_over_near_code(self):
        text = "验证码556677（2025-01-15前有效），请及时使用。"
        self.assertEqual(extract(text), "556677")

    def test_no_digits(self):
        self.assertIsNone(extract("您好，欢迎注册示例平台。"))

    def test_fallback_nearest_to_keyword(self):
        # 验证码超出关键词窗口，走全文兜底
        text = "您正在申请示例服务会员资格，请仔细核对个人信息后输入验证码 314159 完成校验。"
        self.assertEqual(extract(text), "314159")

    def test_case_insensitive_english_keyword(self):
        text = "Your code is 24680, do not share it."
        self.assertEqual(extract(text, keywords=["code"]), "24680")

    def test_custom_regex_with_group(self):
        text = "示例通行码：AB-42CD，有效期5分钟。"
        self.assertEqual(
            extract(text, custom_regex=r"通行码[:：]\s*([A-Z0-9\-]+)"),
            "AB-42CD",
        )

    def test_custom_regex_no_match(self):
        self.assertIsNone(extract("没有验证码的内容", custom_regex=r"\d{6}"))

    def test_min_length_filters_short_digits(self):
        text = "验证码 98，请在 5 分钟内使用。"
        self.assertIsNone(extract(text, min_len=4, max_len=8))


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write(self, data):
        path = self.dir / "config.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_load_creates_default_when_missing(self):
        path = self.dir / "sub" / "config.json"
        cfg = scc.load_config(path)
        self.assertTrue(path.exists())
        self.assertEqual(cfg["keywords"], scc.DEFAULT_CONFIG["keywords"])

    def test_keywords_as_string_accepted(self):
        cfg = scc.load_config(self._write({"keywords": "验证码"}))
        self.assertEqual(cfg["keywords"], ["验证码"])

    def test_empty_keywords_rejected(self):
        with self.assertRaises(scc.ConfigError):
            scc.load_config(self._write({"keywords": []}))

    def test_bad_regex_rejected(self):
        with self.assertRaises(scc.ConfigError):
            scc.load_config(self._write({"code_regex": "("}))

    def test_min_greater_than_max_rejected(self):
        with self.assertRaises(scc.ConfigError):
            scc.load_config(self._write({"code_min_length": 8, "code_max_length": 4}))


# ---------------------------------------------------------------------------
# 轮询与复制（使用临时目录中的虚构数据库）
# ---------------------------------------------------------------------------

def _apple_ns(dt: datetime) -> int:
    """把 UTC datetime 转成 chat.db 的纳秒时间戳。"""
    return int((dt - scc.EPOCH_2001).total_seconds() * 1_000_000_000)


def make_fixture_db(path: Path, rows):
    """创建一个最小可用的 chat.db 结构并插入虚构短信。rows: (rowid, text, dt, is_from_me)"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
    conn.execute(
        "CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, "
        "date INTEGER, is_from_me INTEGER, handle_id INTEGER)"
    )
    conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '10690000000')")
    for rowid, text, dt, is_from_me in rows:
        conn.execute(
            "INSERT INTO message (ROWID, text, date, is_from_me, handle_id) "
            "VALUES (?, ?, ?, ?, 1)",
            (rowid, text, _apple_ns(dt), is_from_me),
        )
    conn.commit()
    conn.close()


class WatcherTests(unittest.TestCase):
    MATCHING_TEXT = "【示例服务】您的验证码为654321，10分钟内有效。"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.copied = []
        self._orig_copy = scc.copy_to_clipboard
        self._orig_notify = scc.send_notification
        scc.copy_to_clipboard = lambda text: (self.copied.append(text) or True)
        scc.send_notification = lambda *a, **k: True
        self.addCleanup(setattr, scc, "copy_to_clipboard", self._orig_copy)
        self.addCleanup(setattr, scc, "send_notification", self._orig_notify)

    def make_watcher(self, **cfg_overrides):
        db_path = self.tmp / "chat.db"
        config = {
            "db_path": str(db_path),
            "log_file": "",
            "notification": False,
            "poll_interval": 1,
        }
        config.update(cfg_overrides)
        config_path = self.tmp / "config.json"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return scc.SmsCodeWatcher(
            config_path, state_path=self.tmp / "state.json"
        )

    def make_db(self, rows):
        make_fixture_db(self.tmp / "chat.db", rows)

    def test_processes_new_message_and_copies(self):
        now = datetime.now(timezone.utc)
        self.make_db([
            (1, "你好，这是一条普通短信。", now - timedelta(seconds=120), 0),
            (2, self.MATCHING_TEXT, now - timedelta(seconds=60), 0),
            (3, "【示例服务】我发出的短信不算", now - timedelta(seconds=30), 1),
        ])
        # 预置游标 = 1，使 rowid=2 成为“新短信”
        (self.tmp / "state.json").write_text('{"last_rowid": 1}', encoding="utf-8")

        watcher = self.make_watcher()
        copied = watcher.poll_once()

        self.assertEqual(copied, 1)
        self.assertEqual(self.copied, ["654321"])
        self.assertEqual(watcher._last_rowid, 2)
        self.assertEqual((self.tmp / "state.json").exists(), True)

    def test_first_run_skips_history_by_default(self):
        now = datetime.now(timezone.utc)
        self.make_db([
            (1, self.MATCHING_TEXT, now - timedelta(seconds=60), 0),
        ])

        watcher = self.make_watcher()
        copied = watcher.poll_once()

        self.assertEqual(copied, 0)
        self.assertEqual(self.copied, [])
        self.assertEqual(watcher._last_rowid, 1)  # 游标直接推进到最新

    def test_first_run_lookback_window(self):
        now = datetime.now(timezone.utc)
        self.make_db([
            (1, self.MATCHING_TEXT, now - timedelta(seconds=3600), 0),  # 太旧
            (2, "【示例银行】动态密码：112233，5分钟内有效。",
             now - timedelta(seconds=60), 0),                            # 窗口内
        ])

        watcher = self.make_watcher(first_run_max_age_seconds=300)
        copied = watcher.poll_once()

        self.assertEqual(copied, 1)
        self.assertEqual(self.copied, ["112233"])

    def test_cursor_restored_from_state(self):
        now = datetime.now(timezone.utc)
        self.make_db([
            (1, self.MATCHING_TEXT, now - timedelta(seconds=60), 0),
            (2, "【示例银行】动态密码：223344，5分钟内有效。",
             now - timedelta(seconds=30), 0),
        ])
        (self.tmp / "state.json").write_text('{"last_rowid": 1}', encoding="utf-8")

        watcher = self.make_watcher()
        watcher.poll_once()

        self.assertEqual(self.copied, ["223344"])  # rowid=1 已处理过，不重复复制

    def test_keyword_match_but_no_code(self):
        now = datetime.now(timezone.utc)
        self.make_db([
            (1, "您的验证申请已收到，请耐心等待审核结果。", now, 0),
        ])
        (self.tmp / "state.json").write_text('{"last_rowid": 0}', encoding="utf-8")

        watcher = self.make_watcher()
        copied = watcher.poll_once()

        self.assertEqual(copied, 0)
        self.assertEqual(self.copied, [])


# ---------------------------------------------------------------------------
# 状态文件
# ---------------------------------------------------------------------------

class StateStoreTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = scc.StateStore(Path(tmp) / "state.json")
            self.assertIsNone(store.load())
            store.save(12345)
            self.assertEqual(store.load(), 12345)

    def test_corrupted_state_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("not json", encoding="utf-8")
            store = scc.StateStore(path)
            self.assertIsNone(store.load())


if __name__ == "__main__":
    unittest.main()
