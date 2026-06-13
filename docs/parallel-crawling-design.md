# 多线程并行爬取设计方案

> **策略**：按用户拆分（一个线程处理一个用户）+ 下载异步化  
> **目标**：支持多 cookie、减少总爬取时间、保持反封禁机制有效

---

## 1. 当前串行流程分析

### 1.1 时间线

```
total_time = cookie_session_preheat (~2s)
           + Σ per_user:
               sleep_between_users (~30-60s, 仅开头)
               + get_user_info (~5-10s, 含重试)
               + Σ per_page:
                   dynamic_delay (8-15s)
                   + network_request (~1-3s)
                   + parse_weibo (~0.1s)
                   + batch_delay (每50条暂停30s)
                   + random_page_delay (每1-5页 6-12s)
               + write_data (~1-5s, 每20页)
               + download_files (~N分钟, 取决于图片/视频数量)
```

### 1.2 关键瓶颈

| 阶段 | 占比 | 性质 | 可否并行 |
|------|------|------|:---:|
| API 请求（分页遍历） | 30-50% | IO 密集 | 不同用户可并行 |
| 图片/视频下载 | 40-60% | IO 密集 | ✅ 天然可并行 |
| anti-ban 人为延迟 | 10-30% | CPU 等待 | 取决于全局策略 |
| 解析 + 写入 | <5% | CPU 密集 | 不可加速 |

### 1.3 当前调用链

```
main()
  get_config()
  Weibo(config)                  ← 单 cookie → 单 session → 单 crawl_stats
  wb.start()
    for user_config in user_config_list:
      initialize_info()           ← 重置 per-user 状态
      get_pages()
        get_user_info()
        for page in pages:
          get_one_page(page)      ← 含 cookie 校验、日期过滤、防封禁
            get_weibo_json(page)
            get_one_weibo(card)
          write_data()            ← 每 20 页批量写入
        write_data()              ← 尾页写入
        download_files()          ← 同步阻塞下载
```

---

## 2. 可并行的边界

### 2.1 用户级并行（主策略）

**前提条件**：每个线程持有独立的 cookie 和 `requests.Session`。

```
Thread 1 (cookie_1, session_1):
  用户A: pages 1..N → write 用户A/ → download 用户A/

Thread 2 (cookie_2, session_2):
  用户B: pages 1..M → write 用户B/ → download 用户B/
```

**为什么安全**：
- 不同用户 → 不同的 API 请求参数（不同的 `containerid`）→ 无数据重叠
- 不同用户 → 不同的输出目录（`用户A/` vs `用户B/`）→ 文件级别隔离
- 不同 cookie → 不同的 `requests.Session` → 无认证冲突

### 2.2 下载异步化（辅助策略）

```
主线程:
  page=1 → parse → [提交到下载队列]
  page=2 → parse → [提交到下载队列]
  ...

下载线程池 (ThreadPoolExecutor, max_workers=3):
  从队列取任务 → download_one_file() → 完成
```

**为什么安全**：
- `download_one_file()` 已使用独立的 `requests.Session()`（第 1078 行）
- 每个文件的路径由日期和微博 ID 确定，天然唯一
- 下载线程与爬取线程没有共享可变状态

### 2.3 并行范围总结

| 操作 | 策略 A（用户级并行） | 下载异步化 | 同时启用 |
|------|:---:|:---:|:---:|
| 不同用户的 get_user_info | ✅ | — | ✅ |
| 不同用户的 get_pages | ✅ | — | ✅ |
| 同一用户的图片下载 | — | ✅ | ✅ |
| 同一用户的视频下载 | — | ✅ | ✅ |
| 不同用户的 Markdown 生成 | ✅ | — | ✅ |

---

## 3. 不可并行的部分

### 3.1 同一用户的页面爬取

`get_one_page()` 的日期终止逻辑（第 1967 行）依赖 page 顺序。并行处理同一用户的 page 号会导致终止条件失效。

**结论**：一个用户的页面必须串行处理。不在此方案范围内。

### 3.2 评论/转发下载

评论和转发抓取嵌入在 `weibo_to_sqlite()` 中，该功能依赖 SQLite 写入。此方案不支持评论/转发并行。

### 3.3 SQLite 写入

SQLite 并发写入有 `SQLITE_BUSY` 风险。此方案建议用户限制 `write_mode` 为 `["csv", "json"]`。

