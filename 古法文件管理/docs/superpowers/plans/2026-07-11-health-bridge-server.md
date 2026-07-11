# Health-Bridge 服务端实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 VPS 上部署 FastAPI 服务，接收推送客户端上传的 Gadgetbridge 数据库快照，解析心率/睡眠/步数三类数据，生成 ISO 周 Markdown 归档，并通过 Nginx 反代对外提供 HTTPS 查询接口。

**Architecture:** FastAPI 绑定 127.0.0.1:8765，Nginx 反代 /health/ 路径。服务端使用 SQLite 存储归一化观测数据和摄入元数据。Gadgetbridge schema 适配器将 Xiaomi protobuf 设备（Mi Band 8）的多表数据映射为统一观测模型。归档生成器在每次成功导入后重新生成受影响周的 Markdown 文件。

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, SQLite3 (标准库), systemd, Nginx 1.18

## Global Constraints

- 时区: Asia/Shanghai (UTC+8)，ISO 周从周一到周日
- 设备: Xiaomi Smart Band 8 (MIBAND8)，device_id=1，user_id=1
- 令牌: 上传令牌 `X-Upload-Token`，读取令牌 `Authorization: Bearer`，常量时间比较，永不写入代码仓库
- 安全: HTTPS 强制，SQLite magic 校验 + PRAGMA quick_check + 解压大小限制
- 数据库: 服务端 SQLite，不入 PostgreSQL/消息队列/容器
- 日志: 不记录令牌、完整响应体、敏感健康数据行
- 原子写入: 状态文件、归档文件、latest JSON 均使用临时文件 + os.replace
- 健康数据是描述性的，非诊断性，不生成医学结论

## 实际 Gadgetbridge.db Schema 检查结果

以下基于对 `/tmp/Gadgetbridge.db`（Gadgetbridge 0.92.1，Mi Band 8）的实际检查：

### DEVICE 表
```
_id=1, NAME='Xiaomi Smart Band 8 2194', MANUFACTURER='Xiaomi',
IDENTIFIER='D0:62:2C:4E:21:94', TYPE=0, TYPE_NAME='MIBAND8', MODEL='M2239B1'
```

### USER 表
```
_id=1, NAME='gadgetbridge-user', BIRTHDAY=946708853174, GENDER=2
```

### XIAOMI_ACTIVITY_SAMPLE（主数据表，115 行）
| 列名 | 类型 | 说明 |
|------|------|------|
| TIMESTAMP | INTEGER | Unix epoch 秒 |
| DEVICE_ID | INTEGER | 外键 -> DEVICE._id |
| USER_ID | INTEGER | 外键 -> USER._id |
| RAW_INTENSITY | INTEGER | 原始强度 |
| STEPS | INTEGER | 步数（每分钟采样） |
| RAW_KIND | INTEGER | 活动类型（当前全为 0） |
| HEART_RATE | INTEGER | 心率 BPM（0 表示无数据） |
| STRESS | INTEGER | 压力值 |
| SPO2 | INTEGER | 血氧 |
| DISTANCE_CM | INTEGER | 距离厘米 |
| ACTIVE_CALORIES | INTEGER | 活动卡路里 |
| ENERGY | INTEGER | 能量 |

- 时间范围: 2026-07-11 12:47 ~ 14:41 (Asia/Shanghai)
- 心率: 8 行有 HEART_RATE > 0（79-94 BPM）
- 步数: 0 行有 STEPS > 0（schema 存在，当前无步数数据）
- 采样间隔: 约 1-5 分钟不等

### XIAOMI_DAILY_SUMMARY_SAMPLE（每日汇总，1 行）
| 列名 | 类型 | 说明 |
|------|------|------|
| TIMESTAMP | INTEGER | Unix epoch **毫秒**（注意单位不同） |
| DEVICE_ID, USER_ID | INTEGER | 外键 |
| TIMEZONE | INTEGER | 时区，32 = UTC+8（32 × 15 分钟 = 480 分钟） |
| STEPS | INTEGER | 每日总步数 |
| HR_RESTING, HR_MAX, HR_MIN, HR_AVG | INTEGER | 每日心率汇总 |
| HR_MAX_TS, HR_MIN_TS | INTEGER | 最高/最低心率时间戳（秒） |
| STRESS_AVG, STRESS_MAX, STRESS_MIN | INTEGER | 每日压力汇总 |
| CALORIES | INTEGER | 每日卡路里 |
| SPO2_MAX, SPO2_MIN, SPO2_AVG | INTEGER | 每日血氧汇总 |
| STANDING | INTEGER | 站立时间 |

### XIAOMI_SLEEP_TIME_SAMPLE（睡眠会话，0 行，schema 已就绪）
| 列名 | 类型 | 说明 |
|------|------|------|
| TIMESTAMP | INTEGER | 会话开始时间（秒） |
| DEVICE_ID, USER_ID | INTEGER | 外键 |
| WAKEUP_TIME | INTEGER | 醒来时间（秒） |
| IS_AWAKE | INTEGER | 是否清醒 |
| TOTAL_DURATION | INTEGER | 总时长（秒） |
| DEEP_SLEEP_DURATION | INTEGER | 深睡时长 |
| LIGHT_SLEEP_DURATION | INTEGER | 浅睡时长 |
| REM_SLEEP_DURATION | INTEGER | REM 时长 |
| AWAKE_DURATION | INTEGER | 清醒时长 |

### XIAOMI_SLEEP_STAGE_SAMPLE（睡眠分期，0 行，schema 已就绪）
| 列名 | 类型 | 说明 |
|------|------|------|
| TIMESTAMP | INTEGER | 分期时间戳（秒） |
| DEVICE_ID, USER_ID | INTEGER | 外键 |
| STAGE | INTEGER | 睡眠阶段代码 |

### 数据映射策略

| 目标类型 | 源表 | 提取条件 | 时间戳单位 |
|----------|------|----------|------------|
| heart_rate | XIAOMI_ACTIVITY_SAMPLE | WHERE HEART_RATE > 0 | 秒 |
| steps | XIAOMI_ACTIVITY_SAMPLE | WHERE STEPS > 0（逐采样点） | 秒 |
| steps_daily | XIAOMI_DAILY_SUMMARY_SAMPLE | 全量（每日汇总） | 毫秒 |
| sleep_session | XIAOMI_SLEEP_TIME_SAMPLE | 全量 | 秒 |
| sleep_stage | XIAOMI_SLEEP_STAGE_SAMPLE | 全量 | 秒 |

注意：当前数据库中步数和睡眠数据为空行，但 schema 已就绪。适配器必须实现对这三类数据的完整映射，使用合成 fixture 测试，待真实数据到达后验证。

---

## 文件结构

```
/srv/health-bridge/
  app/
    __init__.py
    main.py              # FastAPI 应用入口，路由定义
    config.py            # 服务端配置（令牌、路径、大小限制）
    database.py           # SQLite 数据库初始化、迁移、连接管理
    auth.py              # 令牌校验（常量时间比较）
    ingest.py            # 上传处理管线：接收 → 校验 → 解压 → 导入
    schema_inspector.py  # Gadgetbridge schema 探测和指纹
    device_resolver.py   # 设备/用户 ID 映射为稳定内部标识
    adapters/
      __init__.py
      base.py            # 适配器基类和注册机制
      heart_rate.py      # 心率适配器
      steps.py           # 步数适配器
      sleep.py           # 睡眠适配器
    normalizer.py        # 统一观测模型和去重键生成
    archive.py           # ISO 周 Markdown 归档生成器
    latest.py            # latest JSON 文件原子生成
    models.py            # Pydantic 请求/响应模型
  data/
    incoming/            # 临时上传文件
    raw/                 # 已接受的原始快照
    health.sqlite3       # 归一化数据库
  archives/              # Markdown 周归档
  latest/                # latest JSON 文件
  logs/
  tests/
    __init__.py
    conftest.py          # 共享 fixture
    test_auth.py
    test_ingest.py
    test_schema_inspector.py
    test_device_resolver.py
    test_heart_rate_adapter.py
    test_steps_adapter.py
    test_sleep_adapter.py
    test_normalizer.py
    test_archive.py
    test_latest.py
    test_api_endpoints.py
```

---

## Task 1: 项目脚手架和配置模块

**Files:**
- Create: `/srv/health-bridge/app/__init__.py`
- Create: `/srv/health-bridge/app/config.py`
- Create: `/srv/health-bridge/app/tests/__init__.py`
- Create: `/srv/health-bridge/app/tests/conftest.py`
- Create: `/srv/health-bridge/app/tests/test_config.py`

**Interfaces:**
- Produces: `ServerConfig` dataclass with fields: `upload_token`, `read_token`, `data_dir`, `db_path`, `raw_dir`, `incoming_dir`, `archives_dir`, `latest_dir`, `max_uncompressed_bytes`, `max_upload_bytes`

- [ ] **Step 1: 编写配置模块的失败测试**

```python
# /srv/health-bridge/app/tests/test_config.py
"""Tests for server configuration loading."""
import os
import tempfile
import unittest
from pathlib import Path

from app.config import ServerConfig, load_server_config


class TestServerConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_config_from_env_vars(self):
        cfg = load_server_config(environ={
            "HEALTH_UPLOAD_TOKEN": "upload-secret",
            "HEALTH_READ_TOKEN": "read-secret",
            "HEALTH_DATA_DIR": self._tmp,
        })
        self.assertEqual(cfg.upload_token, "upload-secret")
        self.assertEqual(cfg.read_token, "read-secret")
        self.assertEqual(cfg.data_dir, Path(self._tmp))
        self.assertEqual(cfg.db_path, Path(self._tmp) / "health.sqlite3")
        self.assertEqual(cfg.raw_dir, Path(self._tmp) / "raw")
        self.assertEqual(cfg.incoming_dir, Path(self._tmp) / "incoming")
        self.assertEqual(cfg.archives_dir, Path(self._tmp) / "archives")
        self.assertEqual(cfg.latest_dir, Path(self._tmp) / "latest")
        self.assertEqual(cfg.max_uncompressed_bytes, 104_857_600)
        self.assertEqual(cfg.max_upload_bytes, 104_857_600)

    def test_missing_upload_token_raises(self):
        with self.assertRaises(ValueError) as ctx:
            load_server_config(environ={
                "HEALTH_READ_TOKEN": "read-secret",
                "HEALTH_DATA_DIR": self._tmp,
            })
        self.assertIn("upload_token", str(ctx.exception).lower())

    def test_missing_read_token_raises(self):
        with self.assertRaises(ValueError) as ctx:
            load_server_config(environ={
                "HEALTH_UPLOAD_TOKEN": "upload-secret",
                "HEALTH_DATA_DIR": self._tmp,
            })
        self.assertIn("read_token", str(ctx.exception).lower())

    def test_default_data_dir(self):
        cfg = load_server_config(environ={
            "HEALTH_UPLOAD_TOKEN": "u",
            "HEALTH_READ_TOKEN": "r",
        })
        self.assertEqual(cfg.data_dir, Path("/srv/health-bridge/data"))

    def test_token_not_in_repr(self):
        cfg = load_server_config(environ={
            "HEALTH_UPLOAD_TOKEN": "secret-upload",
            "HEALTH_READ_TOKEN": "secret-read",
            "HEALTH_DATA_DIR": self._tmp,
        })
        self.assertNotIn("secret-upload", repr(cfg))
        self.assertNotIn("secret-read", repr(cfg))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 3: 实现配置模块**

```python
# /srv/health-bridge/app/config.py
"""Server configuration for the health-bridge VPS service.

Tokens are read exclusively from environment variables and never from
files or source code.  Token values use repr=False so they never
surface in debug dumps.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_DATA_DIR = "/srv/health-bridge/data"


@dataclass(frozen=True)
class ServerConfig:
    data_dir: Path
    db_path: Path
    raw_dir: Path
    incoming_dir: Path
    archives_dir: Path
    latest_dir: Path
    max_uncompressed_bytes: int
    max_upload_bytes: int
    # repr=False so tokens never surface in logs or debug dumps.
    upload_token: str = field(default="", repr=False)
    read_token: str = field(default="", repr=False)


def load_server_config(environ: dict[str, str]) -> ServerConfig:
    upload_token = environ.get("HEALTH_UPLOAD_TOKEN", "")
    read_token = environ.get("HEALTH_READ_TOKEN", "")
    data_dir = Path(environ.get("HEALTH_DATA_DIR", _DEFAULT_DATA_DIR))

    if not upload_token:
        raise ValueError(
            "HEALTH_UPLOAD_TOKEN environment variable is required"
        )
    if not read_token:
        raise ValueError(
            "HEALTH_READ_TOKEN environment variable is required"
        )

    return ServerConfig(
        data_dir=data_dir,
        db_path=data_dir / "health.sqlite3",
        raw_dir=data_dir / "raw",
        incoming_dir=data_dir / "incoming",
        archives_dir=data_dir.parent / "archives",
        latest_dir=data_dir.parent / "latest",
        max_uncompressed_bytes=104_857_600,
        max_upload_bytes=104_857_600,
        upload_token=upload_token,
        read_token=read_token,
    )
```

```python
# /srv/health-bridge/app/__init__.py
```

```python
# /srv/health-bridge/app/tests/__init__.py
```

```python
# /srv/health-bridge/app/tests/conftest.py
import sys
from pathlib import Path

# Ensure /srv/health-bridge is on sys.path for `from app...` imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_config.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
git add app/__init__.py app/config.py app/tests/
git commit -m "feat: add server config module with env-based token loading"
```

---

## Task 2: 令牌认证模块

**Files:**
- Create: `/srv/health-bridge/app/auth.py`
- Create: `/srv/health-bridge/app/tests/test_auth.py`

**Interfaces:**
- Consumes: `ServerConfig.upload_token`, `ServerConfig.read_token` from Task 1
- Produces: `verify_upload_token(token: str, cfg: ServerConfig) -> bool`, `verify_read_token(token: str, cfg: ServerConfig) -> bool`, FastAPI dependencies `require_upload_auth`, `require_read_auth`

- [ ] **Step 1: 编写认证模块的失败测试**

```python
# /srv/health-bridge/app/tests/test_auth.py
"""Tests for token authentication — constant-time comparison."""
import unittest

from app.config import ServerConfig
from app.auth import verify_upload_token, verify_read_token
from pathlib import Path


def _make_cfg(**kw) -> ServerConfig:
    defaults = dict(
        data_dir=Path("/tmp/hb-test"),
        db_path=Path("/tmp/hb-test/health.sqlite3"),
        raw_dir=Path("/tmp/hb-test/raw"),
        incoming_dir=Path("/tmp/hb-test/incoming"),
        archives_dir=Path("/tmp/hb-test/archives"),
        latest_dir=Path("/tmp/hb-test/latest"),
        max_uncompressed_bytes=104_857_600,
        max_upload_bytes=104_857_600,
        upload_token="upload-secret",
        read_token="read-secret",
    )
    defaults.update(kw)
    return ServerConfig(**defaults)


class TestVerifyUploadToken(unittest.TestCase):
    def test_correct_token_returns_true(self):
        cfg = _make_cfg()
        self.assertTrue(verify_upload_token("upload-secret", cfg))

    def test_wrong_token_returns_false(self):
        cfg = _make_cfg()
        self.assertFalse(verify_upload_token("wrong", cfg))

    def test_empty_token_returns_false(self):
        cfg = _make_cfg()
        self.assertFalse(verify_upload_token("", cfg))


class TestVerifyReadToken(unittest.TestCase):
    def test_correct_token_returns_true(self):
        cfg = _make_cfg()
        self.assertTrue(verify_read_token("read-secret", cfg))

    def test_wrong_token_returns_false(self):
        cfg = _make_cfg()
        self.assertFalse(verify_read_token("wrong", cfg))


class TestTokenSeparation(unittest.TestCase):
    def test_upload_token_cannot_be_used_for_read(self):
        cfg = _make_cfg()
        self.assertFalse(verify_read_token("upload-secret", cfg))

    def test_read_token_cannot_be_used_for_upload(self):
        cfg = _make_cfg()
        self.assertFalse(verify_upload_token("read-secret", cfg))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auth'`

- [ ] **Step 3: 实现认证模块**

```python
# /srv/health-bridge/app/auth.py
"""Constant-time token verification for upload and read endpoints.

