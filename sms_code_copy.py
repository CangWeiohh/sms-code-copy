#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sms-code-copy —— 监听 Mac 收到的短信验证码，自动复制到剪贴板。

工作原理：
  1. 以只读模式轮询 macOS「信息」数据库 ~/Library/Messages/chat.db；
  2. 新短信命中可配置关键词（默认：验证码 / 动态密码 / 验证密码）后，
     优先在关键词附近提取数字验证码（支持空格 / 连字符分隔，如 123 456）；
  3. 将验证码写入系统剪贴板（pbcopy），可选弹出 macOS 系统通知。

用法：
  python3 sms_code_copy.py                          # 前台运行
  python3 sms_code_copy.py --once                   # 只扫描一次后退出（用于测试）
  python3 sms_code_copy.py --config 其他路径/config.json
  python3 sms_code_copy.py --test "短信内容"          # 测试关键词匹配与验证码提取
  python3 sms_code_copy.py --reset-cursor           # 忽略历史短信，从最新一条开始
  python3 sms_code_copy.py --debug                  # 输出调试日志

权限：
  读取短信数据库需要「完全磁盘访问权限」：
  系统设置 → 隐私与安全性 → 完全磁盘访问权限，
  勾选运行本脚本的终端 App 或 Python 解释器（详见 README）。

说明：
  - 仅使用 Python 标准库，无第三方依赖（macOS 自带 python3 即可运行）；
  - 对短信数据库只读，绝不写入；
  - config.json 修改后自动热重载（关键词、轮询间隔等，无需重启）；
  - 本脚本与仓库不包含任何真实短信样例，测试请使用 --test 与虚构内容。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APP_NAME = "sms-code-copy"
VERSION = "1.0.0"

# 脚本所在目录（launchd 运行时以此定位默认配置）
SCRIPT_DIR = Path(__file__).resolve().parent

# 关键词命中后，在其后方多少个字符内寻找验证码
KEYWORD_WINDOW = 20

# Apple 参考纪元：2001-01-01 00:00:00 UTC（chat.db 的 date 字段基于此）
EPOCH_2001 = datetime(2001, 1, 1, tzinfo=timezone.utc)

logger = logging.getLogger(APP_NAME)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    # 命中任一关键词即尝试提取验证码（不区分大小写）
    "keywords": ["验证码", "动态密码", "验证密码", "校验码"],
    # 验证码数字位数范围（避免误抓手机号、日期等）
    "code_min_length": 4,
    "code_max_length": 8,
    # 自定义验证码正则（可选）。设置后优先于位数规则；
    # 若包含捕获组则取第 1 个捕获组，否则取整体匹配。
    "code_regex": "",
    # 轮询间隔（秒）
    "poll_interval": 3,
    # 短信数据库路径
    "db_path": "~/Library/Messages/chat.db",
    # 写入剪贴板的前后缀（默认只复制验证码本身）
    "copy_prefix": "",
    "copy_suffix": "",
    # 复制成功后是否弹 macOS 通知
    "notification": True,
    # 通知是否带提示音
    "notification_sound": False,
    # 首次运行（无历史游标）时回看最近 N 秒内的短信；0 = 跳过全部历史
    "first_run_max_age_seconds": 0,
    # 日志级别：DEBUG / INFO / WARNING / ERROR
    "log_level": "INFO",
    # 日志文件；相对路径基于 config.json 所在目录；留空表示不写文件
    "log_file": "logs/sms-code-copy.log",
}