### 3.4 `const.py` Cookie 校验

`const.CHECK_COOKIE["CHECKED"]` 和 `["EXIT_AFTER_CHECK"]` 是全局可变状态，多线程同时修改会竞态。

**处理**：并行模式下自动禁用 cookie 校验，或将其改为 per-instance 变量。

### 3.5 append 模式

`validate_config()` 强制要求 `"sqlite" in write_mode`。并行模式下 append 不可用。

---

## 4. 推荐架构

### 4.1 架构图

```
                          main()
                            │
                  ┌─────────┴──────────┐
                  │  config.json       │
                  │  cookies: [...]    │
                  │  max_workers: N    │
                  └─────────┬──────────┘
                            │
                  ┌─────────▼──────────┐
                  │ GlobalAntiBanState │  ← 全局共享（RLock 保护）
                  │  - total_weibo     │
                  │  - total_requests  │
                  │  - total_errors    │
                  │  - start_time      │
                  │  - batch counters  │
                  └─────────┬──────────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
        ┌─────▼──────┐ ┌───▼───────┐ ┌───▼──────┐
        │ Worker 1   │ │ Worker 2  │ │ Worker N │  ThreadPoolExecutor
        │ ────────── │ │ ───────── │ │ ───────── │
        │ cookie_1   │ │ cookie_2  │ │ cookie_N  │  max_workers = len(cookies)
        │ session_1  │ │ session_2 │ │ session_N │
        │            │ │           │ │           │
        │ Worker.run():                    │
        │  while queue not empty:          │
        │    user = queue.get()            │
        │    get_pages(user)               │
        │    write_*()                     │
        │    download_files() ─ ─ ─ ─ ┐   │
        │    queue.task_done()         │   │
        │                             │   │
        │ ┌─────────────────────┐     │   │
        │ │ DownloadPool (per   │◄────┘   │
        │ │ worker, max=3)      │         │
        │ │ download_one_file() │         │
        │ └─────────────────────┘         │
        └─────────────────────────────────┘
              │             │             │
              └─────────────┼─────────────┘
                            │
                ┌───────────▼───────────┐
                │  Shared Resource Locks │
                │  - users_csv_lock     │
                │  - config_file_lock   │
                │  - not_downloaded_lock│
                └───────────────────────┘
```

### 4.2 核心组件

#### 4.2.1 `GlobalAntiBanState`（新建）

```python
class GlobalAntiBanState:
    """全局反封禁状态管理器"""
    
    def __init__(self, anti_ban_config):
        self._lock = threading.RLock()
        self._config = anti_ban_config
        self._enabled = anti_ban_config.get("enabled", False)
        
        # 所有线程共享的计数器
        self.stats = {
            "total_weibo": 0,
            "total_requests": 0,
            "total_api_errors": 0,
            "start_time": None,
        }
        # per-thread batch 计数（keyed by thread_id）
        self._batch_counters = {}
    
    def should_pause(self, thread_id):
        """任一条件满足 → 所有线程暂停"""
        with self._lock:
            if self.stats["total_weibo"] >= self._config["max_weibo_per_session"]:
                return True, "total_weibo"
            if self.stats["total_api_errors"] >= self._config["max_api_errors"]:
                return True, "api_errors"
            elapsed = time.time() - (self.stats["start_time"] or time.time())
            if elapsed > self._config["max_session_time"]:
                return True, "max_time"
            return False, ""
    
    def record_weibo(self, thread_id, count=1):
        with self._lock:
            self.stats["total_weibo"] += count
    
    def record_request(self, thread_id, count=1):
        with self._lock:
            self.stats["total_requests"] += count
    
    def record_error(self, thread_id):
        with self._lock:
            self.stats["total_api_errors"] += 1
    
    def reset(self):
        with self._lock:
            self.stats["total_weibo"] = 0
            self.stats["total_requests"] = 0
            self.stats["total_api_errors"] = 0
            self.stats["start_time"] = time.time()
```

#### 4.2.2 `CrawlerWorker`（新建）

每个 worker 持有一个 `Weibo` 实例，从共享队列中消费用户任务。

#### 4.2.3 `DownloadPool`（新建或嵌入 Weibo）

每个 worker 内部一个小型线程池，异步执行图片/视频下载。

### 4.3 数据流