Upload and read tokens are separate to allow independent revocation.
Comparisons use hmac.compare_digest to prevent timing attacks.
"""
from __future__ import annotations

import hmac

from app.config import ServerConfig


def verify_upload_token(token: str, cfg: ServerConfig) -> bool:
    if not token or not cfg.upload_token:
        return False
    return hmac.compare_digest(token, cfg.upload_token)


def verify_read_token(token: str, cfg: ServerConfig) -> bool:
    if not token or not cfg.read_token:
        return False
    return hmac.compare_digest(token, cfg.read_token)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_auth.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
git add app/auth.py app/tests/test_auth.py
git commit -m "feat: add constant-time token auth with upload/read separation"
```

---

## Task 3: SQLite 数据库初始化和归一化存储

**Files:**
- Create: `/srv/health-bridge/app/database.py`
- Create: `/srv/health-bridge/app/tests/test_database.py`

**Interfaces:**
- Consumes: `ServerConfig.db_path` from Task 1
- Produces: `init_database(db_path: Path) -> None`, `get_connection(db_path: Path) -> sqlite3.Connection`, database schema constants

- [ ] **Step 1: 编写数据库初始化的失败测试**

```python
# /srv/health-bridge/app/tests/test_database.py
"""Tests for database initialization and schema."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.database import init_database, get_connection, SCHEMA_VERSION


class TestDatabaseInit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._db = Path(self._tmp) / "test.sqlite3"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_init_creates_all_tables(self):
        init_database(self._db)
        conn = get_connection(self._db)
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("snapshots", tables)
            self.assertIn("devices", tables)
            self.assertIn("users", tables)
            self.assertIn("observations", tables)
            self.assertIn("archive_state", tables)
        finally:
            conn.close()

    def test_init_is_idempotent(self):
        init_database(self._db)
        init_database(self._db)  # should not raise
        conn = get_connection(self._db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM snapshots"
            ).fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            conn.close()

    def test_schema_version_recorded(self):
        init_database(self._db)
        conn = get_connection(self._db)
        try:
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()[0]
            self.assertEqual(int(version), SCHEMA_VERSION)
        finally:
            conn.close()

    def test_observations_unique_key(self):
        """The deterministic uniqueness key must be enforced."""
        init_database(self._db)
        conn = get_connection(self._db)
        try:
            # Insert a device and user first.
            conn.execute(
                "INSERT INTO devices (id, gb_device_id, name, identifier, "
                "model, type_name) VALUES (1, 1, 'Test', 'AA:BB', 'M1', 'T1')"
            )
            conn.execute(
                "INSERT INTO users (id, gb_user_id, name) VALUES (1, 1, 'U1')"
            )
            # Insert an observation.
            conn.execute(
                "INSERT INTO observations (dedup_key, device_id, user_id, "
                "data_type, timestamp_utc, timestamp_local, normalized_value, "
                "source_table, source_rowid) "
                "VALUES ('key1', 1, 1, 'heart_rate', 1000, '2026-01-01T00:00:00+08:00', "
                "'{\"bpm\": 80}', 'TEST_TABLE', 1)"
            )
            # Duplicate insert should fail.
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO observations (dedup_key, device_id, user_id, "
                    "data_type, timestamp_utc, timestamp_local, normalized_value, "
                    "source_table, source_rowid) "
                    "VALUES ('key1', 1, 1, 'heart_rate', 1000, "
                    "'2026-01-01T00:00:00+08:00', '{\"bpm\": 80}', 'TEST_TABLE', 1)"
                )
            conn.commit()
        finally:
            conn.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_database.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.database'`

- [ ] **Step 3: 实现数据库模块**

```python
# /srv/health-bridge/app/database.py
"""SQLite database initialization and connection management.

The schema stores:
- snapshots: ingestion metadata (hash, timestamp, status, counts)
- devices: stable internal device records mapped from Gadgetbridge
- users: stable internal user records
- observations: normalized health observations with dedup keys
- archive_state: tracking for which weeks have been generated
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256          TEXT NOT NULL UNIQUE,
    received_at     TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    db_version      TEXT,
    schema_fingerprint TEXT,
    validation_status TEXT NOT NULL,
    import_status   TEXT NOT NULL,
    imported_counts TEXT,
    error_summary   TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    gb_device_id INTEGER NOT NULL,
    name         TEXT NOT NULL,
    identifier   TEXT,
    model        TEXT,
    type_name    TEXT,
    manufacturer TEXT,
    UNIQUE(gb_device_id, identifier)
);

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    gb_user_id  INTEGER NOT NULL,
    name        TEXT NOT NULL,
    UNIQUE(gb_user_id)
);

CREATE TABLE IF NOT EXISTS observations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key        TEXT NOT NULL UNIQUE,
    device_id        INTEGER NOT NULL REFERENCES devices(id),
    user_id          INTEGER NOT NULL REFERENCES users(id),
    data_type        TEXT NOT NULL,
    timestamp_utc    INTEGER NOT NULL,
    timestamp_local  TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    raw_source       TEXT,
    source_table     TEXT NOT NULL,
    source_rowid     INTEGER,
    snapshot_id      INTEGER REFERENCES snapshots(id),
    imported_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_obs_type_ts
    ON observations(data_type, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_obs_dedup
    ON observations(dedup_key);

CREATE TABLE IF NOT EXISTS archive_state (
    week_id    TEXT NOT NULL,
    data_type  TEXT NOT NULL,
    generated_at TEXT,
    record_count INTEGER DEFAULT 0,
    PRIMARY KEY (week_id, data_type)
);
"""


def init_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) "
            "VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    finally:
        conn.close()


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_database.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
git add app/database.py app/tests/test_database.py
git commit -m "feat: add SQLite database schema with dedup keys and indices"
```

---

## Task 4: Gadgetbridge schema 探测器

**Files:**
- Create: `/srv/health-bridge/app/schema_inspector.py`
- Create: `/srv/health-bridge/app/tests/test_schema_inspector.py`

**Interfaces:**
- Consumes: a path to a Gadgetbridge SQLite database (read-only)
- Produces: `SchemaInfo` dataclass with fields: `tables: list[str]`, `fingerprint: str`, `db_version: str | None`, `is_supported: bool`; function `inspect_schema(db_path: Path) -> SchemaInfo`

- [ ] **Step 1: 编写 schema 探测器的失败测试**

```python
# /srv/health-bridge/app/tests/test_schema_inspector.py
"""Tests for Gadgetbridge schema inspection."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.schema_inspector import inspect_schema, SchemaInfo


def _create_test_db(path: Path, tables: list[str]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        for table in tables:
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.execute("PRAGMA user_version = 202")
        conn.commit()
    finally:
        conn.close()


class TestInspectSchema(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._db = Path(self._tmp) / "test.db"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_detects_xiaomi_tables(self):
        _create_test_db(self._db, [
            "DEVICE", "USER",
            "XIAOMI_ACTIVITY_SAMPLE",
            "XIAOMI_SLEEP_TIME_SAMPLE",
            "XIAOMI_SLEEP_STAGE_SAMPLE",
            "XIAOMI_DAILY_SUMMARY_SAMPLE",
        ])
        info = inspect_schema(self._db)
        self.assertIn("XIAOMI_ACTIVITY_SAMPLE", info.tables)
        self.assertTrue(info.is_supported)
        self.assertIsNotNone(info.fingerprint)
        self.assertEqual(len(info.fingerprint), 64)  # SHA-256 hex

    def test_unsupported_schema(self):
        _create_test_db(self._db, ["UNKNOWN_TABLE"])
        info = inspect_schema(self._db)
        self.assertFalse(info.is_supported)

    def test_fingerprint_is_deterministic(self):
        _create_test_db(self._db, ["XIAOMI_ACTIVITY_SAMPLE", "DEVICE"])
        info1 = inspect_schema(self._db)
        info2 = inspect_schema(self._db)
        self.assertEqual(info1.fingerprint, info2.fingerprint)

    def test_db_version_extracted(self):
        _create_test_db(self._db, ["XIAOMI_ACTIVITY_SAMPLE"])
        info = inspect_schema(self._db)
        self.assertEqual(info.db_version, "202")

    def test_fingerprint_differs_for_different_tables(self):
        _create_test_db(self._db, ["XIAOMI_ACTIVITY_SAMPLE"])
        info1 = inspect_schema(self._db)

        db2 = Path(self._tmp) / "test2.db"
        _create_test_db(db2, ["MI_BAND_ACTIVITY_SAMPLE"])
        info2 = inspect_schema(db2)

        self.assertNotEqual(info1.fingerprint, info2.fingerprint)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_schema_inspector.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 schema 探测器**

```python
# /srv/health-bridge/app/schema_inspector.py
"""Gadgetbridge schema inspection and fingerprinting.

Inspects an uploaded SQLite database to determine which tables exist,
compute a deterministic schema fingerprint, and check whether the
schema is supported by the current adapters.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# Tables that indicate a supported Xiaomi protobuf device schema.
_SUPPORTED_TABLES = frozenset({
    "XIAOMI_ACTIVITY_SAMPLE",
    "XIAOMI_SLEEP_TIME_SAMPLE",
    "XIAOMI_SLEEP_STAGE_SAMPLE",
    "XIAOMI_DAILY_SUMMARY_SAMPLE",
})


@dataclass(frozen=True)
class SchemaInfo:
    tables: list[str]
    fingerprint: str
    db_version: str | None
    is_supported: bool


def inspect_schema(db_path: Path) -> SchemaInfo:
    uri = str(db_path.resolve()).replace("file:///", "file:///")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "ORDER BY name"
        ).fetchall()
        tables = [r[0] for r in table_rows]

        # Fingerprint: SHA-256 of sorted table names + their column signatures.
        sig_parts = []
        for table in tables:
            cols = conn.execute(
                f'PRAGMA table_info("{table}")'
            ).fetchall()
            col_names = [c[1] for c in cols]
            sig_parts.append(f"{table}({','.join(col_names)})")
        fingerprint = hashlib.sha256(
            "|".join(sig_parts).encode("utf-8")
        ).hexdigest()

        # DB version from PRAGMA user_version.
        version_row = conn.execute("PRAGMA user_version").fetchone()
        db_version = str(version_row[0]) if version_row and version_row[0] > 0 else None

        is_supported = bool(_SUPPORTED_TABLES & set(tables))

        return SchemaInfo(
            tables=tables,
            fingerprint=fingerprint,
            db_version=db_version,
            is_supported=is_supported,
        )
    finally:
        conn.close()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_schema_inspector.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
git add app/schema_inspector.py app/tests/test_schema_inspector.py
git commit -m "feat: add Gadgetbridge schema inspector with fingerprinting"
```

---

## Task 5: 设备/用户解析器

**Files:**
- Create: `/srv/health-bridge/app/device_resolver.py`
- Create: `/srv/health-bridge/app/tests/test_device_resolver.py`

**Interfaces:**
- Consumes: `sqlite3.Connection` to the Gadgetbridge DB, `sqlite3.Connection` to the server DB
- Produces: `ResolvedDevice` dataclass with `internal_id: int`, `gb_device_id: int`, `name: str`, `identifier: str`, `model: str`, `type_name: str`; `ResolvedUser` with `internal_id: int`, `gb_user_id: int`, `name: str`; function `resolve_devices(gb_conn, server_conn) -> tuple[list[ResolvedDevice], list[ResolvedUser]]`

- [ ] **Step 1: 编写设备解析器的失败测试**

```python
# /srv/health-bridge/app/tests/test_device_resolver.py
"""Tests for device/user resolution from Gadgetbridge to internal IDs."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.database import init_database, get_connection
from app.device_resolver import resolve_devices, ResolvedDevice, ResolvedUser