class ConfigError(Exception):
    """配置加载或校验失败。"""


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并配置字典，override 优先。"""
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _validate_config(cfg: Dict[str, Any]) -> None:
    """校验配置合法性，不合法抛出 ConfigError。"""
    keywords = cfg.get("keywords")
    if isinstance(keywords, str):
        keywords = [keywords]
        cfg["keywords"] = keywords
    if not isinstance(keywords, list) or not [k for k in keywords if str(k).strip()]:
        raise ConfigError("keywords 必须是非空的关键词列表，如 [\"验证码\", \"动态密码\"]")

    def _int_in_range(name: str, low: int, high: int) -> None:
        value = cfg.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or not (low <= value <= high):
            raise ConfigError(f"{name} 必须是 {low}~{high} 之间的整数，当前为 {value!r}")

    _int_in_range("code_min_length", 2, 12)
    _int_in_range("code_max_length", 2, 12)
    if cfg["code_min_length"] > cfg["code_max_length"]:
        raise ConfigError("code_min_length 不能大于 code_max_length")

    interval = cfg.get("poll_interval")
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) \
            or not (0.5 <= float(interval) <= 3600):
        raise ConfigError("poll_interval 必须是 0.5~3600 之间的数字（秒）")

    for name in ("notification", "notification_sound"):
        if not isinstance(cfg.get(name), bool):
            raise ConfigError(f"{name} 必须是 true 或 false")

    max_age = cfg.get("first_run_max_age_seconds")
    if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 0 \
            or max_age > 86400 * 30:
        raise ConfigError("first_run_max_age_seconds 必须是 0~2592000 之间的整数（秒）")

    if not str(cfg.get("db_path") or "").strip():
        raise ConfigError("db_path 不能为空")

    level = str(cfg.get("log_level") or "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(f"log_level 不支持: {level!r}，可选 DEBUG/INFO/WARNING/ERROR")
    cfg["log_level"] = level

    code_regex = str(cfg.get("code_regex") or "")
    if code_regex:
        try:
            re.compile(code_regex)
        except re.error as exc:
            raise ConfigError(f"code_regex 正则无效: {exc}") from exc


def load_config(path: Path, create_default: bool = True) -> Dict[str, Any]:
    """
    加载配置文件并与默认值合并。

    文件不存在时默认自动按 DEFAULT_CONFIG 生成一份，便于首次运行后直接修改。
    """
    path = Path(path).expanduser()
    if not path.exists():
        if not create_default:
            raise ConfigError(f"配置文件不存在: {path}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"[初始化] 已生成默认配置文件: {path}", file=sys.stderr)
        except OSError as exc:
            raise ConfigError(f"无法创建默认配置文件 {path}: {exc}") from exc

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ConfigError(f"配置文件读取失败 {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件必须是 JSON 对象: {path}")

    cfg = _deep_merge(DEFAULT_CONFIG, raw)
    _validate_config(cfg)
    return cfg


# ---------------------------------------------------------------------------
# 短信数据库（只读）
# ---------------------------------------------------------------------------

# Apple 时间戳兼容：旧版 macOS 为秒，10.13+ 为纳秒，以 1e11 为阈值自动判断
def apple_time_to_datetime(value: Optional[float]) -> Optional[datetime]:
    """将 chat.db 的 date 字段转换为本地时区 datetime。"""
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    seconds = value / 1_000_000_000 if abs(value) > 1e11 else value
    return (EPOCH_2001 + timedelta(seconds=seconds)).astimezone()


@dataclass
class Message:
    """一条收到的短信。"""
    rowid: int                      # message.ROWID
    text: str                       # 短信正文
    sender: str                     # 发送方（handle.id，可能为空）
    received_at: Optional[datetime]  # 接收时间（本地时区）


_QUERY_NEW_MESSAGES = """
SELECT m.ROWID AS rowid,
       m.text AS text,
       m.date AS date,
       COALESCE(h.id, '') AS sender
FROM message m
LEFT JOIN handle h ON m.handle_id = h.ROWID
WHERE m.ROWID > ?
  AND m.is_from_me = 0
  AND m.text IS NOT NULL
  AND TRIM(m.text) != ''
ORDER BY m.ROWID ASC
"""

_QUERY_NEW_MESSAGES_BY_DATE = """
SELECT m.ROWID AS rowid,
       m.text AS text,
       m.date AS date,
       COALESCE(h.id, '') AS sender
FROM message m
LEFT JOIN handle h ON m.handle_id = h.ROWID
WHERE m.date >= ?
  AND m.is_from_me = 0
  AND m.text IS NOT NULL
  AND TRIM(m.text) != ''