```
user_config_list
  │
  ▼
Queue (thread-safe)     ← 所有用户任务
  │
  ├─ Worker 1: pop → get_pages(userX) → write → download (异步)
  ├─ Worker 2: pop → get_pages(userY) → write → download (异步)
  └─ Worker N: pop → get_pages(userZ) → write → download (异步)
  │
  ▼
  All workers:
    - 共享 GlobalAntiBanState（通过 lock 保护读写）
    - 独占 per-user 输出目录（无冲突）
    - 竞争 users.csv（通过 lock 保护）
```

---

## 5. 配置项设计

### 5.1 `config.json` 扩展

```json5
{
    // === 新增：多 Cookie 支持 ===
    // cookie 保留，向后兼容单 cookie 模式
    "cookie": "your single cookie",
    
    // 多 cookie 数组。若设置，优先于 cookie 字段。
    // 每个 worker 使用一个 cookie，worker 数量 = 数组长度
    "cookies": [
        "SUB=xxx; ...",
        "SUB=yyy; ..."
    ],
    
    // === 新增：并行控制 ===
    "parallel": {
        // max_workers: 爬取并行数。0 或 1 = 串行（向后兼容）。
        // 默认 = len(cookies)，不超过可用 cookie 数量。
        // 若只有 1 个 cookie，强制 = 1（即使设更大值）。
        "max_workers": 2,
        
        // download_workers: 每个 worker 内部的下载线程数。
        // 0 = 串行下载（原行为），默认 3。
        "download_workers": 3,
        
        // 启用关键字列表功能启用项
        // 设为 false 时，只做用户级并行，不做下载并行
        "enable": true
    },
    
    // === 原有配置（不变） ===
    "user_id_list": ["1223178222", "1669879400"],
    "write_mode": ["csv", "json"],    // 推荐限制为 csv + json
    // ...
}
```

### 5.2 Worker 数量决策逻辑

```
def determine_max_workers(config):
    if not config.get("parallel", {}).get("enable", True):
        return 1                             # 并行关闭 → 串行
    
    cookies = _get_cookies(config)
    configured = config["parallel"].get("max_workers", len(cookies))
    
    max_workers = min(configured, len(cookies))
    
    if max_workers <= 1:
        logger.info("并行模式已禁用（cookie 数量不足或配置为 1）")
    
    return max(1, max_workers)
```

### 5.3 向下兼容

| 场景 | 行为 |
|------|------|
| `config.json` 没有 `parallel` 字段 | 串行，原行为 |
| 有 `parallel` 但 `enable: false` | 串行 |
| 有 `cookies` + `parallel.enable: true` | 并行，worker 数 = min(max_workers, len(cookies)) |
| 只有 `cookie`（单 cookie） | 强制串行（即使 `max_workers > 1`） |
| `write_mode` 含 `sqlite` | 强制串行 + 日志警告 |
| `const.MODE == "append"` | 强制串行 + 日志警告 |

---

## 6. 线程安全风险

### 6.1 风险矩阵

