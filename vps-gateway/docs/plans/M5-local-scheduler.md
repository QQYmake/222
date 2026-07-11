# M5 — LocalScheduler + 主动回合调度

## 范围

在服务启动时创建后台线程，按固定间隔产生 TimerTrigger，调用 TurnRunner 执行主动回合。
服务关闭时优雅停止后台线程。

## Task 分解

### Task 1: LocalScheduler

**数据输入:**
- `enabled: bool` — 来自 `ACTIVE_TURN_ENABLED`
- `interval_minutes: int` — 来自 `ACTIVE_TURN_INTERVAL_MINUTES`
- `instruction: str` — 来自 `ACTIVE_TURN_INSTRUCTION`
- `turn_runner: TurnRunner` — 已注入完整依赖

**针对输入数据执行的指令:**
1. 构造时启动一个 daemon 线程
2. 线程内循环：sleep(interval) → 生成 trigger_id → 检查锁 → 执行 turn_runner.run(timer_trigger)
3. trigger_id 格式: `"timer:" + slot_start_iso`（如 `timer:2025-01-15T10:00:00+00:00`）
4. slot_start = 当前时间对齐到 interval 边界（floor 到整点）
5. 使用 threading.Lock 保证同一时刻只有一个主动回合运行
6. 上一次未结束时跳过本次（`skip_timer_slot`），不排队
7. 异常不传播：捕获 → 记录 `active_turn_failed` → 继续下一周期
8. `enabled=False` 时线程不启动，`start()` 为 no-op
9. 提供 `shutdown()` 方法设置 `_running=False`，线程在下次循环退出

**数据输出:**
- `TimerTrigger` 实例（传给 turn_runner）
- 日志: `timer_slot_started`, `active_turn_failed`, `skip_timer_slot`, `active_turn_completed`

**伪代码:**
```text
class LocalScheduler:
    __init__(turn_runner, interval_minutes, instruction, enabled):
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        if enabled:
            self.start()

    start():
        self._running = True
        self._thread = Thread(target=self._loop, daemon=True)
        self._thread.start()

    shutdown():
        self._running = False

    _loop():
        while self._running:
            sleep(interval_minutes * 60)   # 先等一个完整间隔
            if not self._running:
                break
            slot = floor_to_interval(now_utc(), interval_minutes)
            trigger_id = "timer:" + slot.isoformat()
            if self._lock.locked():
                log("skip_timer_slot", trigger_id, reason="previous_turn_running")
                continue
            with self._lock:
                try:
                    trigger = TimerTrigger(
                        type="timer",
                        trigger_id=trigger_id,
                        fired_at=now_iso(),
                        instruction=self._instruction,
                    )
                    result = turn_runner.run(trigger)
                    log("active_turn_completed", trigger_id, result.outcome)
                catch error:
                    log("active_turn_failed", trigger_id, error)
                    # 不重试，等下一周期
```

### Task 2: App 工厂集成 Scheduler

**数据输入:**
- `Config` 实例
- `TurnRunner` 实例

**针对输入数据执行的指令:**
1. 在 `create_app()` 中构造 `LocalScheduler`
2. 把 scheduler 存到 `app.state.scheduler`
3. 注册 `@app.on_event("shutdown")` 调用 `scheduler.shutdown()`
4. `ACTIVE_TURN_ENABLED=false` 时仍创建 scheduler 但不启动线程

**数据输出:**
- FastAPI app 实例，`app.state.scheduler` 可被访问

### Task 3: 配置校验

**数据输入:**
- `Config` 实例

**针对输入数据执行的指令:**
1. `ACTIVE_TURN_ENABLED=true` 时校验 `ACTIVE_TURN_INTERVAL_MINUTES >= 1`
2. `UPSTREAM_MODEL` 为空时拒绝启动
3. `UPSTREAM_TOKEN_LIMIT_FIELD` 不在 `["max_completion_tokens", "max_tokens"]` 时拒绝启动

**数据输出:**
- `ValueError` 异常（启动时 fail-fast）

### Task 4: 启动脚本

**数据输入:**
- `pyproject.toml` 中的 `[project.scripts]`
- 环境变量

**针对输入数据执行的指令:**
1. 创建 `app/main.py` 作为 uvicorn 入口
2. 从 `.env` 加载配置 → `Config.load_from_env()` → `create_app(config)`
3. uvicorn 监听 `GATEWAY_HOST:GATEWAY_PORT`

**数据输出:**
- 可运行的 HTTP 服务

## 验收基线 (12.3 + 12.5)

| # | 验收项 | 测试方式 |
|---|--------|----------|
| 1 | 定时器能产生一次 TimerTrigger | 单元测试: mock turn_runner，验证 run() 被调用 |
| 2 | 主动回合使用与被动回合相同的四份 Sample | 集成测试: 真实 Sample 读取 |
| 3 | 同一 trigger_id 即使重新调用，Outbox 中至多一条 | 已在 M4 覆盖 |
| 4 | 上一次未结束时跳过本次 | 单元测试: 模拟长耗时回合 |
| 5 | 失败后不立即重试 | 单元测试: mock 抛异常，验证不立即再次调用 |
| 6 | 服务重启后消息仍可查询 | 已在 M4 覆盖 |
| 7 | 业务代码不包含硬编码本地绝对路径 | 代码审查 |
| 8 | Sample/SQLite/上游配置均来自环境变量 | 已在 Config 覆盖 |