ORDER BY m.ROWID ASC
"""

_QUERY_MAX_ROWID = "SELECT COALESCE(MAX(ROWID), 0) FROM message"


class MessageDatabase:
    """「信息」数据库的只读访问封装。"""

    def __init__(self, db_path: str):
        self._db_file = Path(db_path).expanduser().resolve()
        self._conn: Optional[sqlite3.Connection] = None
        self._opened_identity: Optional[Tuple] = None

    @property
    def opened_identity(self) -> Optional[Tuple]:
        """当前连接打开时对应的文件身份，用于识别数据库被替换。"""
        return self._opened_identity

    def identity(self) -> Tuple:
        """返回数据库文件的稳定身份（设备 / inode / 创建时间）。"""
        stat = self._db_file.stat()
        birth = int(getattr(stat, "st_birthtime", 0) * 1_000_000)
        return (stat.st_dev, stat.st_ino, birth)

    def connect(self) -> None:
        """以只读模式打开数据库（需「完全磁盘访问权限」）。"""
        if not self._db_file.exists():
            raise FileNotFoundError(
                f"短信数据库不存在: {self._db_file}\n"
                "请确认 iPhone 的「短信转发」已开启（设置 → 信息 → 短信转发 → 勾选本 Mac），"
                "或检查配置中的 db_path。"
            )
        # 数据库可能被「信息」原子替换，连接前后校验文件身份，最多重试 3 次
        for _ in range(3):
            identity_before = self.identity()
            uri = self._db_file.as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            identity_after = self.identity()
            if identity_before == identity_after:
                self._conn = conn
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA busy_timeout = 5000")
                self._opened_identity = identity_after
                return
            conn.close()
        raise sqlite3.OperationalError("短信数据库在连接过程中持续被替换，请稍后重试")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._opened_identity = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        return self._conn  # type: ignore[return-value]

    def get_max_rowid(self) -> int:
        """当前数据库最大 message ROWID（首次运行用于跳过历史短信）。"""
        row = self._require_conn().execute(_QUERY_MAX_ROWID).fetchone()
        return int(row[0]) if row else 0

    def fetch_new_messages(self, after_rowid: int) -> List[Message]:
        """获取 ROWID 大于 after_rowid 的收件短信（按 ROWID 升序）。"""
        rows = self._require_conn().execute(
            _QUERY_NEW_MESSAGES, (int(after_rowid),)
        ).fetchall()
        return self._rows_to_messages(rows)

    def fetch_messages_since_date(self, cutoff: float) -> List[Message]:
        """获取 date 字段不小于 cutoff 的收件短信（首次运行回看用）。"""
        rows = self._require_conn().execute(
            _QUERY_NEW_MESSAGES_BY_DATE, (cutoff,)
        ).fetchall()
        return self._rows_to_messages(rows)

    def detect_date_unit(self) -> Optional[str]:
        """
        探测 date 字段单位：返回 'ns' / 's'；数据库为空时返回 None。
        """
        row = self._require_conn().execute(
            "SELECT date FROM message WHERE date IS NOT NULL "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return "ns" if abs(float(row[0])) > 1e11 else "s"

    @staticmethod
    def _rows_to_messages(rows) -> List[Message]:
        messages: List[Message] = []
        for r in rows:
            messages.append(
                Message(
                    rowid=int(r["rowid"]),
                    text=str(r["text"] or ""),
                    sender=str(r["sender"] or ""),
                    received_at=apple_time_to_datetime(r["date"]),
                )
            )
        return messages


# ---------------------------------------------------------------------------
# 关键词匹配与验证码提取
# ---------------------------------------------------------------------------

def build_keyword_pattern(keywords: List[str]) -> re.Pattern:
    """把关键词列表编译为不区分大小写的「或」正则。"""
    parts = [re.escape(str(k).strip()) for k in keywords if str(k).strip()]
    if not parts:
        raise ConfigError("keywords 不能为空")
    return re.compile("|".join(parts), re.IGNORECASE)


def build_code_pattern(min_len: int, max_len: int) -> re.Pattern:
    """
    构造验证码数字正则。

    - 数字位数限制在 [min_len, max_len]；
    - 允许数字间出现单个空格或连字符（如 "123 456"、"123-456"）；
    - 前后向断言保证不会被更长的数字串（手机号、订单号等）截段匹配。
    """
    min_len = max(2, min(int(min_len), int(max_len)))
    max_len = min(12, max(int(min_len), int(max_len)))
    return re.compile(
        r"(?<!\d)\d(?:[\s\-]?\d){%d,%d}(?!\d)" % (min_len - 1, max_len - 1)
    )


def _normalize_code(raw: str) -> str:
    """去掉验证码中的空格 / 连字符。"""
    return re.sub(r"[\s\-]+", "", raw or "").strip()


def extract_code(text: str,
                 keyword_pattern: re.Pattern,
                 min_len: int = 4,
                 max_len: int = 8,
                 custom_regex: str = "") -> Optional[str]:
    """
    从短信文本中提取验证码。

    策略：
      1. 若配置了 custom_regex，则直接用它匹配（含捕获组取第 1 组）；
      2. 优先取每个关键词后方 KEYWORD_WINDOW 字符内的第一个验证码，
         多个命中时取离关键词最近的；
      3. 兜底：在全文验证码候选中取离任一关键词最近的。
    """
    text = text or ""
    if not text:
        return None

    if custom_regex:
        m = re.search(custom_regex, text, re.IGNORECASE)
        if m is None:
            return None
        # 自定义正则的匹配结果原样返回（不做分隔符清洗）
        raw = m.group(1) if m.re.groups else m.group(0)
        return (raw or "").strip()

    code_pat = build_code_pattern(min_len, max_len)

    # 1) 关键词附近优先
    near_hits: List[Tuple[int, int, str]] = []
    kw_ends: List[int] = []
    for m in keyword_pattern.finditer(text):
        kw_end = m.end()
        kw_ends.append(kw_end)
        window_end = min(len(text), kw_end + KEYWORD_WINDOW)
        hit = code_pat.search(text, kw_end, window_end)
        if hit:
            near_hits.append((hit.start() - kw_end, hit.start(), hit.group(0)))
    if near_hits:
        near_hits.sort(key=lambda item: (item[0], item[1]))
        return _normalize_code(near_hits[0][2])

    # 2) 全文兜底：取离关键词最近的验证码候选
    if not kw_ends:
        return None
    candidates = list(code_pat.finditer(text))
    if not candidates:
        return None

    def _distance(hit: "re.Match") -> Tuple[int, int]:
        nearest = min(abs(hit.start() - p) for p in kw_ends)
        return (nearest, hit.start())

    candidates.sort(key=_distance)
    return _normalize_code(candidates[0].group(0))


# ---------------------------------------------------------------------------
# 剪贴板与系统通知
# ---------------------------------------------------------------------------

def _tool_path(name: str) -> str:
    """定位系统工具的绝对路径，找不到时退回原始名称。"""
    return shutil.which(name) or f"/usr/bin/{name}"


def _applescript_escape(text: str) -> str:
    """转义 AppleScript 字符串中的反斜杠与双引号。"""
    return (text or "").replace("\\", "\\\\").replace('"', '\\"')


def copy_to_clipboard(text: str) -> bool:
    """把文本写入系统剪贴板；优先 pbcopy，失败时退回 osascript。"""
    text = text or ""
    try:
        proc = subprocess.run(
            [_tool_path("pbcopy")],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return True
        logger.warning("pbcopy 返回码 %s，尝试 osascript 兜底", proc.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("pbcopy 调用失败: %s，尝试 osascript 兜底", exc)

    try:
        script = f'set the clipboard to "{_applescript_escape(text)}"'
        proc = subprocess.run(
            [_tool_path("osascript"), "-e", script],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("osascript 写剪贴板也失败: %s", exc)
        return False


def send_notification(title: str, message: str, sound: bool = False) -> bool:
    """弹出一则 macOS 通知；失败仅记录日志，不影响主流程。"""
    script = f'display notification "{_applescript_escape(message)}" with title "{_applescript_escape(title)}"'
    if sound:
        script += ' sound name "Glass"'
    try:
        proc = subprocess.run(
            [_tool_path("osascript"), "-e", script],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("发送系统通知失败: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 游标状态（记录最后处理的 ROWID，避免重复复制）
# ---------------------------------------------------------------------------

class StateStore:
    """state.json 的原子读写（临时文件 + os.replace）。"""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()

    def load(self) -> Optional[int]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("读取状态文件失败，将重新初始化: %s", exc)
            return None
        if not isinstance(data, dict):
            return None
        try:
            value = int(data.get("last_rowid", 0))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def save(self, rowid: int) -> None:
        tmp_path = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            tmp_path.write_text(
                json.dumps({"last_rowid": int(rowid)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, self.path)
        except OSError as exc:
            logger.warning("写入状态文件失败（不影响运行）: %s", exc)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------

class SmsCodeWatcher:
    """轮询短信数据库，命中关键词后提取验证码并复制到剪贴板。"""

    def __init__(self,
                 config_path: Path,
                 state_path: Optional[Path] = None,
                 reset_cursor: bool = False):
        self.config_path = Path(config_path).expanduser().resolve()
        self.config_dir = self.config_path.parent
        self.cfg = load_config(self.config_path)
        self._config_mtime: Optional[float] = self._stat_mtime()
        self._kw_pat = build_keyword_pattern(self.cfg["keywords"])
        self._code_pat = build_code_pattern(
            self.cfg["code_min_length"], self.cfg["code_max_length"]
        )
        self.state = StateStore(
            Path(state_path) if state_path else self.config_dir / "state.json"
        )
        self._db: Optional[MessageDatabase] = None
        self._last_rowid = 0
        self._cursor_ready = False
        self._reset_cursor = reset_cursor
        self._stop = False
        self._warn_unknown_keys()

    # -- 配置 ---------------------------------------------------------------

    def _stat_mtime(self) -> Optional[float]:
        try:
            return self.config_path.stat().st_mtime
        except OSError:
            return None

    def _warn_unknown_keys(self) -> None:
        unknown = sorted(set(self.cfg) - set(DEFAULT_CONFIG))
        if unknown:
            logger.warning("配置文件中存在未识别的配置项（已忽略）: %s", ", ".join(unknown))

    def _reload_config_if_changed(self) -> None:
        """config.json 被修改时热重载（db_path 等需重启的项仅提示）。"""
        mtime = self._stat_mtime()
        if mtime is None or mtime == self._config_mtime:
            return
        try:
            new_cfg = load_config(self.config_path, create_default=False)
        except ConfigError as exc:
            logger.error("配置变更但重新加载失败，继续使用旧配置: %s", exc)
            self._config_mtime = mtime  # 避免每轮重复报错
            return

        if new_cfg["db_path"] != self.cfg["db_path"]:
            logger.warning("db_path 变更需重启进程后生效")
        self.cfg = new_cfg
        self._kw_pat = build_keyword_pattern(new_cfg["keywords"])
        self._code_pat = build_code_pattern(
            new_cfg["code_min_length"], new_cfg["code_max_length"]
        )
        self._config_mtime = mtime
        self._warn_unknown_keys()
        logger.info(
            "检测到配置变更，已热重载: 关键词=%s 轮询=%ss 验证码位数=%d~%d",
            new_cfg["keywords"], new_cfg["poll_interval"],
            new_cfg["code_min_length"], new_cfg["code_max_length"],
        )

    # -- 数据库 --------------------------------------------------------------

    def _connect_db(self) -> None:
        self._db = MessageDatabase(self.cfg["db_path"])
        self._db.connect()

    def _reconnect(self, reset_cursor: bool = False) -> None:
        if self._db is not None:
            self._db.close()
        self._connect_db()
        if reset_cursor:
            self._last_rowid = self._db.get_max_rowid()
            self.state.save(self._last_rowid)
            logger.warning("短信数据库已替换，游标重置为 ROWID=%d（跳过历史）", self._last_rowid)

    def _ensure_db_current(self) -> None:
        """识别 chat.db 被替换（文件身份变化），必要时重建只读连接。"""
        try:
            current = self._db.identity()
        except OSError:
            return  # 文件暂时不可见，下一轮再检查
        if current != self._db.opened_identity:
            logger.warning("检测到短信数据库文件被替换，正在重新建立只读连接")
            self._reconnect(reset_cursor=True)

    # -- 游标 ----------------------------------------------------------------

    def _init_cursor(self) -> int:
        """
        初始化扫描游标：优先恢复上次位置，否则跳过历史。

        :return: 初始化过程中（首次回看）复制的验证码条数
        """
        assert self._db is not None
        copied = 0
        saved = None if self._reset_cursor else self.state.load()
        max_rowid = self._db.get_max_rowid()

        if saved is not None:
            if saved > max_rowid:
                logger.warning(
                    "状态文件中的游标(%d)大于数据库最大 ROWID(%d)，"
                    "短信数据库可能已重建，游标将重置",
                    saved, max_rowid,
                )
                self._last_rowid = max_rowid
            else:
                self._last_rowid = saved
                logger.info("已恢复游标，从 ROWID=%d 之后继续监听", saved)
        else:
            self._last_rowid = max_rowid
            max_age = int(self.cfg["first_run_max_age_seconds"] or 0)
            if max_age > 0 and max_rowid > 0:
                logger.info("首次运行: 回看最近 %d 秒内的短信", max_age)
                copied = self._handle_recent_messages(max_age)
            else:
                logger.info("首次运行: 跳过历史短信，从 ROWID=%d 开始监听", max_rowid)
            self.state.save(max_rowid)

        self._cursor_ready = True
        return copied

    def _handle_recent_messages(self, max_age_seconds: int) -> int:
        """首次运行时按时间回看最近的短信（不依赖 ROWID 游标）。"""
        unit = self._db.detect_date_unit()
        if unit is None:
            return 0
        now_since_2001 = (datetime.now(timezone.utc) - EPOCH_2001).total_seconds()
        cutoff = (now_since_2001 - max_age_seconds) * (1_000_000_000 if unit == "ns" else 1)
        messages = self._db.fetch_messages_since_date(cutoff)
        logger.info("首次运行回看: 共 %d 条近期短信", len(messages))
        return self._handle_messages(messages)

    # -- 核心处理 --------------------------------------------------------------

    def _handle_messages(self, messages: List[Message]) -> int:
        """对一批短信做关键词匹配、验证码提取与剪贴板复制，返回复制条数。"""
        copied = 0
        for msg in messages:
            kw_match = self._kw_pat.search(msg.text)
            if kw_match is None:
                continue

            code = extract_code(
                msg.text,
                self._kw_pat,
                self.cfg["code_min_length"],
                self.cfg["code_max_length"],
                self.cfg["code_regex"],
            )
            if not code:
                logger.info(
                    "命中关键词[%s]但未提取到验证码: ROWID=%d 来源=%s",
                    kw_match.group(0), msg.rowid, msg.sender or "未知",
                )
                continue

            value = f"{self.cfg['copy_prefix']}{code}{self.cfg['copy_suffix']}"
            if copy_to_clipboard(value):
                copied += 1
                received = (
                    msg.received_at.strftime("%Y-%m-%d %H:%M:%S")
                    if msg.received_at else "未知时间"
                )
                logger.info(
                    "已复制验证码: ROWID=%d 来源=%s 时间=%s 关键词=%s 验证码=%s",
                    msg.rowid, msg.sender or "未知", received,
                    kw_match.group(0), code,
                )
                if self.cfg["notification"]:
                    send_notification(
                        APP_NAME,
                        f"已复制验证码 {code}（来自 {msg.sender or '未知'}）",
                        sound=bool(self.cfg["notification_sound"]),
                    )
            else:
                logger.error("写入剪贴板失败: 验证码=%s", code)
        return copied

    def poll_once(self) -> int:
        """扫描一次新短信并处理，返回本次复制的验证码条数。"""
        self._reload_config_if_changed()
        if self._db is None:
            self._connect_db()
        copied = 0
        if not self._cursor_ready:
            copied += self._init_cursor()
        self._ensure_db_current()

        messages = self._db.fetch_new_messages(self._last_rowid)
        copied += self._handle_messages(messages)
        if messages:
            self._last_rowid = messages[-1].rowid
            self.state.save(self._last_rowid)
        logger.debug("本轮扫描 %d 条新短信，复制 %d 个验证码", len(messages), copied)
        return copied

    # -- 主循环 ----------------------------------------------------------------

    def run(self, once: bool = False) -> None:
        """主循环；once=True 时只扫描一次（用于测试）。"""
        self._connect_db()
        logger.info(
            "%s v%s 启动: 轮询间隔=%ss 关键词=%s 验证码位数=%d~%d 通知=%s",
            APP_NAME, VERSION, self.cfg["poll_interval"], self.cfg["keywords"],
            self.cfg["code_min_length"], self.cfg["code_max_length"],
            self.cfg["notification"],
        )
        while not self._stop:
            try:
                self.poll_once()
            except sqlite3.Error as exc:
                logger.error("短信数据库访问出错，将重连后重试: %s", exc)
                try:
                    self._reconnect()
                except Exception:  # noqa: BLE001
                    logger.exception("数据库重连失败，下一轮继续尝试")
            except Exception:  # noqa: BLE001 - 守护进程不因单轮异常退出
                logger.exception("轮询处理出错（已跳过本轮）")

            if once or self._stop:
                break
            self._sleep(float(self.cfg["poll_interval"]))

    def _sleep(self, seconds: float) -> None:
        """分片睡眠，保证收到退出信号后能及时响应。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.25, remaining))

    def request_stop(self) -> None:
        """请求停止（供信号处理器调用）。"""
        self._stop = True

    def close(self) -> None:
        if self._db is not None:
            self._db.close()


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def setup_logging(cfg: Dict[str, Any], config_dir: Path, debug: bool) -> None:
    """初始化日志：控制台 + 可选滚动文件。"""
    level = logging.DEBUG if debug else getattr(logging, cfg["log_level"], logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    ))
    root.addHandler(console)

    log_file = str(cfg.get("log_file") or "").strip()
    if log_file:
        log_path = Path(log_file).expanduser()
        if not log_path.is_absolute():
            log_path = config_dir / log_path
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
            )
            file_handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s"
            ))
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("无法创建日志文件 %s: %s", log_path, exc)


