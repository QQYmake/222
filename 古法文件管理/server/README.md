# Health-Bridge Server

FastAPI 服务端，接收推送客户端上传的 Gadgetbridge SQLite 快照，
解析为归一化数据，生成 ISO 周 Markdown 归档和 latest.json。

## 架构

详见 `docs/server-architecture-plan.md`。

```
Push Client → Nginx :443 → FastAPI :8765 (127.0.0.1 only)
                              │
                    ┌─────────┼──────────┐
                    ▼                    ▼
              Ingest Pipeline      Read Endpoints
              (POST /upload)       (GET latest/data/
                    │              weeks/archive)
                    ▼
              Schema Inspector
                    │
                    ▼
              Device Resolver
                    │
                    ▼
              Type Adapters
              (heart_rate/steps/sleep)
                    │
                    ▼
              Normalizer
                    │
                    ▼
              SQLite (health.sqlite3)
                    │
              ┌─────┴──────┐
              ▼            ▼
         Archive Gen   Latest JSON
         (.md files)   (.json)
```

## 模块清单

| 模块 | 文件 | 职责 |
|------|------|------|
| Config | `app/config.py` | 环境变量/参数加载，路径管理 |
| Database | `app/database.py` | SQLite schema 初始化、CRUD、迁移 |
| Schema Inspector | `app/schema_inspector.py` | 探测上传 DB 的表结构，生成指纹 |
| Heart Rate Adapter | `app/adapters/heart_rate.py` | 心率数据提取 |
| Steps Adapter | `app/adapters/steps.py` | 步数数据提取（双源：逐采样+每日汇总） |
| Sleep Adapter | `app/adapters/sleep.py` | 睡眠数据提取（会话+分期） |
| Normalizer | `app/normalizer.py` | 时区转换、去重键生成、ISO 周计算 |
| Ingest Pipeline | `app/ingest.py` | 完整上传管线编排 |
| Archive Generator | `app/archive.py` | ISO 周 Markdown 归档生成 |
| Latest Generator | `app/latest.py` | latest.json 生成 |
| Auth | `app/auth.py` | 令牌校验（上传/读取分离） |
| API Entry | `app/main.py` | FastAPI 6 端点路由 |

## API 端点

| 端点 | 方法 | 认证 | 说明 |
|------|------|------|------|
| `/health/api/v1/health` | GET | 无 | 存活检查 |
| `/health/api/v1/upload` | POST | X-Upload-Token | 上传 gzip SQLite 快照 |
| `/health/api/v1/latest` | GET | Bearer | 最新值查询 |
| `/health/api/v1/data` | GET | Bearer | 时间范围查询（分页） |
| `/health/api/v1/weeks` | GET | Bearer | 列出可用归档周 |
| `/health/api/v1/archive/{week}` | GET | Bearer | 读取周归档 Markdown |

## 运行

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HEALTH_BRIDGE_DATA_DIR` | `/srv/health-bridge/data` | 数据目录 |
| `HEALTH_BRIDGE_ARCHIVES_DIR` | `/srv/health-bridge/archives` | 归档输出目录 |
| `HEALTH_BRIDGE_LATEST_DIR` | `/srv/health-bridge/latest` | latest.json 目录 |
| `HEALTH_BRIDGE_UPLOAD_TOKEN` | (无) | 上传令牌 |
| `HEALTH_BRIDGE_READ_TOKEN` | (无) | 读取令牌 |
| `HEALTH_BRIDGE_LISTEN_HOST` | `127.0.0.1` | 绑定地址 |
| `HEALTH_BRIDGE_LISTEN_PORT` | `8765` | 绑定端口 |

### 启动

```sh
cd server
pip install -r requirements.txt
export HEALTH_BRIDGE_UPLOAD_TOKEN="your-upload-token"
export HEALTH_BRIDGE_READ_TOKEN="your-read-token"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

### 测试

```sh
cd server
python -m pytest tests/ -v
```

### Nginx 集成

将 `nginx-health-bridge.conf` 的内容添加到现有 Nginx server 块中，
reload 后 `/health/` 路径将代理到 FastAPI。

### systemd 服务（可选）

```ini
[Unit]
Description=Health-Bridge API Server
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/health-bridge/server
Environment=HEALTH_BRIDGE_UPLOAD_TOKEN=your-token
Environment=HEALTH_BRIDGE_READ_TOKEN=your-token
ExecStart=/usr/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8765
Restart=on-failure

[Install]
WantedBy=multi-user.target
```