def _create_gb_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE DEVICE (_id INTEGER PRIMARY KEY, NAME TEXT, "
            "MANUFACTURER TEXT, IDENTIFIER TEXT, TYPE INTEGER, "
            "TYPE_NAME TEXT, MODEL TEXT, ALIAS TEXT, PARENT_FOLDER INTEGER)"
        )
        conn.execute(
            "CREATE TABLE USER (_id INTEGER PRIMARY KEY, NAME TEXT, "
            "BIRTHDAY INTEGER, GENDER INTEGER)"
        )
        conn.execute(
            "INSERT INTO DEVICE VALUES (1, 'Xiaomi Smart Band 8', 'Xiaomi', "
            "'D0:62:2C:4E:21:94', 0, 'MIBAND8', 'M2239B1', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO USER VALUES (1, 'gadgetbridge-user', 946708853174, 2)"
        )
        conn.commit()
    finally:
        conn.close()


class TestResolveDevices(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._gb_db = Path(self._tmp) / "gb.db"
        self._server_db = Path(self._tmp) / "server.db"
        init_database(self._server_db)
        _create_gb_db(self._gb_db)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_resolves_device_and_user(self):
        gb_conn = sqlite3.connect(str(self._gb_db))
        server_conn = get_connection(self._server_db)
        try:
            devices, users = resolve_devices(gb_conn, server_conn)
            self.assertEqual(len(devices), 1)
            self.assertEqual(devices[0].gb_device_id, 1)
            self.assertEqual(devices[0].name, "Xiaomi Smart Band 8")
            self.assertEqual(devices[0].identifier, "D0:62:2C:4E:21:94")
            self.assertEqual(devices[0].model, "M2239B1")
            self.assertEqual(devices[0].type_name, "MIBAND8")
            self.assertEqual(devices[0].internal_id, 1)

            self.assertEqual(len(users), 1)
            self.assertEqual(users[0].gb_user_id, 1)
            self.assertEqual(users[0].name, "gadgetbridge-user")
            self.assertEqual(users[0].internal_id, 1)
            server_conn.commit()
        finally:
            gb_conn.close()
            server_conn.close()

    def test_idempotent_resolve(self):
        """Resolving the same device twice returns the same internal ID."""
        gb_conn = sqlite3.connect(str(self._gb_db))
        server_conn = get_connection(self._server_db)
        try:
            devices1, users1 = resolve_devices(gb_conn, server_conn)
            server_conn.commit()
            devices2, users2 = resolve_devices(gb_conn, server_conn)
            server_conn.commit()
            self.assertEqual(devices1[0].internal_id, devices2[0].internal_id)
            self.assertEqual(users1[0].internal_id, users2[0].internal_id)
        finally:
            gb_conn.close()
            server_conn.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_device_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现设备解析器**

```python
# /srv/health-bridge/app/device_resolver.py
"""Device and user resolution from Gadgetbridge to stable internal IDs.

Maps Gadgetbridge device_id and user_id to stable internal IDs in the
server database.  Re-importing the same Gadgetbridge database returns
the same internal IDs.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedDevice:
    internal_id: int
    gb_device_id: int
    name: str
    identifier: str
    model: str
    type_name: str


@dataclass(frozen=True)
class ResolvedUser:
    internal_id: int
    gb_user_id: int
    name: str


def resolve_devices(
    gb_conn: sqlite3.Connection,
    server_conn: sqlite3.Connection,
) -> tuple[list[ResolvedDevice], list[ResolvedUser]]:
    devices = []
    for row in gb_conn.execute(
        "SELECT _id, NAME, IDENTIFIER, MODEL, TYPE_NAME, MANUFACTURER "
        "FROM DEVICE ORDER BY _id"
    ).fetchall():
        gb_id, name, identifier, model, type_name, manufacturer = row
        server_conn.execute(
            "INSERT OR IGNORE INTO devices "
            "(gb_device_id, name, identifier, model, type_name, manufacturer) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (gb_id, name, identifier, model, type_name, manufacturer),
        )
        internal = server_conn.execute(
            "SELECT id FROM devices WHERE gb_device_id=? AND identifier=?",
            (gb_id, identifier),
        ).fetchone()[0]
        devices.append(ResolvedDevice(
            internal_id=internal,
            gb_device_id=gb_id,
            name=name,
            identifier=identifier or "",
            model=model or "",
            type_name=type_name or "",
        ))

    users = []
    for row in gb_conn.execute(
        "SELECT _id, NAME FROM USER ORDER BY _id"
    ).fetchall():
        gb_id, name = row
        server_conn.execute(
            "INSERT OR IGNORE INTO users (gb_user_id, name) VALUES (?, ?)",
            (gb_id, name),
        )
        internal = server_conn.execute(
            "SELECT id FROM users WHERE gb_user_id=?", (gb_id,)
        ).fetchone()[0]
        users.append(ResolvedUser(
            internal_id=internal,
            gb_user_id=gb_id,
            name=name or "",
        ))

    return devices, users
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_device_resolver.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
git add app/device_resolver.py app/tests/test_device_resolver.py
git commit -m "feat: add device/user resolver with idempotent mapping"
```

---

## Task 6: 心率适配器

**Files:**
- Create: `/srv/health-bridge/app/adapters/__init__.py`
- Create: `/srv/health-bridge/app/adapters/base.py`
- Create: `/srv/health-bridge/app/adapters/heart_rate.py`
- Create: `/srv/health-bridge/app/tests/test_heart_rate_adapter.py`

**Interfaces:**
- Consumes: `sqlite3.Connection` to Gadgetbridge DB, `ResolvedDevice`, `ResolvedUser`
- Produces: `RawObservation` dataclass from base.py; `HeartRateAdapter` class with `extract(gb_conn, device, user) -> list[RawObservation]`

- [ ] **Step 1: 编写心率适配器的失败测试**

```python
# /srv/health-bridge/app/tests/test_heart_rate_adapter.py
"""Tests for the heart rate adapter — extracts HR from XIAOMI_ACTIVITY_SAMPLE."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.adapters.heart_rate import HeartRateAdapter
from app.adapters.base import RawObservation
from app.device_resolver import ResolvedDevice, ResolvedUser


def _create_gb_db_with_hr(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE XIAOMI_ACTIVITY_SAMPLE ("
            "TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, "
            "RAW_INTENSITY INTEGER, STEPS INTEGER, RAW_KIND INTEGER, "
            "HEART_RATE INTEGER, STRESS INTEGER, SPO2 INTEGER, "
            "DISTANCE_CM INTEGER, ACTIVE_CALORIES INTEGER, ENERGY INTEGER)"
        )
        # Rows: some with HR, some without (HR=0 should be skipped).
        conn.execute(
            "INSERT INTO XIAOMI_ACTIVITY_SAMPLE VALUES "
            "(1000, 1, 1, 0, 0, 0, 80, 0, 0, 0, 0, -1),"
            "(1060, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, -1),"
            "(1120, 1, 1, 0, 0, 0, 85, 0, 0, 0, 0, -1),"
            "(1180, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, -1)"
        )
        conn.commit()
    finally:
        conn.close()


_DEVICE = ResolvedDevice(
    internal_id=1, gb_device_id=1, name="Test",
    identifier="AA:BB", model="M1", type_name="MIBAND8",
)
_USER = ResolvedUser(internal_id=1, gb_user_id=1, name="U1")


class TestHeartRateAdapter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._db = Path(self._tmp) / "gb.db"
        _create_gb_db_with_hr(self._db)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_extracts_only_rows_with_hr(self):
        conn = sqlite3.connect(str(self._db))
        try:
            adapter = HeartRateAdapter()
            observations = adapter.extract(conn, _DEVICE, _USER)
            self.assertEqual(len(observations), 2)
        finally:
            conn.close()

    def test_observation_fields(self):
        conn = sqlite3.connect(str(self._db))
        try:
            adapter = HeartRateAdapter()
            obs = adapter.extract(conn, _DEVICE, _USER)
            self.assertEqual(obs[0].data_type, "heart_rate")
            self.assertEqual(obs[0].timestamp_utc, 1000)
            self.assertEqual(obs[0].source_table, "XIAOMI_ACTIVITY_SAMPLE")
            self.assertIn("bpm", obs[0].normalized_value)
        finally:
            conn.close()

    def test_dedup_key_is_deterministic(self):
        conn = sqlite3.connect(str(self._db))
        try:
            adapter = HeartRateAdapter()
            obs = adapter.extract(conn, _DEVICE, _USER)
            self.assertEqual(
                obs[0].dedup_key,
                "heart_rate|1|1000|XIAOMI_ACTIVITY_SAMPLE|80",
            )
        finally:
            conn.close()

    def test_handles_empty_table(self):
        db2 = Path(self._tmp) / "empty.db"
        conn2 = sqlite3.connect(str(db2))
        try:
            conn2.execute(
                "CREATE TABLE XIAOMI_ACTIVITY_SAMPLE ("
                "TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, "
                "RAW_INTENSITY INTEGER, STEPS INTEGER, RAW_KIND INTEGER, "
                "HEART_RATE INTEGER, STRESS INTEGER, SPO2 INTEGER, "
                "DISTANCE_CM INTEGER, ACTIVE_CALORIES INTEGER, ENERGY INTEGER)"
            )
            conn2.commit()
            adapter = HeartRateAdapter()
            obs = adapter.extract(conn2, _DEVICE, _USER)
            self.assertEqual(len(obs), 0)
        finally:
            conn2.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_heart_rate_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现适配器基类和心率适配器**

```python
# /srv/health-bridge/app/adapters/__init__.py
```

```python
# /srv/health-bridge/app/adapters/base.py
"""Base classes for Gadgetbridge data type adapters.

Each adapter extracts raw observations from a Gadgetbridge SQLite
database for one data type. The normalizer converts RawObservation
into the final stored format.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Protocol

from app.device_resolver import ResolvedDevice, ResolvedUser


@dataclass(frozen=True)
class RawObservation:
    data_type: str
    timestamp_utc: int
    normalized_value: str  # JSON string
    raw_source: str  # JSON string of original row fields
    source_table: str
    source_rowid: int
    dedup_key: str


class TypeAdapter(Protocol):
    """Protocol for all type adapters."""

    def extract(
        self,
        gb_conn: sqlite3.Connection,
        device: ResolvedDevice,
        user: ResolvedUser,
    ) -> list[RawObservation]:
        ...
```

```python
# /srv/health-bridge/app/adapters/heart_rate.py
"""Heart rate adapter — extracts BPM from XIAOMI_ACTIVITY_SAMPLE.

For Xiaomi Mi Band 8, heart rate is stored as a column in the
combined activity sample table.  Rows where HEART_RATE = 0 indicate
no measurement and are skipped.
"""
from __future__ import annotations

import json
import sqlite3

from app.adapters.base import RawObservation
from app.device_resolver import ResolvedDevice, ResolvedUser

_SOURCE_TABLE = "XIAOMI_ACTIVITY_SAMPLE"


class HeartRateAdapter:
    def extract(
        self,
        gb_conn: sqlite3.Connection,
        device: ResolvedDevice,
        user: ResolvedUser,
    ) -> list[RawObservation]:
        rows = gb_conn.execute(
            f"SELECT TIMESTAMP, HEART_RATE, RAW_KIND, STRESS, SPO2 "
            f"FROM {_SOURCE_TABLE} "
            f"WHERE DEVICE_ID=? AND USER_ID=? AND HEART_RATE > 0 "
            f"ORDER BY TIMESTAMP",
            (device.gb_device_id, user.gb_user_id),
        ).fetchall()

        observations = []
        for row in rows:
            timestamp, hr, raw_kind, stress, spo2 = row
            normalized = json.dumps({"bpm": hr})
            raw_source = json.dumps({
                "timestamp": timestamp,
                "heart_rate": hr,
                "raw_kind": raw_kind,
                "stress": stress,
                "spo2": spo2,
            })
            dedup_key = f"heart_rate|{device.internal_id}|{timestamp}|{_SOURCE_TABLE}|{hr}"
            observations.append(RawObservation(
                data_type="heart_rate",
                timestamp_utc=timestamp,
                normalized_value=normalized,
                raw_source=raw_source,
                source_table=_SOURCE_TABLE,
                source_rowid=timestamp,
                dedup_key=dedup_key,
            ))
        return observations
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_heart_rate_adapter.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
git add app/adapters/ app/tests/test_heart_rate_adapter.py
git commit -m "feat: add heart rate adapter for XIAOMI_ACTIVITY_SAMPLE"
```

---

## Task 7: 步数适配器

**Files:**
- Create: `/srv/health-bridge/app/adapters/steps.py`
- Create: `/srv/health-bridge/app/tests/test_steps_adapter.py`

**Interfaces:**
- Consumes: same as Task 6
- Produces: `StepsAdapter` class with `extract(gb_conn, device, user) -> list[RawObservation]`

- [ ] **Step 1: 编写步数适配器的失败测试**

```python
# /srv/health-bridge/app/tests/test_steps_adapter.py
"""Tests for the steps adapter — extracts steps from XIAOMI_ACTIVITY_SAMPLE
and daily totals from XIAOMI_DAILY_SUMMARY_SAMPLE."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.adapters.steps import StepsAdapter
from app.adapters.base import RawObservation
from app.device_resolver import ResolvedDevice, ResolvedUser


def _create_gb_db_with_steps(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE XIAOMI_ACTIVITY_SAMPLE ("
            "TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, "
            "RAW_INTENSITY INTEGER, STEPS INTEGER, RAW_KIND INTEGER, "
            "HEART_RATE INTEGER, STRESS INTEGER, SPO2 INTEGER, "
            "DISTANCE_CM INTEGER, ACTIVE_CALORIES INTEGER, ENERGY INTEGER)"
        )
        conn.execute(
            "CREATE TABLE XIAOMI_DAILY_SUMMARY_SAMPLE ("
            "TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, "
            "TIMEZONE INTEGER, STEPS INTEGER, HR_RESTING INTEGER, "
            "HR_MAX INTEGER, HR_MAX_TS INTEGER, HR_MIN INTEGER, "
            "HR_MIN_TS INTEGER, HR_AVG INTEGER, STRESS_AVG INTEGER, "
            "STRESS_MAX INTEGER, STRESS_MIN INTEGER, STANDING INTEGER, "
            "CALORIES INTEGER, SPO2_MAX INTEGER, SPO2_MAX_TS INTEGER, "
            "SPO2_MIN INTEGER, SPO2_MIN_TS INTEGER, SPO2_AVG INTEGER, "
            "TRAINING_LOAD_DAY INTEGER, TRAINING_LOAD_WEEK INTEGER, "
            "TRAINING_LOAD_LEVEL INTEGER, VITALITY_INCREASE_LIGHT INTEGER, "
            "VITALITY_INCREASE_MODERATE INTEGER, VITALITY_INCREASE_HIGH INTEGER, "
            "VITALITY_CURRENT INTEGER)"
        )
        # Per-minute step samples (only rows with STEPS > 0).
        conn.execute(
            "INSERT INTO XIAOMI_ACTIVITY_SAMPLE VALUES "
            "(1000, 1, 1, 10, 5, 0, 0, 0, 0, 500, 1, -1),"
            "(1060, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, -1),"
            "(1120, 1, 1, 20, 12, 0, 0, 0, 0, 1200, 3, -1)"
        )
        # Daily summary (timestamp in milliseconds).
        conn.execute(
            "INSERT INTO XIAOMI_DAILY_SUMMARY_SAMPLE VALUES "
            "(86400000, 1, 1, 32, 500, 0, 100, 50000, 60, 40000, 75, "
            "0, 0, 0, 0, 29, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0)"
        )
        conn.commit()
    finally:
        conn.close()


_DEVICE = ResolvedDevice(
    internal_id=1, gb_device_id=1, name="Test",
    identifier="AA:BB", model="M1", type_name="MIBAND8",
)
_USER = ResolvedUser(internal_id=1, gb_user_id=1, name="U1")


class TestStepsAdapter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._db = Path(self._tmp) / "gb.db"
        _create_gb_db_with_steps(self._db)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_extracts_per_minute_samples(self):
        conn = sqlite3.connect(str(self._db))
        try:
            adapter = StepsAdapter()
            obs = adapter.extract(conn, _DEVICE, _USER)
            # 2 per-minute samples + 1 daily summary.
            sample_obs = [o for o in obs if o.source_table == "XIAOMI_ACTIVITY_SAMPLE"]
            daily_obs = [o for o in obs if o.source_table == "XIAOMI_DAILY_SUMMARY_SAMPLE"]
            self.assertEqual(len(sample_obs), 2)
            self.assertEqual(len(daily_obs), 1)
        finally:
            conn.close()

    def test_daily_summary_timestamp_converted_from_ms(self):
        conn = sqlite3.connect(str(self._db))
        try:
            adapter = StepsAdapter()
            obs = adapter.extract(conn, _DEVICE, _USER)
            daily = [o for o in obs if o.source_table == "XIAOMI_DAILY_SUMMARY_SAMPLE"][0]
            # 86400000 ms = 86400 seconds.
            self.assertEqual(daily.timestamp_utc, 86400)
        finally:
            conn.close()

    def test_dedup_key_includes_value(self):
        conn = sqlite3.connect(str(self._db))
        try:
            adapter = StepsAdapter()
            obs = adapter.extract(conn, _DEVICE, _USER)
            sample = [o for o in obs if o.source_table == "XIAOMI_ACTIVITY_SAMPLE"][0]
            self.assertTrue(sample.dedup_key.startswith("steps|1|"))
        finally:
            conn.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_steps_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现步数适配器**

```python
# /srv/health-bridge/app/adapters/steps.py
"""Steps adapter — extracts per-minute step counts from
XIAOMI_ACTIVITY_SAMPLE and daily totals from
XIAOMI_DAILY_SUMMARY_SAMPLE.

Note: XIAOMI_DAILY_SUMMARY_SAMPLE.TIMESTAMP is in milliseconds,
while XIAOMI_ACTIVITY_SAMPLE.TIMESTAMP is in seconds.
"""
from __future__ import annotations

import json
import sqlite3

from app.adapters.base import RawObservation
from app.device_resolver import ResolvedDevice, ResolvedUser

_ACTIVITY_TABLE = "XIAOMI_ACTIVITY_SAMPLE"
_DAILY_TABLE = "XIAOMI_DAILY_SUMMARY_SAMPLE"


class StepsAdapter:
    def extract(
        self,
        gb_conn: sqlite3.Connection,
        device: ResolvedDevice,
        user: ResolvedUser,
    ) -> list[RawObservation]:
        observations: list[RawObservation] = []

        # Per-minute samples: only rows with STEPS > 0.
        rows = gb_conn.execute(
            f"SELECT TIMESTAMP, STEPS, DISTANCE_CM, ACTIVE_CALORIES, "
            f"RAW_INTENSITY FROM {_ACTIVITY_TABLE} "
            f"WHERE DEVICE_ID=? AND USER_ID=? AND STEPS > 0 "
            f"ORDER BY TIMESTAMP",
            (device.gb_device_id, user.gb_user_id),
        ).fetchall()

        for row in rows:
            ts, steps, distance_cm, calories, intensity = row
            normalized = json.dumps({
                "steps": steps,
                "distance_cm": distance_cm,
                "calories": calories,
            })
            raw_source = json.dumps({
                "timestamp": ts,
                "steps": steps,
                "distance_cm": distance_cm,
                "active_calories": calories,
                "raw_intensity": intensity,
            })
            dedup_key = f"steps|{device.internal_id}|{ts}|{_ACTIVITY_TABLE}|{steps}"
            observations.append(RawObservation(
                data_type="steps",
                timestamp_utc=ts,
                normalized_value=normalized,
                raw_source=raw_source,
                source_table=_ACTIVITY_TABLE,
                source_rowid=ts,
                dedup_key=dedup_key,
            ))

        # Daily summary: timestamp is in milliseconds.
        daily_rows = gb_conn.execute(
            f"SELECT TIMESTAMP, STEPS, CALORIES, STANDING FROM {_DAILY_TABLE} "
            f"WHERE DEVICE_ID=? AND USER_ID=? AND STEPS > 0 "
            f"ORDER BY TIMESTAMP",
            (device.gb_device_id, user.gb_user_id),
        ).fetchall()

        for row in daily_rows:
            ts_ms, steps, calories, standing = row
            ts_sec = ts_ms // 1000
            normalized = json.dumps({
                "steps_daily": steps,
                "calories": calories,
                "standing": standing,
            })
            raw_source = json.dumps({
                "timestamp_ms": ts_ms,
                "steps": steps,
                "calories": calories,
                "standing": standing,
            })
            dedup_key = f"steps_daily|{device.internal_id}|{ts_sec}|{_DAILY_TABLE}|{steps}"
            observations.append(RawObservation(
                data_type="steps",
                timestamp_utc=ts_sec,
                normalized_value=normalized,
                raw_source=raw_source,
                source_table=_DAILY_TABLE,
                source_rowid=ts_ms,
                dedup_key=dedup_key,
            ))

        return observations
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_steps_adapter.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
git add app/adapters/steps.py app/tests/test_steps_adapter.py
git commit -m "feat: add steps adapter for activity samples and daily summary"
```

---

## Task 8: 睡眠适配器

**Files:**
- Create: `/srv/health-bridge/app/adapters/sleep.py`
- Create: `/srv/health-bridge/app/tests/test_sleep_adapter.py`

**Interfaces:**
- Consumes: same as Task 6
- Produces: `SleepAdapter` class with `extract(gb_conn, device, user) -> list[RawObservation]`; produces two sub-types: `sleep_session` and `sleep_stage`

- [ ] **Step 1: 编写睡眠适配器的失败测试**

```python
# /srv/health-bridge/app/tests/test_sleep_adapter.py
"""Tests for the sleep adapter — extracts sessions and stages."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.adapters.sleep import SleepAdapter
from app.device_resolver import ResolvedDevice, ResolvedUser


def _create_gb_db_with_sleep(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE XIAOMI_SLEEP_TIME_SAMPLE ("
            "TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, "
            "WAKEUP_TIME INTEGER, IS_AWAKE INTEGER, TOTAL_DURATION INTEGER, "
            "DEEP_SLEEP_DURATION INTEGER, LIGHT_SLEEP_DURATION INTEGER, "
            "REM_SLEEP_DURATION INTEGER, AWAKE_DURATION INTEGER)"
        )
        conn.execute(
            "CREATE TABLE XIAOMI_SLEEP_STAGE_SAMPLE ("
            "TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, "
            "STAGE INTEGER)"
        )
        conn.execute(
            "INSERT INTO XIAOMI_SLEEP_TIME_SAMPLE VALUES "
            "(1000, 1, 1, 2800, 0, 1800, 600, 900, 200, 100)"
        )
        conn.execute(
            "INSERT INTO XIAOMI_SLEEP_STAGE_SAMPLE VALUES "
            "(1000, 1, 1, 1),"
            "(1100, 1, 1, 2),"
            "(1200, 1, 1, 1),"
            "(1300, 1, 1, 4)"
        )
        conn.commit()
    finally:
        conn.close()


_DEVICE = ResolvedDevice(
    internal_id=1, gb_device_id=1, name="Test",
    identifier="AA:BB", model="M1", type_name="MIBAND8",
)
_USER = ResolvedUser(internal_id=1, gb_user_id=1, name="U1")


class TestSleepAdapter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._db = Path(self._tmp) / "gb.db"
        _create_gb_db_with_sleep(self._db)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_extracts_sessions_and_stages(self):
        conn = sqlite3.connect(str(self._db))
        try:
            adapter = SleepAdapter()
            obs = adapter.extract(conn, _DEVICE, _USER)
            sessions = [o for o in obs if o.data_type == "sleep_session"]
            stages = [o for o in obs if o.data_type == "sleep_stage"]
            self.assertEqual(len(sessions), 1)
            self.assertEqual(len(stages), 4)
        finally:
            conn.close()

    def test_session_fields(self):
        conn = sqlite3.connect(str(self._db))
        try:
            adapter = SleepAdapter()
            obs = adapter.extract(conn, _DEVICE, _USER)
            session = [o for o in obs if o.data_type == "sleep_session"][0]
            self.assertEqual(session.timestamp_utc, 1000)
            self.assertEqual(session.source_table, "XIAOMI_SLEEP_TIME_SAMPLE")
        finally:
            conn.close()

    def test_stage_dedup_key_includes_stage_code(self):
        conn = sqlite3.connect(str(self._db))
        try:
            adapter = SleepAdapter()
            obs = adapter.extract(conn, _DEVICE, _USER)
            stages = [o for o in obs if o.data_type == "sleep_stage"]
            # Two stages have code=1 at different timestamps — different keys.
            keys = {s.dedup_key for s in stages}
            self.assertEqual(len(keys), 4)
        finally:
            conn.close()

    def test_handles_empty_sleep_tables(self):
        db2 = Path(self._tmp) / "empty.db"
        conn2 = sqlite3.connect(str(db2))
        try:
            conn2.execute(
                "CREATE TABLE XIAOMI_SLEEP_TIME_SAMPLE ("
                "TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, "
                "WAKEUP_TIME INTEGER, IS_AWAKE INTEGER, TOTAL_DURATION INTEGER, "
                "DEEP_SLEEP_DURATION INTEGER, LIGHT_SLEEP_DURATION INTEGER, "
                "REM_SLEEP_DURATION INTEGER, AWAKE_DURATION INTEGER)"
            )
            conn2.execute(
                "CREATE TABLE XIAOMI_SLEEP_STAGE_SAMPLE ("
                "TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, "
                "STAGE INTEGER)"
            )
            conn2.commit()
            adapter = SleepAdapter()
            obs = adapter.extract(conn2, _DEVICE, _USER)
            self.assertEqual(len(obs), 0)
        finally:
            conn2.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_sleep_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现睡眠适配器**

```python
# /srv/health-bridge/app/adapters/sleep.py
"""Sleep adapter — extracts sleep sessions and stages.

XIAOMI_SLEEP_TIME_SAMPLE: one row per sleep session with aggregate
durations (deep, light, REM, awake).

XIAOMI_SLEEP_STAGE_SAMPLE: one row per time interval with a stage
code.  Stage codes are device-specific and must not be presented as
medically authoritative.
"""
from __future__ import annotations

import json
import sqlite3

from app.adapters.base import RawObservation
from app.device_resolver import ResolvedDevice, ResolvedUser

_SESSION_TABLE = "XIAOMI_SLEEP_TIME_SAMPLE"
_STAGE_TABLE = "XIAOMI_SLEEP_STAGE_SAMPLE"


class SleepAdapter:
    def extract(
        self,
        gb_conn: sqlite3.Connection,
        device: ResolvedDevice,
        user: ResolvedUser,
    ) -> list[RawObservation]:
        observations: list[RawObservation] = []

        # Sleep sessions.
        rows = gb_conn.execute(
            f"SELECT TIMESTAMP, WAKEUP_TIME, IS_AWAKE, TOTAL_DURATION, "
            f"DEEP_SLEEP_DURATION, LIGHT_SLEEP_DURATION, "
            f"REM_SLEEP_DURATION, AWAKE_DURATION "
            f"FROM {_SESSION_TABLE} "
            f"WHERE DEVICE_ID=? AND USER_ID=? ORDER BY TIMESTAMP",
            (device.gb_device_id, user.gb_user_id),
        ).fetchall()

        for row in rows:
            (ts, wakeup, is_awake, total, deep, light,
             rem, awake_dur) = row
            normalized = json.dumps({
                "wakeup_time": wakeup,
                "total_duration": total,
                "deep_sleep_duration": deep,
                "light_sleep_duration": light,
                "rem_sleep_duration": rem,
                "awake_duration": awake_dur,
            })
            raw_source = json.dumps({
                "timestamp": ts,
                "wakeup_time": wakeup,
                "is_awake": is_awake,
                "total_duration": total,
                "deep_sleep_duration": deep,
                "light_sleep_duration": light,
                "rem_sleep_duration": rem,
                "awake_duration": awake_dur,
            })
            dedup_key = f"sleep_session|{device.internal_id}|{ts}|{_SESSION_TABLE}"
            observations.append(RawObservation(
                data_type="sleep_session",
                timestamp_utc=ts,
                normalized_value=normalized,
                raw_source=raw_source,
                source_table=_SESSION_TABLE,
                source_rowid=ts,
                dedup_key=dedup_key,
            ))

        # Sleep stages.
        stage_rows = gb_conn.execute(
            f"SELECT TIMESTAMP, STAGE FROM {_STAGE_TABLE} "
            f"WHERE DEVICE_ID=? AND USER_ID=? ORDER BY TIMESTAMP",
            (device.gb_device_id, user.gb_user_id),
        ).fetchall()

        for row in stage_rows:
            ts, stage = row
            normalized = json.dumps({"stage_code": stage})
            raw_source = json.dumps({"timestamp": ts, "stage": stage})
            dedup_key = f"sleep_stage|{device.internal_id}|{ts}|{_STAGE_TABLE}|{stage}"
            observations.append(RawObservation(
                data_type="sleep_stage",
                timestamp_utc=ts,
                normalized_value=normalized,
                raw_source=raw_source,
                source_table=_STAGE_TABLE,
                source_rowid=ts,
                dedup_key=dedup_key,
            ))

        return observations
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_sleep_adapter.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
git add app/adapters/sleep.py app/tests/test_sleep_adapter.py
git commit -m "feat: add sleep adapter for sessions and stages"
```

---

## Task 9: 归一化器和导入管线

**Files:**
- Create: `/srv/health-bridge/app/normalizer.py`
- Create: `/srv/health-bridge/app/tests/test_normalizer.py`

**Interfaces:**
- Consumes: `list[RawObservation]` from adapters, `ResolvedDevice`, `ResolvedUser`, `sqlite3.Connection` to server DB, `snapshot_id: int`
- Produces: `ImportResult` dataclass with `imported: dict[str, int]`, `skipped_duplicates: int`; function `import_observations(server_conn, observations, device, user, snapshot_id) -> ImportResult`

- [ ] **Step 1: 编写归一化器的失败测试**

```python
# /srv/health-bridge/app/tests/test_normalizer.py
"""Tests for observation normalization and import with dedup."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.database import init_database, get_connection
from app.adapters.base import RawObservation
from app.normalizer import import_observations, ImportResult
from app.device_resolver import ResolvedDevice, ResolvedUser


_DEVICE = ResolvedDevice(
    internal_id=1, gb_device_id=1, name="Test",
    identifier="AA:BB", model="M1", type_name="T1",
)
_USER = ResolvedUser(internal_id=1, gb_user_id=1, name="U1")


def _make_obs(ts: int, hr: int) -> RawObservation:
    return RawObservation(
        data_type="heart_rate",
        timestamp_utc=ts,
        normalized_value=json.dumps({"bpm": hr}),
        raw_source=json.dumps({"timestamp": ts, "heart_rate": hr}),
        source_table="XIAOMI_ACTIVITY_SAMPLE",
        source_rowid=ts,
        dedup_key=f"heart_rate|1|{ts}|XIAOMI_ACTIVITY_SAMPLE|{hr}",
    )


class TestImportObservations(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._db = Path(self._tmp) / "server.db"
        init_database(self._db)
        conn = get_connection(self._db)
        try:
            conn.execute(
                "INSERT INTO devices (id, gb_device_id, name, identifier, "
                "model, type_name) VALUES (1, 1, 'Test', 'AA:BB', 'M1', 'T1')"
            )
            conn.execute(
                "INSERT INTO users (id, gb_user_id, name) VALUES (1, 1, 'U1')"
            )
            conn.execute(
                "INSERT INTO snapshots (id, sha256, received_at, "
                "source_filename, validation_status, import_status) "
                "VALUES (1, 'abc123', '2026-07-11T00:00:00Z', 'test.db', "
                "'valid', 'imported')"
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_imports_new_observations(self):
        obs = [_make_obs(1000, 80), _make_obs(1060, 85)]
        conn = get_connection(self._db)
        try:
            result = import_observations(conn, obs, _DEVICE, _USER, snapshot_id=1)
            conn.commit()
            self.assertEqual(result.imported["heart_rate"], 2)
            self.assertEqual(result.skipped_duplicates, 0)
        finally:
            conn.close()

    def test_skips_duplicates(self):
        obs = [_make_obs(1000, 80)]
        conn = get_connection(self._db)
        try:
            import_observations(conn, obs, _DEVICE, _USER, snapshot_id=1)
            conn.commit()
            # Re-import the same observations.
            result = import_observations(conn, obs, _DEVICE, _USER, snapshot_id=1)
            conn.commit()
            self.assertEqual(result.imported.get("heart_rate", 0), 0)
            self.assertEqual(result.skipped_duplicates, 1)
        finally:
            conn.close()

    def test_mixed_types_counted_separately(self):
        from app.adapters.base import RawObservation
        obs = [
            RawObservation(
                data_type="heart_rate", timestamp_utc=1000,
                normalized_value='{"bpm": 80}',
                raw_source='{}', source_table="T",
                source_rowid=1,
                dedup_key="heart_rate|1|1000|T|80",
            ),
            RawObservation(
                data_type="steps", timestamp_utc=1000,
                normalized_value='{"steps": 5}',
                raw_source='{}', source_table="T",
                source_rowid=1,
                dedup_key="steps|1|1000|T|5",
            ),
        ]
        conn = get_connection(self._db)
        try:
            result = import_observations(conn, obs, _DEVICE, _USER, snapshot_id=1)
            conn.commit()
            self.assertEqual(result.imported["heart_rate"], 1)
            self.assertEqual(result.imported["steps"], 1)
        finally:
            conn.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_normalizer.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现归一化器**

```python
# /srv/health-bridge/app/normalizer.py
"""Observation normalization and import with hash-based deduplication.

Each RawObservation has a deterministic dedup_key. Re-importing the
same Gadgetbridge database is idempotent: existing keys are skipped,
new keys are inserted. The import counts per data type are returned
for the upload response.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from app.adapters.base import RawObservation
from app.device_resolver import ResolvedDevice, ResolvedUser

_SHANGHAI_TZ = timezone(timedelta(hours=8))


@dataclass
class ImportResult:
    imported: dict[str, int] = field(default_factory=dict)
    skipped_duplicates: int = 0


def _utc_to_local_str(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=_SHANGHAI_TZ)
    return dt.isoformat()


def import_observations(
    server_conn: sqlite3.Connection,
    observations: list[RawObservation],
    device: ResolvedDevice,
    user: ResolvedUser,
    snapshot_id: int,
) -> ImportResult:
    imported: dict[str, int] = defaultdict(int)
    skipped = 0

    for obs in observations:
        # Check if the dedup_key already exists.
        existing = server_conn.execute(
            "SELECT 1 FROM observations WHERE dedup_key=?",
            (obs.dedup_key,),
        ).fetchone()
        if existing:
            skipped += 1
            continue

        server_conn.execute(
            "INSERT INTO observations "
            "(dedup_key, device_id, user_id, data_type, timestamp_utc, "
            "timestamp_local, normalized_value, raw_source, source_table, "
            "source_rowid, snapshot_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                obs.dedup_key,
                device.internal_id,
                user.internal_id,
                obs.data_type,
                obs.timestamp_utc,
                _utc_to_local_str(obs.timestamp_utc),
                obs.normalized_value,
                obs.raw_source,
                obs.source_table,
                obs.source_rowid,
                snapshot_id,
            ),
        )
        imported[obs.data_type] += 1

    return ImportResult(imported=dict(imported), skipped_duplicates=skipped)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_normalizer.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
git add app/normalizer.py app/tests/test_normalizer.py
git commit -m "feat: add observation normalizer with dedup and import counts"
```

---

## Task 10: 上传摄入管线

**Files:**
- Create: `/srv/health-bridge/app/ingest.py`
- Create: `/srv/health-bridge/app/tests/test_ingest.py`

**Interfaces:**
- Consumes: `ServerConfig`, uploaded gzip file, `SchemaInfo`, all adapters, `import_observations`
- Produces: `IngestResult` dataclass with `sha256: str`, `is_duplicate: bool`, `status: str`, `imported_counts: dict[str, int]`, `error: str | None`; function `process_upload(config: ServerConfig, upload_data: bytes, expected_sha256: str) -> IngestResult`

- [ ] **Step 1: 编写摄入管线的失败测试**

```python
# /srv/health-bridge/app/tests/test_ingest.py
"""Tests for the upload ingestion pipeline."""
import gzip
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.config import load_server_config
from app.database import init_database
from app.ingest import process_upload, IngestResult


def _create_gb_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE DEVICE (_id INTEGER PRIMARY KEY, NAME TEXT, "
            "MANUFACTURER TEXT, IDENTIFIER TEXT, TYPE INTEGER, "
            "TYPE_NAME TEXT, MODEL TEXT, ALIAS TEXT, PARENT_FOLDER INTEGER)"
        )
        conn.execute(
            "CREATE TABLE USER (_id INTEGER PRIMARY KEY, NAME TEXT, "
            "BIRTHDAY INTEGER, GENDER INTEGER)"
        )
        conn.execute(
            "CREATE TABLE XIAOMI_ACTIVITY_SAMPLE ("
            "TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, "
            "RAW_INTENSITY INTEGER, STEPS INTEGER, RAW_KIND INTEGER, "
            "HEART_RATE INTEGER, STRESS INTEGER, SPO2 INTEGER, "
            "DISTANCE_CM INTEGER, ACTIVE_CALORIES INTEGER, ENERGY INTEGER)"
        )
        conn.execute(
            "INSERT INTO DEVICE VALUES (1, 'Mi Band 8', 'Xiaomi', 'AA:BB', "
            "0, 'MIBAND8', 'M2239B1', NULL, NULL)"
        )
        conn.execute("INSERT INTO USER VALUES (1, 'user', 0, 2)")
        conn.execute(
            "INSERT INTO XIAOMI_ACTIVITY_SAMPLE VALUES "
            "(1000, 1, 1, 0, 0, 0, 80, 0, 0, 0, 0, -1)"
        )
        conn.execute("PRAGMA user_version = 202")
        conn.commit()
    finally:
        conn.close()


def _gzip_db(db_path: Path) -> tuple[bytes, str]:
    import hashlib
    raw = db_path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    compressed = gzip.compress(raw)
    return compressed, sha


class TestProcessUpload(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._data_dir = Path(self._tmp) / "data"
        self._data_dir.mkdir()
        self._gb_db = Path(self._tmp) / "gb.db"
        _create_gb_db(self._gb_db)
        init_database(self._data_dir / "health.sqlite3")
        self._cfg = load_server_config(environ={
            "HEALTH_UPLOAD_TOKEN": "upload-secret",
            "HEALTH_READ_TOKEN": "read-secret",
            "HEALTH_DATA_DIR": str(self._data_dir),
        })

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_valid_upload_imports_data(self):
        compressed, sha = _gzip_db(self._gb_db)
        result = process_upload(self._cfg, compressed, sha)
        self.assertEqual(result.status, "imported")
        self.assertFalse(result.is_duplicate)
        self.assertIn("heart_rate", result.imported_counts)
        self.assertEqual(result.imported_counts["heart_rate"], 1)

    def test_duplicate_upload_is_idempotent(self):
        compressed, sha = _gzip_db(self._gb_db)
        result1 = process_upload(self._cfg, compressed, sha)
        self.assertEqual(result1.status, "imported")

        result2 = process_upload(self._cfg, compressed, sha)
        self.assertTrue(result2.is_duplicate)
        self.assertEqual(result2.status, "duplicate")

    def test_invalid_sqlite_rejected(self):
        bad_data = gzip.compress(b"not a sqlite database at all")
        import hashlib
        sha = hashlib.sha256(b"not a sqlite database at all").hexdigest()
        result = process_upload(self._cfg, bad_data, sha)
        self.assertEqual(result.status, "validation_failed")

    def test_oversized_gzip_rejected(self):
        # Create data that decompresses to something large.
        huge = b"\x00" * (200 * 1024 * 1024 + 1)
        compressed = gzip.compress(huge)
        import hashlib
        sha = hashlib.sha256(huge).hexdigest()
        result = process_upload(self._cfg, compressed, sha)
        self.assertEqual(result.status, "validation_failed")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_ingest.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现摄入管线**

```python
# /srv/health-bridge/app/ingest.py
"""Upload ingestion pipeline: decompress → validate → inspect →
resolve → adapt → normalize → import.

The pipeline is the core server-side logic that turns a gzipped
Gadgetbridge database snapshot into normalized observations stored
in the server database.
"""
from __future__ import annotations

import gzip
import hashlib
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.config import ServerConfig
from app.database import get_connection, init_database
from app.schema_inspector import inspect_schema
from app.device_resolver import resolve_devices
from app.adapters.heart_rate import HeartRateAdapter
from app.adapters.steps import StepsAdapter
from app.adapters.sleep import SleepAdapter
from app.normalizer import import_observations

_SQLITE_MAGIC = b"SQLite format 3\x00"


@dataclass
class IngestResult:
    sha256: str
    is_duplicate: bool
    status: str  # "imported", "duplicate", "unsupported_schema", "validation_failed"
    imported_counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def _check_duplicate(server_db: Path, sha256: str) -> bool:
    conn = get_connection(server_db)
    try:
        row = conn.execute(
            "SELECT 1 FROM snapshots WHERE sha256=?", (sha256,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _record_snapshot(
    server_db: Path, sha256: str, filename: str,
    schema_fingerprint: str, db_version: str | None,
    validation_status: str, import_status: str,
    imported_counts: dict[str, int], error: str | None,
) -> int:
    import json
    conn = get_connection(server_db)
    try:
        cur = conn.execute(
            "INSERT INTO snapshots "
            "(sha256, received_at, source_filename, db_version, "
            "schema_fingerprint, validation_status, import_status, "
            "imported_counts, error_summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sha256,
                datetime.now(timezone.utc).isoformat(),
                filename,
                db_version,
                schema_fingerprint,
                validation_status,
                import_status,
                json.dumps(imported_counts),
                error,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def process_upload(
    config: ServerConfig,
    upload_data: bytes,
    expected_sha256: str,
) -> IngestResult:
    # Step 1: Decompress with size limit.
    try:
        decompressed = gzip.decompress(upload_data)
    except Exception as exc:
        return IngestResult(
            sha256=expected_sha256, is_duplicate=False,
            status="validation_failed", error=f"decompression failed: {exc}",
        )

    if len(decompressed) > config.max_uncompressed_bytes:
        return IngestResult(
            sha256=expected_sha256, is_duplicate=False,
            status="validation_failed",
            error=f"decompressed size {len(decompressed)} exceeds limit",
        )

    # Step 2: Verify SHA-256.
    actual_sha = hashlib.sha256(decompressed).hexdigest()

    # Step 3: Check for duplicate.
    if _check_duplicate(config.db_path, actual_sha):
        return IngestResult(
            sha256=actual_sha, is_duplicate=True, status="duplicate",
        )

    # Step 4: Validate SQLite magic.
    if decompressed[:16] != _SQLITE_MAGIC:
        _record_snapshot(
            config.db_path, actual_sha, "upload", "", None,
            "invalid", "rejected", {},
            "Not a valid SQLite database",
        )
        return IngestResult(
            sha256=actual_sha, is_duplicate=False,
            status="validation_failed", error="invalid SQLite magic",
        )

    # Step 5: Write to temp file and inspect.
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp.write(decompressed)
        tmp_path = Path(tmp.name)

    try:
        # PRAGMA quick_check.
        conn = sqlite3.connect(str(tmp_path))
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                _record_snapshot(
                    config.db_path, actual_sha, "upload", "", None,
                    "corrupt", "rejected", {},
                    f"quick_check failed: {row[0] if row else 'no result'}",
                )
                return IngestResult(
                    sha256=actual_sha, is_duplicate=False,
                    status="validation_failed", error="quick_check failed",
                )
        finally:
            conn.close()

        # Step 6: Inspect schema.
        schema_info = inspect_schema(tmp_path)

        if not schema_info.is_supported:
            # Retain raw snapshot, record unsupported schema.
            raw_path = config.raw_dir / f"{actual_sha}.db"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(decompressed)
            _record_snapshot(
                config.db_path, actual_sha, "upload",
                schema_info.fingerprint, schema_info.db_version,
                "valid", "unsupported_schema", {},
                "Schema not recognized by any adapter",
            )
            return IngestResult(
                sha256=actual_sha, is_duplicate=False,
                status="unsupported_schema",
            )

        # Step 7: Resolve devices and users.
        gb_conn = sqlite3.connect(str(tmp_path))
        server_conn = get_connection(config.db_path)
        try:
            devices, users = resolve_devices(gb_conn, server_conn)

            # Step 8: Run all adapters.
            all_observations = []
            for device, user in zip(devices, users):
                for adapter in [HeartRateAdapter(), StepsAdapter(), SleepAdapter()]:
                    all_observations.extend(
                        adapter.extract(gb_conn, device, user)
                    )

            # Step 9: Record snapshot and import observations.
            snapshot_id = _record_snapshot(
                config.db_path, actual_sha, "upload",
                schema_info.fingerprint, schema_info.db_version,
                "valid", "imported", {},
                None,
            )

            result = import_observations(
                server_conn, all_observations,
                devices[0] if devices else None,
                users[0] if users else None,
                snapshot_id=snapshot_id,
            )
            server_conn.commit()

            # Update snapshot with actual counts.
            import json
            server_conn.execute(
                "UPDATE snapshots SET imported_counts=? WHERE id=?",
                (json.dumps(result.imported), snapshot_id),
            )
            server_conn.commit()

            return IngestResult(
                sha256=actual_sha, is_duplicate=False,
                status="imported",
                imported_counts=result.imported,
            )
        finally:
            gb_conn.close()
            server_conn.close()
    finally:
        tmp_path.unlink(missing_ok=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_ingest.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
git add app/ingest.py app/tests/test_ingest.py
git commit -m "feat: add upload ingestion pipeline with validation and import"
```

---

## Task 11: 归档生成器

**Files:**
- Create: `/srv/health-bridge/app/archive.py`
- Create: `/srv/health-bridge/app/tests/test_archive.py`

**Interfaces:**
- Consumes: `ServerConfig`, `sqlite3.Connection` to server DB
- Produces: function `generate_archives(config: ServerConfig, week_id: str | None = None) -> list[str]` (returns generated week IDs)

- [ ] **Step 1: 编写归档生成器的失败测试**

```python
# /srv/health-bridge/app/tests/test_archive.py
"""Tests for the ISO week Markdown archive generator."""
import json
import tempfile
import unittest
from pathlib import Path

from app.config import load_server_config
from app.database import init_database, get_connection
from app.archive import generate_archives, get_iso_week


class TestArchiveGenerator(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._data_dir = Path(self._tmp) / "data"
        self._data_dir.mkdir()
        init_database(self._data_dir / "health.sqlite3")
        self._cfg = load_server_config(environ={
            "HEALTH_UPLOAD_TOKEN": "u",
            "HEALTH_READ_TOKEN": "r",
            "HEALTH_DATA_DIR": str(self._data_dir),
        })

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _insert_observation(self, ts: int, data_type: str, value: dict):
        conn = get_connection(self._data_dir / "health.sqlite3")
        try:
            conn.execute(
                "INSERT INTO devices (id, gb_device_id, name, identifier, "
                "model, type_name) VALUES (1, 1, 'Test', 'AA', 'M1', 'T1')"
            )
            conn.execute(
                "INSERT INTO users (id, gb_user_id, name) VALUES (1, 1, 'U1')"
            )
            from datetime import datetime, timezone, timedelta
            dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
            conn.execute(
                "INSERT INTO observations "
                "(dedup_key, device_id, user_id, data_type, timestamp_utc, "
                "timestamp_local, normalized_value, source_table, source_rowid) "
                "VALUES (?, 1, 1, ?, ?, ?, ?, 'T', 1)",
                (
                    f"{data_type}|1|{ts}|T|{value}",
                    data_type, ts, dt.isoformat(),
                    json.dumps(value),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_generates_heart_rate_archive(self):
        # 2026-07-09 is a Thursday in ISO week 28.
        self._insert_observation(1752048000, "heart_rate", {"bpm": 80})
        weeks = generate_archives(self._cfg)
        self.assertTrue(len(weeks) > 0)
        archive_dir = self._cfg.archives_dir
        # Check that a heart_rate.md file was created.
        hr_files = list(archive_dir.rglob("heart_rate.md"))
        self.assertTrue(len(hr_files) > 0)

    def test_summary_file_created(self):
        self._insert_observation(1752048000, "heart_rate", {"bpm": 80})
        generate_archives(self._cfg)
        summary_files = list(self._cfg.archives_dir.rglob("summary.md"))
        self.assertTrue(len(summary_files) > 0)

    def test_deterministic_output(self):
        self._insert_observation(1752048000, "heart_rate", {"bpm": 80})
        generate_archives(self._cfg)
        content1 = ""
        for f in self._cfg.archives_dir.rglob("heart_rate.md"):
            content1 = f.read_text()
            break

        generate_archives(self._cfg)
        content2 = ""
        for f in self._cfg.archives_dir.rglob("heart_rate.md"):
            content2 = f.read_text()
            break

        self.assertEqual(content1, content2)

    def test_get_iso_week(self):
        from datetime import datetime, timezone, timedelta
        dt = datetime(2026, 7, 9, tzinfo=timezone(timedelta(hours=8)))
        week = get_iso_week(dt)
        self.assertEqual(week, "2026-W28")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_archive.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现归档生成器**

```python
# /srv/health-bridge/app/archive.py
"""ISO week Markdown archive generator.

After a successful import, generates or regenerates Markdown archives
for all affected ISO weeks.  Archives are grouped by local date and
time within each week, using deterministic ordering and atomic writes.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.config import ServerConfig
from app.database import get_connection

_SHANGHAI_TZ = timezone(timedelta(hours=8))

_TYPE_LABELS = {
    "heart_rate": "Heart Rate",
    "steps": "Steps",
    "sleep_session": "Sleep Sessions",
    "sleep_stage": "Sleep Stages",
}


def get_iso_week(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def generate_archives(
    config: ServerConfig,
    week_id: str | None = None,
) -> list[str]:
    conn = get_connection(config.db_path)
    try:
        if week_id:
            weeks = [week_id]
        else:
            rows = conn.execute(
                "SELECT DISTINCT data_type, timestamp_utc FROM observations"
            ).fetchall()
            weeks = set()
            for row in rows:
                dt = datetime.fromtimestamp(row[1], tz=_SHANGHAI_TZ)
                weeks.add(get_iso_week(dt))
            weeks = sorted(weeks)

        for wk in weeks:
            _generate_week(config, conn, wk)
        return weeks
    finally:
        conn.close()


def _generate_week(
    config: ServerConfig,
    conn,
    week_id: str,
) -> None:
    week_dir = config.archives_dir / week_id
    week_dir.mkdir(parents=True, exist_ok=True)

    # Parse week_id to get date range.
    year_str, week_str = week_id.split("-W")
    year = int(year_str)
    week = int(week_str)

    # Monday of the ISO week.
    monday = datetime.fromisocalendar(year, week, 1, tz=_SHANGHAI_TZ)
    sunday = monday + timedelta(days=7)

    start_ts = int(monday.timestamp())
    end_ts = int(sunday.timestamp())

    # Get all data types for this week.
    type_rows = conn.execute(
        "SELECT DISTINCT data_type FROM observations "
        "WHERE timestamp_utc >= ? AND timestamp_utc < ? "
        "ORDER BY data_type",
        (start_ts, end_ts),
    ).fetchall()
    data_types = [r[0] for r in type_rows]

    # Generate per-type files.
    for dt_type in data_types:
        content = _generate_type_markdown(conn, dt_type, start_ts, end_ts, week_id)
        _atomic_write(week_dir / f"{dt_type}.md", content)

    # Generate summary.
    summary = _generate_summary(conn, data_types, start_ts, end_ts, week_id)
    _atomic_write(week_dir / "summary.md", summary)


def _generate_type_markdown(
    conn, data_type: str, start_ts: int, end_ts: int, week_id: str,
) -> str:
    rows = conn.execute(
        "SELECT timestamp_utc, timestamp_local, normalized_value "
        "FROM observations "
        "WHERE data_type=? AND timestamp_utc >= ? AND timestamp_utc < ? "
        "ORDER BY timestamp_utc",
        (data_type, start_ts, end_ts),
    ).fetchall()

    lines = [
        f"# {_TYPE_LABELS.get(data_type, data_type)} — {week_id}",
        "",
    ]

    current_date = None
    for row in rows:
        ts, local_str, value_json = row
        dt = datetime.fromtimestamp(ts, tz=_SHANGHAI_TZ)
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M:%S")

        if date_str != current_date:
            lines.append(f"## {date_str}")
            current_date = date_str

        value = json.loads(value_json)
        if data_type == "heart_rate":
            lines.append(f"- {time_str} — {value.get('bpm', '?')} BPM")
        elif data_type == "steps":
            steps = value.get("steps", value.get("steps_daily", 0))
            lines.append(f"- {time_str} — {steps} steps")
        elif data_type == "sleep_session":
            total = value.get("total_duration", 0)
            deep = value.get("deep_sleep_duration", 0)
            lines.append(f"- {time_str} — total {total}s, deep {deep}s")
        elif data_type == "sleep_stage":
            stage = value.get("stage_code", "?")
            lines.append(f"- {time_str} — stage {stage}")
        else:
            lines.append(f"- {time_str} — {json.dumps(value)}")

    lines.append("")
    return "\n".join(lines)


def _generate_summary(
    conn, data_types: list[str], start_ts: int, end_ts: int, week_id: str,
) -> str:
    lines = [
        f"# Health Archive Summary — {week_id}",
        "",
        "| Type | Records |",
        "|------|---------|",
    ]

    for dt_type in data_types:
        count = conn.execute(
            "SELECT COUNT(*) FROM observations "
            "WHERE data_type=? AND timestamp_utc >= ? AND timestamp_utc < ?",
            (dt_type, start_ts, end_ts),
        ).fetchone()[0]
        label = _TYPE_LABELS.get(dt_type, dt_type)
        lines.append(f"| {label} | {count} |")

    lines.append("")
    lines.append("## Files")
    for dt_type in data_types:
        lines.append(f"- [{dt_type}.md]({dt_type}.md)")

    lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_archive.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
git add app/archive.py app/tests/test_archive.py
git commit -m "feat: add ISO week Markdown archive generator"
```

---

## Task 12: Latest JSON 生成器

**Files:**
- Create: `/srv/health-bridge/app/latest.py`
- Create: `/srv/health-bridge/app/tests/test_latest.py`

**Interfaces:**
- Consumes: `ServerConfig`, server DB connection
- Produces: function `generate_latest(config: ServerConfig) -> None`

- [ ] **Step 1: 编写 latest 生成器的失败测试**

```python
# /srv/health-bridge/app/tests/test_latest.py
"""Tests for latest JSON file generation."""
import json
import tempfile
import unittest
from pathlib import Path

from app.config import load_server_config
from app.database import init_database, get_connection
from app.latest import generate_latest


class TestLatestGenerator(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._data_dir = Path(self._tmp) / "data"
        self._data_dir.mkdir()
        init_database(self._data_dir / "health.sqlite3")
        self._cfg = load_server_config(environ={
            "HEALTH_UPLOAD_TOKEN": "u",
            "HEALTH_READ_TOKEN": "r",
            "HEALTH_DATA_DIR": str(self._data_dir),
        })

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_generates_heart_rate_json(self):
        conn = get_connection(self._data_dir / "health.sqlite3")
        try:
            conn.execute(
                "INSERT INTO devices (id, gb_device_id, name, identifier, "
                "model, type_name) VALUES (1, 1, 'T', 'A', 'M', 'TT')"
            )
            conn.execute(
                "INSERT INTO users (id, gb_user_id, name) VALUES (1, 1, 'U')"
            )
            conn.execute(
                "INSERT INTO observations "
                "(dedup_key, device_id, user_id, data_type, timestamp_utc, "
                "timestamp_local, normalized_value, source_table, source_rowid) "
                "VALUES ('k1', 1, 1, 'heart_rate', 1000, "
                "'2026-07-11T12:00:00+08:00', '{\"bpm\": 80}', 'T', 1)"
            )
            conn.commit()
        finally:
            conn.close()

        generate_latest(self._cfg)
        hr_file = self._cfg.latest_dir / "heart_rate.json"
        self.assertTrue(hr_file.exists())
        data = json.loads(hr_file.read_text())
        self.assertIn("bpm", data)

    def test_no_observations_creates_no_file(self):
        generate_latest(self._cfg)
        # With no data, no latest files should exist.
        files = list(self._cfg.latest_dir.glob("*.json"))
        self.assertEqual(len(files), 0)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_latest.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 latest 生成器**

```python
# /srv/health-bridge/app/latest.py
"""Latest JSON file generator.

After a successful import, generates atomic latest JSON files for
each data type. These files are useful for inspection, backup, and
simple integrations that don't need the full API.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from app.config import ServerConfig
from app.database import get_connection

_DATA_TYPES = ("heart_rate", "steps", "sleep_session", "sleep_stage")


def generate_latest(config: ServerConfig) -> None:
    config.latest_dir.mkdir(parents=True, exist_ok=True)
    conn = get_connection(config.db_path)
    try:
        for data_type in _DATA_TYPES:
            row = conn.execute(
                "SELECT timestamp_utc, timestamp_local, normalized_value "
                "FROM observations WHERE data_type=? "
                "ORDER BY timestamp_utc DESC LIMIT 1",
                (data_type,),
            ).fetchone()
            if row is None:
                continue
            ts, local_str, value_json = row
            payload = {
                "data_type": data_type,
                "timestamp_utc": ts,
                "timestamp_local": local_str,
                "value": json.loads(value_json),
            }
            _atomic_write(
                config.latest_dir / f"{data_type}.json",
                json.dumps(payload, indent=2, sort_keys=True),
            )
    finally:
        conn.close()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_latest.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
git add app/latest.py app/tests/test_latest.py
git commit -m "feat: add latest JSON file generator with atomic writes"
```

---

## Task 13: FastAPI 应用和 API 端点

**Files:**
- Create: `/srv/health-bridge/app/models.py`
- Create: `/srv/health-bridge/app/main.py`
- Create: `/srv/health-bridge/app/tests/test_api_endpoints.py`

**Interfaces:**
- Consumes: all previous modules
- Produces: FastAPI `app` object with endpoints: `GET /health`, `POST /upload`, `GET /latest`, `GET /data`, `GET /weeks`, `GET /archive/{week}/{type}`

- [ ] **Step 1: 编写 API 端点的失败测试**

```python
# /srv/health-bridge/app/tests/test_api_endpoints.py
"""Tests for FastAPI endpoints."""
import gzip
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import load_server_config
from app.database import init_database
from app.main import create_app


def _create_gb_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE DEVICE (_id INTEGER PRIMARY KEY, NAME TEXT, "
            "MANUFACTURER TEXT, IDENTIFIER TEXT, TYPE INTEGER, "
            "TYPE_NAME TEXT, MODEL TEXT, ALIAS TEXT, PARENT_FOLDER INTEGER)"
        )
        conn.execute(
            "CREATE TABLE USER (_id INTEGER PRIMARY KEY, NAME TEXT, "
            "BIRTHDAY INTEGER, GENDER INTEGER)"
        )
        conn.execute(
            "CREATE TABLE XIAOMI_ACTIVITY_SAMPLE ("
            "TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, "
            "RAW_INTENSITY INTEGER, STEPS INTEGER, RAW_KIND INTEGER, "
            "HEART_RATE INTEGER, STRESS INTEGER, SPO2 INTEGER, "
            "DISTANCE_CM INTEGER, ACTIVE_CALORIES INTEGER, ENERGY INTEGER)"
        )
        conn.execute(
            "INSERT INTO DEVICE VALUES (1, 'Mi Band 8', 'Xiaomi', 'AA', "
            "0, 'MIBAND8', 'M2239B1', NULL, NULL)"
        )
        conn.execute("INSERT INTO USER VALUES (1, 'u', 0, 2)")
        conn.execute(
            "INSERT INTO XIAOMI_ACTIVITY_SAMPLE VALUES "
            "(1000, 1, 1, 0, 0, 0, 80, 0, 0, 0, 0, -1)"
        )
        conn.execute("PRAGMA user_version = 202")
        conn.commit()
    finally:
        conn.close()


class TestHealthEndpoint(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._data_dir = Path(self._tmp) / "data"
        self._data_dir.mkdir()
        init_database(self._data_dir / "health.sqlite3")
        self._cfg = load_server_config(environ={
            "HEALTH_UPLOAD_TOKEN": "upload-secret",
            "HEALTH_READ_TOKEN": "read-secret",
            "HEALTH_DATA_DIR": str(self._data_dir),
        })
        self._app = create_app(self._cfg)
        self._client = TestClient(self._app)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_health_no_auth(self):
        resp = self._client.get("/health/api/v1/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("status", data)
        self.assertEqual(data["status"], "ok")

    def test_upload_without_token_rejected(self):
        resp = self._client.post("/health/api/v1/upload")
        self.assertEqual(resp.status_code, 401)

    def test_upload_with_wrong_token_rejected(self):
        resp = self._client.post(
            "/health/api/v1/upload",
            headers={"X-Upload-Token": "wrong"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_upload_valid_data(self):
        gb_db = Path(self._tmp) / "gb.db"
        _create_gb_db(gb_db)
        raw = gb_db.read_bytes()
        compressed = gzip.compress(raw)
        import hashlib
        sha = hashlib.sha256(raw).hexdigest()

        resp = self._client.post(
            "/health/api/v1/upload",
            content=compressed,
            headers={
                "X-Upload-Token": "upload-secret",
                "X-Snapshot-SHA256": sha,
                "Content-Encoding": "gzip",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "imported")

    def test_latest_without_read_token_rejected(self):
        resp = self._client.get("/health/api/v1/latest?type=heart_rate")
        self.assertEqual(resp.status_code, 401)

    def test_latest_with_correct_token(self):
        resp = self._client.get(
            "/health/api/v1/latest?type=heart_rate",
            headers={"Authorization": "Bearer read-secret"},
        )
        self.assertEqual(resp.status_code, 200)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_api_endpoints.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 FastAPI 应用**

```python
# /srv/health-bridge/app/models.py
"""Pydantic models for API request/response."""
from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    version: str


class UploadResponse(BaseModel):
    sha256: str
    is_duplicate: bool
    status: str
    imported_counts: dict[str, int] = {}


class ObservationResponse(BaseModel):
    data_type: str
    timestamp_utc: int
    timestamp_local: str
    value: dict


class DataResponse(BaseModel):
    observations: list[ObservationResponse]
    next_cursor: str | None = None


class WeeksResponse(BaseModel):
    weeks: list[str]


class ErrorResponse(BaseModel):
    error: dict
    request_id: str
```

```python
# /srv/health-bridge/app/main.py
"""FastAPI application for the health-bridge VPS service.

Endpoints are rooted at /health/api/v1. The app is served by Uvicorn
on 127.0.0.1:8765 and proxied through Nginx.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, JSONResponse

from app.archive import generate_archives
from app.auth import verify_upload_token, verify_read_token
from app.config import ServerConfig
from app.database import get_connection
from app.ingest import process_upload
from app.latest import generate_latest
from app.models import (
    HealthResponse,
    UploadResponse,
    ObservationResponse,
    DataResponse,
    WeeksResponse,
    ErrorResponse,
)


def create_app(config: ServerConfig) -> FastAPI:
    app = FastAPI(title="Health Bridge", version="1.0.0")
    app.state.config = config

    @app.get("/health/api/v1/health")
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", version="1.0.0")

    @app.post("/health/api/v1/upload")
    async def upload(
        request: Request,
        x_upload_token: str = Header(default=""),
        x_snapshot_sha256: str = Header(default=""),
    ):
        cfg: ServerConfig = request.app.state.config
        if not verify_upload_token(x_upload_token, cfg):
            raise HTTPException(status_code=401, detail="invalid upload token")

        body = await request.body()
        result = process_upload(cfg, body, x_snapshot_sha256)

        if result.status == "validation_failed":
            return JSONResponse(
                status_code=422,
                content=UploadResponse(
                    sha256=result.sha256,
                    is_duplicate=False,
                    status="validation_failed",
                ).model_dump(),
            )

        if result.status == "unsupported_schema":
            return JSONResponse(
                status_code=202,
                content=UploadResponse(
                    sha256=result.sha256,
                    is_duplicate=False,
                    status="unsupported_schema",
                ).model_dump(),
            )

        # Generate archives and latest after successful import.
        if result.status == "imported":
            generate_archives(cfg)
            generate_latest(cfg)

        return UploadResponse(
            sha256=result.sha256,
            is_duplicate=result.is_duplicate,
            status=result.status,
            imported_counts=result.imported_counts,
        )

    @app.get("/health/api/v1/latest")
    async def latest(
        request: Request,
        type: str = Query(...),
        authorization: str = Header(default=""),
    ):
        cfg: ServerConfig = request.app.state.config
        token = authorization.removeprefix("Bearer ").strip()
        if not verify_read_token(token, cfg):
            raise HTTPException(status_code=401, detail="invalid read token")

        conn = get_connection(cfg.db_path)
        try:
            row = conn.execute(
                "SELECT timestamp_utc, timestamp_local, normalized_value "
                "FROM observations WHERE data_type=? "
                "ORDER BY timestamp_utc DESC LIMIT 1",
                (type,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="no data")
            return ObservationResponse(
                data_type=type,
                timestamp_utc=row[0],
                timestamp_local=row[1],
                value=json.loads(row[2]),
            )
        finally:
            conn.close()

    @app.get("/health/api/v1/data")
    async def data(
        request: Request,
        type: str = Query(...),
        frm: str = Query(None, alias="from"),
        to: str = Query(None),
        limit: int = Query(100, le=1000),
        authorization: str = Header(default=""),
    ):
        cfg: ServerConfig = request.app.state.config
        token = authorization.removeprefix("Bearer ").strip()
        if not verify_read_token(token, cfg):
            raise HTTPException(status_code=401, detail="invalid read token")

        conn = get_connection(cfg.db_path)
        try:
            query = (
                "SELECT timestamp_utc, timestamp_local, normalized_value "
                "FROM observations WHERE data_type=?"
            )
            params: list = [type]
            if frm:
                query += " AND timestamp_utc >= ?"
                params.append(int(frm))
            if to:
                query += " AND timestamp_utc < ?"
                params.append(int(to))
            query += " ORDER BY timestamp_utc ASC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return DataResponse(
                observations=[
                    ObservationResponse(
                        data_type=type,
                        timestamp_utc=r[0],
                        timestamp_local=r[1],
                        value=json.loads(r[2]),
                    )
                    for r in rows
                ]
            )
        finally:
            conn.close()

    @app.get("/health/api/v1/weeks")
    async def weeks(
        request: Request,
        authorization: str = Header(default=""),
    ):
        cfg: ServerConfig = request.app.state.config
        token = authorization.removeprefix("Bearer ").strip()
        if not verify_read_token(token, cfg):
            raise HTTPException(status_code=401, detail="invalid read token")

        if not cfg.archives_dir.exists():
            return WeeksResponse(weeks=[])

        week_dirs = sorted(
            d.name for d in cfg.archives_dir.iterdir() if d.is_dir()
        )
        return WeeksResponse(weeks=week_dirs)

    @app.get("/health/api/v1/archive/{week}/{type}")
    async def archive(
        request: Request,
        week: str,
        type: str,
        authorization: str = Header(default=""),
    ):
        cfg: ServerConfig = request.app.state.config
        token = authorization.removeprefix("Bearer ").strip()
        if not verify_read_token(token, cfg):
            raise HTTPException(status_code=401, detail="invalid read token")

        file_path = cfg.archives_dir / week / f"{type}.md"
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="archive not found")

        return PlainTextResponse(
            content=file_path.read_text(encoding="utf-8"),
            media_type="text/markdown; charset=utf-8",
        )

    return app
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_api_endpoints.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
git add app/models.py app/main.py app/tests/test_api_endpoints.py
git commit -m "feat: add FastAPI app with all v1 endpoints"
```

---

## Task 14: systemd 服务和 Nginx 集成

**Files:**
- Create: `/srv/health-bridge/deploy/health-bridge.service`
- Create: `/srv/health-bridge/deploy/nginx-health-bridge.conf`
- Create: `/srv/health-bridge/deploy/health-bridge.env.example`
- Create: `/srv/health-bridge/deploy/deploy.sh`

- [ ] **Step 1: 创建 systemd 服务文件**

```ini
# /srv/health-bridge/deploy/health-bridge.service
[Unit]
Description=Health Bridge FastAPI Service
After=network.target

[Service]
Type=simple
User=health-bridge
Group=health-bridge
WorkingDirectory=/srv/health-bridge
EnvironmentFile=/srv/health-bridge/deploy/health-bridge.env
ExecStart=/usr/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=5

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/srv/health-bridge/data /srv/health-bridge/archives /srv/health-bridge/latest /srv/health-bridge/logs
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: 创建 Nginx 配置片段**

```nginx
# /srv/health-bridge/deploy/nginx-health-bridge.conf
# Add this location block to the existing server block for
# oh-my-frontweb.duckdns.org. Do NOT replace the existing /chat/ location.

location ^~ /health/ {
    client_max_body_size 100m;
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
}
```

- [ ] **Step 3: 创建环境变量示例文件**

```sh
# /srv/health-bridge/deploy/health-bridge.env.example
# Copy to health-bridge.env and fill in real tokens.
# Generate tokens with: openssl rand -hex 32
HEALTH_UPLOAD_TOKEN=replace-with-random-upload-token
HEALTH_READ_TOKEN=replace-with-random-read-token
HEALTH_DATA_DIR=/srv/health-bridge/data
```

- [ ] **Step 4: 创建部署脚本**

```bash
#!/bin/bash
# /srv/health-bridge/deploy/deploy.sh
# Deployment script for the health-bridge VPS service.
# Run as root or with sudo.

set -euo pipefail

APP_DIR="/srv/health-bridge"
SERVICE_NAME="health-bridge"

echo "[1/7] Create service user"
if ! id -u health-bridge &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin health-bridge
fi

echo "[2/7] Create directories"
mkdir -p "$APP_DIR"/{data/incoming,data/raw,archives,latest,logs,deploy}

echo "[3/7] Copy application files"
# Assumes the caller is in the project root.
cp -r app/ "$APP_DIR/"
cp -r deploy/ "$APP_DIR/"

echo "[4/7] Check environment file"
if [ ! -f "$APP_DIR/deploy/health-bridge.env" ]; then
    echo "ERROR: $APP_DIR/deploy/health-bridge.env not found."
    echo "Copy health-bridge.env.example and fill in real tokens."
    exit 1
fi

echo "[5/7] Set ownership"
chown -R health-bridge:health-bridge "$APP_DIR"

echo "[6/7] Install systemd service"
cp "$APP_DIR/deploy/health-bridge.service" /etc/systemd/system/
systemctl daemon-reload

echo "[7/7] Start service"
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
echo "Done. Check: systemctl status $SERVICE_NAME"
echo "Verify: curl http://127.0.0.1:8765/health/api/v1/health"
```

- [ ] **Step 5: 提交**

```bash
cd /srv/health-bridge
chmod +x deploy/deploy.sh
git add deploy/
git commit -m "feat: add systemd service, nginx config, and deploy script"
```

---

## Task 15: 端到端测试和部署验收

**Files:**
- Create: `/srv/health-bridge/app/tests/test_e2e.py`
- Modify: existing test suite

**Interfaces:**
- Consumes: all modules

- [ ] **Step 1: 编写端到端测试**

```python
# /srv/health-bridge/app/tests/test_e2e.py
"""End-to-end test: upload a real Gadgetbridge DB snapshot through the API,
verify data appears in latest and archive endpoints."""
import gzip
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import load_server_config
from app.database import init_database
from app.main import create_app


def _create_gb_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("""
            CREATE TABLE DEVICE (_id INTEGER PRIMARY KEY, NAME TEXT,
                MANUFACTURER TEXT, IDENTIFIER TEXT, TYPE INTEGER,
                TYPE_NAME TEXT, MODEL TEXT, ALIAS TEXT, PARENT_FOLDER INTEGER);
            CREATE TABLE USER (_id INTEGER PRIMARY KEY, NAME TEXT,
                BIRTHDAY INTEGER, GENDER INTEGER);
            CREATE TABLE XIAOMI_ACTIVITY_SAMPLE (
                TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER,
                RAW_INTENSITY INTEGER, STEPS INTEGER, RAW_KIND INTEGER,
                HEART_RATE INTEGER, STRESS INTEGER, SPO2 INTEGER,
                DISTANCE_CM INTEGER, ACTIVE_CALORIES INTEGER, ENERGY INTEGER);
            CREATE TABLE XIAOMI_SLEEP_TIME_SAMPLE (
                TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER,
                WAKEUP_TIME INTEGER, IS_AWAKE INTEGER, TOTAL_DURATION INTEGER,
                DEEP_SLEEP_DURATION INTEGER, LIGHT_SLEEP_DURATION INTEGER,
                REM_SLEEP_DURATION INTEGER, AWAKE_DURATION INTEGER);
            CREATE TABLE XIAOMI_SLEEP_STAGE_SAMPLE (
                TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER,
                STAGE INTEGER);
            CREATE TABLE XIAOMI_DAILY_SUMMARY_SAMPLE (
                TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER,
                TIMEZONE INTEGER, STEPS INTEGER, HR_RESTING INTEGER,
                HR_MAX INTEGER, HR_MAX_TS INTEGER, HR_MIN INTEGER,
                HR_MIN_TS INTEGER, HR_AVG INTEGER, STRESS_AVG INTEGER,
                STRESS_MAX INTEGER, STRESS_MIN INTEGER, STANDING INTEGER,
                CALORIES INTEGER, SPO2_MAX INTEGER, SPO2_MAX_TS INTEGER,
                SPO2_MIN INTEGER, SPO2_MIN_TS INTEGER, SPO2_AVG INTEGER,
                TRAINING_LOAD_DAY INTEGER, TRAINING_LOAD_WEEK INTEGER,
                TRAINING_LOAD_LEVEL INTEGER, VITALITY_INCREASE_LIGHT INTEGER,
                VITALITY_INCREASE_MODERATE INTEGER, VITALITY_INCREASE_HIGH INTEGER,
                VITALITY_CURRENT INTEGER);
            INSERT INTO DEVICE VALUES (1, 'Mi Band 8', 'Xiaomi', 'AA:BB', 0, 'MIBAND8', 'M2239B1', NULL, NULL);
            INSERT INTO USER VALUES (1, 'user', 0, 2);
            INSERT INTO XIAOMI_ACTIVITY_SAMPLE VALUES
                (1752048000, 1, 1, 0, 0, 0, 80, 0, 0, 0, 0, -1),
                (1752048060, 1, 1, 0, 0, 0, 85, 0, 0, 0, 0, -1),
                (1752048120, 1, 1, 10, 5, 0, 0, 0, 0, 500, 1, -1);
            INSERT INTO XIAOMI_SLEEP_TIME_SAMPLE VALUES
                (1752048000, 1, 1, 1752051600, 0, 3600, 1200, 1800, 400, 200);
            INSERT INTO XIAOMI_SLEEP_STAGE_SAMPLE VALUES
                (1752048000, 1, 1, 1),
                (1752048300, 1, 1, 2);
            PRAGMA user_version = 202;
        """)
        conn.commit()
    finally:
        conn.close()


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._data_dir = Path(self._tmp) / "data"
        self._data_dir.mkdir()
        init_database(self._data_dir / "health.sqlite3")
        self._cfg = load_server_config(environ={
            "HEALTH_UPLOAD_TOKEN": "upload-secret",
            "HEALTH_READ_TOKEN": "read-secret",
            "HEALTH_DATA_DIR": str(self._data_dir),
        })
        self._app = create_app(self._cfg)
        self._client = TestClient(self._app)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_full_pipeline(self):
        # 1. Create and upload a Gadgetbridge DB.
        gb_db = Path(self._tmp) / "gb.db"
        _create_gb_db(gb_db)
        raw = gb_db.read_bytes()
        compressed = gzip.compress(raw)
        sha = hashlib.sha256(raw).hexdigest()

        resp = self._client.post(
            "/health/api/v1/upload",
            content=compressed,
            headers={
                "X-Upload-Token": "upload-secret",
                "X-Snapshot-SHA256": sha,
                "Content-Encoding": "gzip",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "imported")
        self.assertIn("heart_rate", data["imported_counts"])

        # 2. Query latest heart rate.
        resp = self._client.get(
            "/health/api/v1/latest?type=heart_rate",
            headers={"Authorization": "Bearer read-secret"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data_type"], "heart_rate")

        # 3. Query range data.
        resp = self._client.get(
            "/health/api/v1/data?type=heart_rate&limit=10",
            headers={"Authorization": "Bearer read-secret"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["observations"]), 2)

        # 4. List weeks.
        resp = self._client.get(
            "/health/api/v1/weeks",
            headers={"Authorization": "Bearer read-secret"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(len(resp.json()["weeks"]) > 0)

        # 5. Get archive.
        week = resp.json()["weeks"][0]
        resp = self._client.get(
            f"/health/api/v1/archive/{week}/heart_rate",
            headers={"Authorization": "Bearer read-secret"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("BPM", resp.text)

    def test_duplicate_upload_is_idempotent(self):
        gb_db = Path(self._tmp) / "gb.db"
        _create_gb_db(gb_db)
        raw = gb_db.read_bytes()
        compressed = gzip.compress(raw)
        sha = hashlib.sha256(raw).hexdigest()

        headers = {
            "X-Upload-Token": "upload-secret",
            "X-Snapshot-SHA256": sha,
            "Content-Encoding": "gzip",
        }

        resp1 = self._client.post(
            "/health/api/v1/upload", content=compressed, headers=headers
        )
        self.assertEqual(resp1.json()["status"], "imported")

        resp2 = self._client.post(
            "/health/api/v1/upload", content=compressed, headers=headers
        )
        self.assertTrue(resp2.json()["is_duplicate"])
```

- [ ] **Step 2: 运行端到端测试**

Run: `cd /srv/health-bridge && python -m pytest app/tests/test_e2e.py -v`
Expected: PASS (2 tests)

- [ ] **Step 3: 运行全量测试套件**

Run: `cd /srv/health-bridge && python -m pytest app/tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: 提交**

```bash
cd /srv/health-bridge
git add app/tests/test_e2e.py
git commit -m "test: add end-to-end test covering full upload-query-archive pipeline"
```

---

## 部署验收清单

部署到 VPS 后逐项确认：

- [ ] `systemctl status health-bridge` 显示 active (running)
- [ ] `curl http://127.0.0.1:8765/health/api/v1/health` 返回 `{"status":"ok"}`
- [ ] `curl https://oh-my-frontweb.duckdns.org/health/api/v1/health` 通过 Nginx 返回相同结果
- [ ] `curl https://oh-my-frontweb.duckdns.org/chat/` 行为不变（现有服务未受影响）
- [ ] 端口 8765 不对外公开（`ss -tlnp | grep 8765` 只显示 127.0.0.1）
- [ ] 推送客户端 `once` 模式上传成功，返回 imported + 心率计数
- [ ] `GET /latest?type=heart_rate` 返回最新心率
- [ ] `GET /weeks` 返回可用周列表
- [ ] `GET /archive/{week}/heart_rate` 返回 Markdown 归档
- [ ] 重复上传相同快照返回 duplicate，不产生重复观测
- [ ] 服务重启后数据完整，无重复