def run_test_mode(config_path: Path, text: str) -> int:
    """--test 模式：对给定文本执行关键词匹配与验证码提取（不影响游标）。"""
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    kw_pat = build_keyword_pattern(cfg["keywords"])
    kw_match = kw_pat.search(text)
    if kw_match:
        print(f"命中关键词: {kw_match.group(0)}")
    else:
        print(f"未命中任何关键词（当前关键词: {cfg['keywords']}）")

    code = extract_code(
        text, kw_pat,
        cfg["code_min_length"], cfg["code_max_length"], cfg["code_regex"],
    )
    if not code:
        print("未能提取到验证码")
        return 1

    print(f"提取到验证码: {code}")
    value = f"{cfg['copy_prefix']}{code}{cfg['copy_suffix']}"
    if copy_to_clipboard(value):
        print(f"已复制到剪贴板: {value}")
        return 0
    print("复制到剪贴板失败", file=sys.stderr)
    return 1


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="监听 Mac 收到的短信验证码并自动复制到剪贴板",
        epilog=(
            "示例:\n"
            "  %(prog)s --test '【示例服务】您的验证码为654321，10分钟内有效。'\n"
            "  %(prog)s --once --debug\n"
            "配置说明见 README.md；config.json 修改后自动热重载。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.json"),
                        help="配置文件路径（默认: 脚本目录下 config.json）")
    parser.add_argument("--once", action="store_true",
                        help="只扫描一次后退出（用于测试）")
    parser.add_argument("--test", metavar="TEXT",
                        help="对给定文本测试关键词匹配与验证码提取，并复制结果")
    parser.add_argument("--reset-cursor", action="store_true",
                        help="忽略已保存的游标，从最新短信开始监听")
    parser.add_argument("--debug", action="store_true", help="输出 DEBUG 级日志")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()

    # --test 模式：独立处理，不启动轮询
    if args.test is not None:
        return run_test_mode(config_path, args.test)

    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 1

    setup_logging(cfg, config_path.parent, debug=args.debug)
    logger.info("=" * 56)
    logger.info("配置文件: %s", config_path)

    watcher = SmsCodeWatcher(config_path, reset_cursor=args.reset_cursor)

    def _handle_signal(signum, _frame):
        logger.info("收到信号 %s，正在退出…", signum)
        watcher.request_stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        watcher.run(once=args.once)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 2
    except (sqlite3.Error, PermissionError) as exc:
        logger.error("短信数据库打开失败: %s", exc)
        logger.error(
            "请检查「完全磁盘访问权限」：\n"
            "  系统设置 → 隐私与安全性 → 完全磁盘访问权限 →\n"
            "  勾选运行本脚本的终端 App 或 Python 解释器（真实路径可用下述命令查询）:\n"
            "  python3 -c 'import sys; print(sys.executable)'"
        )
        return 2
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1
    finally:
        watcher.close()
        logger.info("%s 已退出", APP_NAME)

    return 0


if __name__ == "__main__":
    sys.exit(main())
