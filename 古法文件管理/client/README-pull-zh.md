# Health-Bridge 拉取客户端 (health_pull)

从远程 Health-Bridge 服务端拉取健康数据（心率、步数、睡眠）。纯 Python 标准库实现，无第三方依赖，支持 Termux/Debian 和 Windows。

## 快速开始

```sh
# 1. 设置环境变量
export HEALTH_PULL_BASE_URL='https://your-server.com'
export HEALTH_READ_TOKEN='your-read-token'

# 2. 获取所有类型的最新值
python health_pull.py latest

# 3. 查询心率历史范围
python health_pull.py range heart_rate \
  --from 2026-07-01T00:00:00+08:00 \
  --to 2026-07-08T00:00:00+08:00

# 4. 下载周归档
python health_pull.py archive 2026-W28

# 5. 列出可用归档周
python health_pull.py weeks

# 6. 长期监听模式
python health_pull.py watch heart_rate steps_daily \
  --interval 60 \
  --output-dir ./latest
```

## 子命令

### `latest [type]`

获取最新观测值。

- 不带 type：返回所有类型的最新值（heart_rate / steps / steps_daily / sleep_stage）
- 带 type：仅返回指定类型

```sh
python health_pull.py latest heart_rate
```

### `range type --from --to --limit --cursor`

按时间范围查询观测值，支持分页。

```sh
python health_pull.py range heart_rate \
  --from 2026-07-01T00:00:00+08:00 \
  --to 2026-07-08T00:00:00+08:00 \
  --limit 50
```

响应包含 `next_cursor` 字段，用于翻页：

```sh
python health_pull.py range heart_rate --cursor "2026-07-11T05:22:00+00:00"
```

### `weeks`

列出所有可用的 ISO 周归档标识。

```sh
python health_pull.py weeks
# {"weeks": ["2026-W27", "2026-W28"]}
```

### `archive week_id`

下载指定周的 Markdown 归档。

```sh
python health_pull.py archive 2026-W28
```

### `watch types... --interval --output-dir`

长期轮询模式。定期拉取最新值，有变更时原子写入文件并打印通知。

```sh
python health_pull.py watch heart_rate steps_daily \
  --interval 60 \
  --output-dir ./latest
```

输出文件：
- `./latest/heart_rate.json` — 最新心率
- `./latest/steps_daily.json` — 最新每日步数

变更通知格式：`[2026-07-11T14:35:00+08:00] heart_rate: 84 bpm`

## 配置

### 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `HEALTH_PULL_BASE_URL` | `https://oh-my-frontweb.duckdns.org` | 服务端基础 URL |
| `HEALTH_READ_TOKEN` | (必需) | 读取令牌（Bearer） |
| `HEALTH_PULL_TIMEOUT` | `30` | HTTP 请求超时（秒） |
| `HEALTH_PULL_TIMEZONE` | `Asia/Shanghai` | 本地时区 |

### 配置文件

可选 JSON 配置文件（令牌不写入文件，仅从环境变量读取）：

```json
{
  "base_url": "https://your-server.com",
  "timeout_seconds": 45,
  "timezone": "America/New_York"
}
```

使用 `--config` 指定路径。

### 优先级

命令行参数 > 配置文件 > 环境变量 > 默认值

### 命令行覆盖

所有子命令支持以下参数覆盖环境变量：

- `--base-url` — 覆盖 `HEALTH_PULL_BASE_URL`
- `--token` — 覆盖 `HEALTH_READ_TOKEN`
- `--timeout` — 覆盖 `HEALTH_PULL_TIMEOUT`
- `--insecure` — 允许 HTTP（用于本地测试）
- `--output` — 输出到文件而非 stdout

## 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 配置错误或认证失败 |
| 2 | 网络错误（连接失败、超时） |
| 3 | 资源未找到（404） |
| 130 | Ctrl+C 中断（watch 模式） |

## 安全

- 读取令牌永不写入配置文件、日志或错误消息
- 生产环境强制 HTTPS（`--insecure` 仅用于本地测试）
- 响应体有界读取（最大 1 MiB），防止内存溢出
- Watch 模式文件写入使用原子操作（临时文件 → rename）
- 拉取客户端使用只读令牌，无法上传、删除或修改服务端数据

## 数据类型

| 类型 | 说明 | 示例值 |
|------|------|--------|
| `heart_rate` | 心率（来自 XIAOMI_ACTIVITY_SAMPLE） | `{"bpm": 84}` |
| `steps` | 逐采样步数（来自 XIAOMI_ACTIVITY_SAMPLE） | `{"steps": 120}` |
| `steps_daily` | 每日步数汇总（来自 XIAOMI_DAILY_SUMMARY_SAMPLE） | `{"source": "daily_summary", "steps": 5000}` |
| `sleep_stage` | 睡眠分期（来自 XIAOMI_SLEEP_STAGE_SAMPLE） | `{"stage": 3}` |

> **注意：** 睡眠分期 stage 为原始整数代码，不作医学诊断映射。

## 文件结构

```
client/
  health_pull.py              # CLI 入口
  health_bridge/
    pull_config.py            # 配置加载
    pull_transport.py         # HTTP 传输层
    pull_commands.py          # 命令处理器 (latest/range/weeks/archive)
    pull_watch.py             # watch 模式轮询
    pull_output.py            # 输出格式化
  tests/
    test_pull_config.py       # 15 个测试
    test_pull_transport.py    # 12 个测试
    test_pull_commands.py     # 10 个测试
    test_pull_watch.py        # 6 个测试
    test_health_pull_cli.py   # 13 个测试
  examples/
    pull-config.example.json
```