| 共享资源 | 操作 | 风险 | 保护方式 |
|---------|------|------|---------|
| **GlobalAntiBanState.stats** | 多线程累加/读取 | 🔴 计数器竞态 | `threading.RLock` |
| **users.csv** | `insert_or_update_user()`、`update_last_weibo_id()` | 🔴 read-modify-write | `threading.Lock`（全局单例） |
| **user_config_file.txt** | `update_user_config_file()` | 🔴 全量读-全量写 | `threading.Lock`（全局单例） |
| **const.CHECK_COOKIE** | `get_one_page()` 中修改标志位 | 🟡 状态不一致 | 并行模式下禁用 |
| **not_downloaded.txt** | 多线程追加 | 🟡 行交错 | `threading.Lock` 或改为 per-user |
| **log/ 文件** | 多线程写 | 🟢 Python logging 线程安全 | 增加线程标识 |
| **per-user CSV** | 线程独占 | 🟢 无冲突 | 无需保护 |
| **per-user JSON** | 线程独占 | 🟢 无冲突 | 无需保护 |
| **per-user Markdown** | 线程独占 | 🟢 无冲突 | 无需保护 |
| **per-user img/video/** | 线程独占 | 🟢 无冲突 | 无需保护 |

### 6.2 Lock 实现要点

```python
# 全局锁定义
FILE_LOCKS = {
    "users_csv": threading.Lock(),
    "user_config_file": threading.Lock(),
    "not_downloaded": threading.Lock(),
}

# 使用模式
def insert_or_update_user(logger, headers, result_data, file_path):
    with FILE_LOCKS["users_csv"]:
        # ... 原有逻辑 ...

def update_last_weibo_id(userid, new_last_weibo_msg, file_path):
    with FILE_LOCKS["users_csv"]:
        # ... 原有逻辑 ...
```

### 6.3 为什么不需要 per-user 输出文件锁

因为工作分配策略保证了一个用户只分配给一个 worker：

```
Queue: [用户A, 用户B, 用户C, 用户D]
       │       │       │       │
     Worker1  Worker2  Worker1  Worker2
     (cookie1)(cookie2)(cookie1)(cookie2)
```

Worker1 处理用户A 和用户C，但是**串行**（先 A 后 C）。Worker2 处理用户B 和用户D，也是串行。同一时刻，没有任何两个线程在处理同一个用户。因此 per-user 文件不需要锁。

---

## 7. 反封禁风险

### 7.1 风险分析

| 风险 | 描述 | 严重程度 |
|------|------|:---:|
| **总请求速率翻倍** | 2 个 worker = 2× 请求速率，微博可能检测到异常 | 🔴 高 |
| **IP 级别限流** | 所有 worker 共享出口 IP | 🔴 高 |
| **Cookie 关联** | 多个 cookie 从同一 IP 并发请求，可能被关联分析 | 🟡 中 |
| **验证码风暴** | 一个 worker 触发验证码 → 所有 worker 被限流 | 🟡 中 |

### 7.2 缓解策略

#### 策略 1：全局速率限制器

```
不管理论上有多少 worker，全局请求间隔 >= request_delay_min。
在 GlobalAntiBanState 中维护 last_request_time:
  - 每个请求前: time.sleep(max(0, delay - (now - last_request_time)))
  - 更新 last_request_time
```

#### 策略 2：worker 数限制

```python
max_workers = min(
    config["parallel"]["max_workers"],
    len(cookies),
    3                            # 硬上限
)
```

#### 策略 3：动态增加 anti-ban 参数

```
if max_workers >= 2:
    anti_ban_config["request_delay_min"] *= 1.3
    anti_ban_config["request_delay_max"] *= 1.3
    anti_ban_config["batch_delay"] *= 1.5
```

#### 策略 4：共享休息

当一个 worker 触发 `should_pause` 返回 True 时，**所有 worker 同时暂停**。通过 `GlobalAntiBanState` 的 `should_pause()` 方法实现（所有 worker 调用同一个方法）。

### 7.3 安全的使用模式

| 场景 | 建议 |
|------|------|
| 少于 1000 条微博/用户 | 串行（禁用并行） |
| 1000-5000 条微博/用户 | 2 个 worker |
| 5000+ 条微博/用户 | 2 个 worker + 增加 anti-ban 延迟 |
| 只有 1 个 cookie | 不使用并行（收益为零） |
| 同一 IP 多个 cookie | 监控验证码触发率 |

---

## 8. 最小实现方案

### 8.1 四阶段实现计划

#### 第一阶段：提取 GlobalAntiBanState（无并行，零风险）

**目标**：将抗封禁状态从 `Weibo` 实例中提取为独立类，不改动调用方。

**改动文件**：新建 `util/anti_ban.py`

```python
# util/anti_ban.py
class GlobalAntiBanState:
    def __init__(self, config):
        ...
    def record_request(self):
        ...
    def record_weibo(self, count=1):
        ...
    def record_error(self):
        ...
    def should_pause(self):
        ...
    def reset(self):
        ...
```

**改动文件**：`weibo.py`

- `Weibo.__init__()`: 创建 `self.anti_ban_state = GlobalAntiBanState(anti_ban_config)`，替代 `self.crawl_stats`
- 所有 `self.crawl_stats["xxx"]` → `self.anti_ban_state.stats["xxx"]`
- 所有 `self.update_crawl_stats()` → `self.anti_ban_state.record_xxx()`
- 所有 `self.should_pause_session()` → `self.anti_ban_state.should_pause()`

**验证**：`python weibo.py` 行为与改动前完全一致。

**代码量**：~80 行新建 + ~30 行修改

#### 第二阶段：添加多 cookie 支持（仍串行，验证 cookie 切换）

**目标**：支持 `config.json` 中 `cookies` 数组，但仍串行执行——每个用户使用轮流的 cookie。

**改动文件**：`weibo.py`

- `get_config()`: 解析 `cookies` 字段
- `Weibo.__init__()`: 接受可选 `cookie_string` 参数（覆盖 `config["cookie"]`）
- `main()`: 为每个用户创建新的 `Weibo` 实例（使用下一个 cookie）

```
main():
    cookies = config.get("cookies") or [config["cookie"]]
    for i, user_config in enumerate(user_config_list):
        cookie = cookies[i % len(cookies)]
        config_copy = {**config, "user_id_list": [user_config["user_id"]]}
        config_copy["cookie"] = cookie  # 覆盖
        wb = Weibo(config_copy)
        wb.start()
```

**验证**：两台 cookie 分别在处理不同用户时使用。

**代码量**：~60 行修改

#### 第三阶段：用户级并行（核心并行逻辑）

**目标**：用 `ThreadPoolExecutor` 并行处理不同的用户。

**改动文件**：`weibo.py` 和/或 `__main__.py`

```
main():
    anti_ban = GlobalAntiBanState(config)
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for i, user_config in enumerate(user_config_list):
            cookie = cookies[i % len(cookies)]
            future = executor.submit(
                crawl_single_user, cookie, config, user_config, anti_ban, locks
            )
            futures.append(future)
        
        for future in futures:
            future.result()  # 等待所有完成，抛出异常
```

**改动文件**：新建 `util/worker.py`

```python
def crawl_single_user(cookie, config, user_config, anti_ban_state, locks):
    config = config.copy()
    config["cookie"] = cookie
    config["user_id_list"] = [user_config["user_id"]]
    
    wb = Weibo(config, anti_ban_state=anti_ban_state, locks=locks)
    wb.start()
```

**验证**：2 个 cookie + 4 个用户 = 2 个线程并行，每个处理 2 个用户（串行）。

**代码量**：~40 行新建 + ~20 行修改

#### 第四阶段：下载并行化（性能优化）

**目标**：每个 worker 内部用独立线程池执行图片/视频下载。

**改动文件**：`weibo.py`

- `download_files()`: 将下载任务提交到 `self.download_pool` 而非同步执行
- 主循环等待所有下载完成后再进入下一个用户

```
# 在 write_data() 或 download_files() 中:
for w in self.weibo[wrote_count:]:
    if w.get("pics"):
        self.download_executor.submit(self.handle_download, "img", dir, w["pics"], w)

# 每个用户结束前:
self.download_executor.shutdown(wait=True)  # 等待所有下载完成
```

**验证**：文件下载不会阻塞下一页的爬取。

**代码量**：~30 行修改

### 8.2 改动总览

| 阶段 | 文件 | 新建 | 修改 | 累积风险 |
|------|------|:---:|:---:|:---:|
| 1 | `util/anti_ban.py` | 80 行 | — | 低（不改变行为） |
| 1 | `weibo.py` | — | 30 行 | 低 |
| 2 | `weibo.py` | — | 60 行 | 中低（串行验证） |
| 3 | `util/worker.py` | 40 行 | — | 中（并行核心） |
| 3 | `weibo.py` | — | 20 行 | 中 |
| 4 | `weibo.py` | — | 30 行 | 中低（仅下载） |
| — | **总计** | **120 行** | **140 行** | **260 行** |

---

## 9. 测试方案

### 9.1 分层测试策略

```
第一层：单元测试（无需网络）
  ├─ GlobalAntiBanState 的计数器、锁、should_pause 逻辑
  ├─ get_config() 的 cookies 解析
  ├─ determine_max_workers() 的决策逻辑
  └─ 所有锁的上下文管理器

第二层：集成测试（需测试微博账号）
  ├─ 单 cookie + 2 个用户 → 串行（验证向后兼容）
  ├─ 2 cookie + 2 个用户 → 并行（验证基本并行）
  ├─ 2 cookie + 4 个用户 → 并行（验证队列分配）
  ├─ 配置了 3 worker 但只有 1 cookie → 串行（验证降级）
  ├─ write_mode 含 sqlite → 串行 + 警告（验证降级）
  └─ append 模式 → 串行 + 警告（验证降级）

第三层：压力测试
  ├─ 3 个用户 × 5000 条微博 = 验证不会触发 IP 限流
  ├─ 对比串行 vs 并行的总耗时
  └─ 验证 user_config_file.txt 更新正确
```

### 9.2 测试准备

| 准备项 | 说明 |
|--------|------|
| 测试用微博账号 | 2 个，获取各自的 cookie |
| 测试用目标用户 | 4 个（各 100-500 条微博的小号） |
| 测试用 config | 独立 `config.test.json`，不污染生产配置 |
| 测试用输出目录 | `weibo_test/`（`.gitignore` 已覆盖） |

### 9.3 快速验证命令

```bash
# 验证串行兼容性（不应有任何行为变化）
python weibo.py

# 验证基本并行（使用测试配置文件）
python -c "
import json5
with open('config.test.json') as f:
    config = json5.load(f)
config['parallel'] = {'max_workers': 2, 'enable': True}
config['cookies'] = [cookie1, cookie2]
# 运行...
"

# 验证下载并行（检查图片是否与串行一致）
diff -r weibo_test/  weibo_test_serial/
```

### 9.4 成功标准

| 指标 | 串行基线 | 并行目标 |
|------|---------|---------|
| 2 用户总耗时 | T | ≤ 0.7T |
| 图片数量 | N | N（完全一致） |
| CSV 行数 | M | M（完全一致） |
| JSON 结构 | — | 字段完全一致 |
| users.csv 完整性 | — | 所有用户都在 |
| `not_downloaded.txt` | — | 有记录即可（允许不同） |

---

## 10. 回滚方案

### 10.1 快速回滚

```json5
// config.json 中关闭并行
{
    "parallel": {
        "enable": false    // 设为 false → 完全回到串行模式
    }
}
```

如果 `parallel.enable` 为 false 或不存在 `parallel` 字段，所有改动代码走回原有路径。

### 10.2 代码级回滚

如果代码改动本身引入问题：

```
git revert <parallel-commit-hash>
```

因为改动集中在少数文件中，且每个阶段独立提交，回滚可以精确到阶段。

### 10.3 数据回滚

并行和串行的输出结构相同（per-user CSV/JSON/Markdown + per-user img/video 目录）。如果并行产生了错误的文件：

```bash
# 删除并行输出，用串行 re-run
rm -rf weibo_data/
python weibo.py    # 串行重爬
```

### 10.4 阶段回滚矩阵

| 如果... | 操作 |
|--------|------|
| 阶段 1 后出问题 | revert 阶段 1 的 commit（因为只是重构，不应该有行为差异） |
| 阶段 2 后 cookie 切换异常 | revert 阶段 2，阶段 1 保留 |
| 阶段 3 后数据不完整 | 关闭 `parallel.enable`，保留阶段 1+2 作为基础设施 |
| 阶段 4 后下载漏文件 | 设置 `download_workers: 0` → 回到串行下载，保留阶段 1-3 |
| 任一阶段验证码频繁触发 | 增加 `request_delay_min/max` 或减少 `max_workers` |

---

## 附录

### A. 关键文件索引

| 文件 | 改动性质 |
|------|---------|
| `util/anti_ban.py` | 新建：全局反封禁状态管理器 |
| `util/worker.py` | 新建：单用户爬取 worker 函数 |
| `weibo.py` | 修改：注入 anti_ban_state、平行分发、下载线程池 |
| `config.json` | 扩展：新增 `cookies` 和 `parallel` 字段 |

### B. 不修改的文件

| 文件 | 原因 |
|------|------|
| `const.py` | 全局状态不动；cookie 校验在并行下禁用 |
| `service.py` | 本身已有任务队列；`max_workers=1` 不变 |
| `__main__.py` | 定时调度逻辑不变 |
| `util/csvutil.py` | 只加锁；逻辑不变 |
| `util/llm_analyzer.py` | 只读配置，无并发问题 |
| `util/notify.py` | 只读 `const.NOTIFY`，无写操作 |
| `util/dateutil.py` | 纯函数，无状态 |

### C. 不建议做的事

1. **不要**尝试并行化同一用户的页面爬取（策略 B）——日期终止逻辑需要重写，投入产出比低
2. **不要**在 SQLite write_mode 下启用并行——并发写冲突无法简单解决
3. **不要**在只有 1 个 cookie 时启用并行——没有收益，引入线程开销
4. **不要**将 `max_workers` 设为 >3——即使用 3 个 cookie，反封禁压力过大
5. **不要**在日志中打印原始 cookie——并行日志更容易被意外分享
