# weibo-crawler 项目架构分析

## 1. 项目简介

weibo-crawler 是一个 Python 编写的微博用户数据爬虫工具，目标是从 `m.weibo.cn`（微博移动版）抓取指定用户的全部微博内容和用户资料。项目源自开源项目 [dataabc/weibo-crawler](https://github.com/dataabc/weibo-crawler)，在此基础上增加了反封禁机制、Markdown 输出、LLM 分析、Flask API 服务等功能。

**核心能力**：
- 抓取单个或多个用户的全部微博（原创 + 转发）
- 下载微博中的图片、视频、Live Photo
- 抓取微博下的评论和转发内容
- 支持时间范围过滤、关键词搜索
- 支持增量追加爬取（只抓取上次运行后的新微博）
- 多种输出格式：CSV、JSON、Markdown、MySQL、MongoDB、SQLite、HTTP POST
- 内置反封禁机制（随机延迟、User-Agent 轮换、批次暂停、会话限制）
- 可选 LLM 集成（情感分析、摘要生成、异常检测）
- 提供 RESTful API 服务和 Docker 容器化支持

---

## 2. 技术栈总览

| 层次 | 技术选型 | 用途 |
|------|---------|------|
| 语言 | Python 3.12+ | 主要开发语言 |
| HTTP 客户端 | `requests` + `HTTPAdapter` | 发送 API 请求，内置重试和会话管理 |
| HTML 解析 | `lxml.etree` | 解析微博正文中的链接、话题、@用户等 |
| 配置解析 | `json5` | 支持注释和尾随逗号的 JSON5 格式 |
| 定时调度 | `schedule` | `__main__.py` 中的循环执行 |
| 进度显示 | `tqdm` | 分页爬取时的进度条 |
| Web 框架 | `Flask` | `service.py` 中的 REST API |
| 数据库驱动 | `pymysql`, `pymongo`, `sqlite3` | 三种可选数据库写入 |
| 图片处理 | `piexif` | 将微博发布时间写入图片 EXIF 元数据 |
| 通知推送 | PushDeer API | 错误/完成通知 |
| 容器化 | Docker + docker-compose | 生产环境部署 |
| LLM 集成 | OpenAI 兼容 API | 可选的情感分析、摘要、异常检测 |

---

## 3. 目录结构说明

```
weibo-crawler/
├── weibo.py                 # 🔴 核心文件 (~3538行)：Weibo 类 + 所有爬取/解析/存储逻辑
├── __main__.py              # 🟠 定时调度入口：schedule 循环 + Docker 默认 CMD
├── service.py               # 🟠 Flask API 服务器：RESTful 爬取任务管理
├── const.py                 # 🟡 全局运行时常量：MODE（overwrite/append）、cookie 校验、推送通知
├── config.json              # 🟡 用户配置文件（JSON5 格式，含注释）
├── requirements.txt         # 依赖列表
├── Dockerfile               # Docker 镜像构建
├── docker-compose.yml       # Docker 编排
├── logging.conf             # 日志配置
├── API.md                   # API 文档
├── README.md                # 项目说明
├── test_llm.py              # LLM 功能测试脚本
├── .gitignore               # Git 忽略规则
├── .dockerignore            # Docker 构建忽略规则
├── util/                    # 🟢 工具模块
│   ├── csvutil.py           #   CSV 读写 + 上次抓取微博 ID 追踪
│   ├── dateutil.py          #   日期转换工具
│   ├── notify.py            #   PushDeer 通知推送
│   └── llm_analyzer.py      #   LLM 分析集成（情感/摘要/异常检测）
├── log/                     # 日志输出（.gitignore）
└── weibo_data/              # 默认输出目录（可配置）
```

**状态标记**：
- 🔴 核心：修改频繁，需要充分理解
- 🟠 重要：系统入口和对外接口
- 🟡 关键：配置和控制逻辑
- 🟢 工具：辅助模块，职责单一

---

## 4. 核心启动流程

项目有三个独立的启动入口，但它们最终都汇聚到同一个执行路径：

### 4.1 入口一：`python weibo.py` — 单次爬取

```
weibo.py::main()
  ├─ config = get_config()          # json5.loads("config.json")
  ├─ wb = Weibo(config)             # 初始化 session/cookie/anti-ban/LLM
  └─ wb.start()                     # 开始爬取
```

特点：执行一次就退出。适合手动测试和单次批量任务。

### 4.2 入口二：`python __main__.py <interval_minutes>` — 定时循环

```
__main__.py::main(interval)
  ├─ schedule.every(interval).minutes.do(weibo.main)    # 注册定时任务
  ├─ weibo.main()                                        # 立即执行首次
  └─ while True:
        schedule.run_pending()                           # 非阻塞轮询
```

特点：永远运行。异常不中断循环，只记录日志。Docker 容器的默认入口。

### 4.3 入口三：`python service.py` — Flask API 服务

```
service.py (启动时)
  ├─ Flask app 监听 :5000
  ├─ schedule_refresh 线程启动（每 10 分钟自动爬取）
  └─ 等待 HTTP 请求

POST /refresh {"user_id_list": [...]}
  ├─ 创建任务 → task_id (UUID)
  ├─ ThreadPoolExecutor(max_workers=1) 提交执行
  └─ 返回 task_id + 状态 (202)

GET /task/<task_id>
  └─ 返回任务状态: PENDING / PROGRESS / SUCCESS / FAILED

GET /weibos
  └─ 从 SQLite 读取所有微博，按时间倒序

GET /weibos/<id>
  └─ 从 SQLite 读取单条微博
```

特点：通过 API 触发爬取，并发控制严格（同时最多 1 个任务）。API 数据仅从 SQLite 读取，因此 API 模式下 `write_mode` 必须包含 `sqlite`。

### 4.4 三者关系

```
     __main__.py          service.py           weibo.py
    (定时循环)            (Flask API)         (单次执行)
         │                    │                    │
         └────────────────────┼────────────────────┘
                              │
                         weibo.main()
                              │
                    get_config() → Weibo(config) → wb.start()
```

---

## 5. 爬虫执行流程

爬虫的核心执行逻辑集中在 `Weibo.start()` → `get_pages()` → `get_one_page()` 这三层调用链上。

### 5.1 用户级循环：`start()`

```
for each user in user_config_list:
    if 有关键词查询:
        for each query:
            self.query = query
            initialize_info()    # 重置 weibo/list/user/got_count
            get_pages()
    else:
        initialize_info()
        get_pages()
    
    export_comments_to_csv()     # 导出该用户的评论到独立 CSV
    
    if 使用 txt 文件配置:
        update_user_config_file() # 更新 txt 中的 since_date
```

### 5.2 页面级循环：`get_pages()`

```
1. get_user_info()
   ├─ API 请求用户基本资料 (containerid=100505{uid})
   ├─ API 请求用户详细资料 (containerid=230283{uid}_-_INFO)
   ├─ user_to_database() → csv/json/mongo/mysql/sqlite
   └─ 返回 last_weibo_id / last_weibo_date (供 append 模式使用)

2. get_page_count()  # 根据微博总数和每页数量计算总页数

3. for page in tqdm(pages):      # 分页迭代
     is_end = get_one_page(page)  # 单页处理
    
     if "need_rest":             # 防封禁暂停信号
         write_data()            # 先保存已爬取的数据
         perform_anti_ban_rest() # 休眠 3~10 分钟
         reset_crawl_stats()
         continue                # 从本页重新开始
     
     if is_end: break            # 日期过滤或 append 模式触发终止
     
     if page % 20 == 0:          # 每 20 页批量写入一次
         write_data()
     
     sleep(6~12s)                # 每 1~5 页随机延迟

4. write_data()                  # 尾页写入
```

### 5.3 单页处理：`get_one_page(page)`

这是最复杂的函数，集成了多重逻辑：

```
1. get_weibo_json(page)
   ├─ 构建 API 参数
   ├─ [防封禁] 随机 UA / Referer / Accept-Language
   ├─ [防封禁] 动态延迟 (8~15s, 随请求数递增)
   ├─ HTTP 请求 + 指数退避重试 (最多 5 次)
   └─ 验证码检测 → 打开浏览器 + 等待用户输入

2. 遍历 cards:
   ├─ card_type=11 → 展开 card_group
   └─ card_type=9  → get_one_weibo(card)
    
3. 对每条微博执行多层过滤（按优先级排列）:
   ├─ [Cookie 校验] 匹配隐藏微博文本 → 标记 cookie 有效
   ├─ [ID 去重]     weibo_id 已在 self.weibo_id_list 中 → 跳过
   ├─ [截止日期]    created_at > end_date → 跳过（置顶微博除外）
   ├─ [append 去重] weibo_id == last_weibo_id → 停止分页
   ├─ [起始日期]    created_at < since_date → 停止分页（置顶微博除外）
   └─ [原创过滤]    only_crawl_original && 是转发 → 跳过

4. 通过过滤的微博:
   ├─ self.weibo.append(wb)
   ├─ self.weibo_id_list.append(id)
   ├─ self.got_count += 1
   └─ [防封禁] should_pause_session() 检查
```

### 5.4 单条微博解析：`parse_weibo(weibo_info)`

从 API 返回的 JSON 中提取结构化字段：

| 字段 | 来源 | 处理方式 |
|------|------|---------|
| `id`, `bid` | `weibo_info["id"]`, `["bid"]` | 直接取值 |
| `text` | `weibo_info["text"]` | `lxml.etree.HTML` 解析，可选移除 HTML 标签 |
| `pics` | `weibo_info["pics"][].large.url` | 按逗号分隔；过滤掉 type=video 的条目；补充正文中的内嵌图片 |
| `video_url` | `pics[]` 中 type=video 的 `videoSrc` | 按分号分隔；回退到 `page_info.urls.media_info` (mp4_720p > mp4_hd > ...) |
| `live_photo_url` | `weibo_info["live_photo"]` | 直接取值 |
| `links` | 正文中的 `<a href>` | 排除 @用户/#话题/图片链接，标准化 sinaurl 跳转 |
| `location` | 正文中的位置图标 span | 正则匹配 |
| `topics` | 正文中的 `#话题#` | XPath 提取 |
| `at_users` | 正文中的 `@用户` | XPath 提取 href |
| `article_url` | 正文中的头条文章链接 | XPath 提取 |
| `attitudes/comments/reposts_count` | 对应 JSON 字段 | `string_to_int()` 转换 |
| `created_at` | 原始字符串 | `standardize_date()` → 提供精确时间 `full_created_at` |
| `edited`, `edit_count` | `edit_count` 字段 | 判断是否编辑过 |

---

## 6. 数据流说明

### 6.1 数据写入分发

`write_data()` 方法是一个分发器，根据 `config.json` 中 `write_mode` 列表逐一调用对应的写入函数：

```
write_data(wrote_count)
  │
  ├─ write_csv()          → {output_dir}/{screen_name}/{user_id}.csv
  ├─ write_json()         → {output_dir}/{screen_name}/{user_id}.json
  ├─ write_post()         → HTTP POST to api_url (JSON: {user, weibo[]})
  ├─ weibo_to_mysql()     → MySQL weibo.weibo 表 (INSERT ON DUPLICATE KEY UPDATE)
  ├─ weibo_to_mongodb()   → MongoDB weibo.weibo collection (find_one + upsert)
  ├─ weibo_to_sqlite()    → SQLite weibo/comment/repost 表 (INSERT OR REPLACE)
  │   └─ [可选] get_weibo_comments() → 递归翻页抓取评论
  │   └─ [可选] get_weibo_reposts()  → 递归翻页抓取转发
  ├─ write_markdown()     → {output_dir}/{screen_name}/[{month}/]{date}.md
  │   └─ [可选] download_markdown_images() → 图片按时间戳命名
  └─ download_files()
      ├─ img             → {output_dir}/{screen_name}/img/{原创|转发}微博图片/
      ├─ video           → {output_dir}/{screen_name}/video/{原创|转发}微博视频/
      └─ live_photo      → {output_dir}/{screen_name}/live_photo/{原创|转发}微博Live Photo视频/
```

### 6.2 写入时机

数据不是逐条写入的，而是**批量化**写入：
- 每爬完 20 页触发一次 `write_data()`
- 防封禁触发休息前，先保存已有数据
- 全部页面爬完后，写入剩余数据

### 6.3 评论/转发抓取数据流

评论和转发的抓取采用了**深度集成**模式——嵌入在 `weibo_to_sqlite()` 中，而非独立阶段：

```
weibo_to_sqlite()
  └─ for each weibo:
       ├─ sqlite_insert_weibo(weibo)
       ├─ if download_comment && comments_count > 0:
       │     get_weibo_comments()
       │       ├─ _get_weibo_comments_cookie()    # 新接口（需 cookie）
       │       │   └─ GET /comments/hotflow?mid={id}
       │       │   └─ 失败 → _get_weibo_comments_nocookie()  # 老接口
       │       └─ 递归翻页 → sqlite_insert_comments()
       │           └─ [可选] 下载评论图片
       │
       └─ if download_repost && reposts_count > 0:
             get_weibo_reposts()
               └─ _get_weibo_reposts_cookie()
                   └─ GET /api/statuses/repostTimeline?id={id}&page={n}
                   └─ 递归翻页 → sqlite_insert_reposts()
```

### 6.4 评论 CSV 导出

每个用户爬取完成后，`export_comments_to_csv_for_current_user()` 从 SQLite 中筛选该用户的评论，导出为独立的 `{screen_name}_comments.csv` 文件。

### 6.5 SQLite 数据库表结构

SQLite 共包含 5 张表：

```sql
-- 用户信息表
user (id, nick_name, gender, follower_count, follow_count, birthday, 
      location, ip_location, edu, company, reg_date, main_page_url, 
      avatar_url, bio)

-- 微博信息表  
weibo (id, bid, user_id, screen_name, text, article_url, topics, 
       at_users, pics, video_url, live_photo_url, location, created_at, 
       source, attitudes_count, comments_count, reposts_count, 
       retweet_id, edited, edit_count)

-- 评论表
comments (id, bid, weibo_id, root_id, user_id, created_at,
          user_screen_name, user_avatar_url, text, pic_url, like_count)

-- 转发表
reposts (id, bid, weibo_id, user_id, created_at, user_screen_name,
         user_avatar_url, text, like_count)

-- 二进制存储表（可选，通过 store_binary_in_sqlite 控制）
bins (id, ext, data BLOB, weibo_id, comment_id, path, url)
```

---

## 7. 关键模块职责

### 7.1 `weibo.py` — 爬虫核心单体（~3538 行）

这是整个系统的心脏。`Weibo` 类承担了所有职责，没有拆分为独立的子模块。主要职责分组：

| 方法组 | 行号范围 | 职责 |
|--------|---------|------|
| `__init__` + 配置验证 | 53–520 | 初始化 session/cookie/headers/anti-ban/LLM，验证所有配置项 |
| 防封禁控制 | 255–432 | 动态延迟计算、会话暂停判断、批次延迟、UA 轮换 |
| HTTP 请求层 | 542–658 | API 请求 + 验证码处理 + 指数退避重试 |
| 用户信息抓取 | 660–891 | 获取用户资料 + 写入数据库 |
| 微博内容解析 | 893–1570 | 图片/视频/Live Photo/Link 提取、HTML 标签处理、LLM 分析 |
| 微博日志打印 | 1572–1624 | 用户和微博信息的格式化日志输出 |
| 单条微博获取 | 1626–1668 | 区分原创/转发，判断长微博 |
| 评论/转发抓取 | 1672–1876 | 递归翻页抓取评论和转发 |
| 分页循环 | 1880–2034 | 单页处理 + 多层过滤 + 防封禁集成 |
| CSV 写入 | 2059–2179 | write_csv + csv_helper |
| JSON 写入 | 2181–2218 | write_json + update_json_data |
| POST 发送 | 2220–2253 | 带 token 的 HTTP POST 重试 |
| MongoDB 写入 | 2256–2285 | find_one + upsert |
| MySQL 写入 | 2287–2427 | INSERT ON DUPLICATE KEY UPDATE |
| SQLite 写入 | 2429–2767 | weibo/comments/reposts 写入 + 表创建 |
| Markdown 写入 | 2887–3282 | 按日期分组 + 增量去重 + 图片链接 + 引用块 |
| 文件下载 | 1065–1418 | 流式下载 + Magic Number 校验 + EXIF + 文件时间 |
| 主循环 | 3320–3480 | get_pages() + 防封禁休息逻辑 |
| 模块级函数 | 3483–3538 | handle_config_renaming, get_config, main |

### 7.2 `const.py` — 全局状态管理

使用 `import const` + 直接赋值的模式管理三个全局变量：

- `const.MODE`：`"overwrite"` 全量爬取 | `"append"` 增量爬取（要求 sqlite 在 write_mode 中）
- `const.CHECK_COOKIE`：cookie 有效性校验配置和内部标志位
- `const.NOTIFY`：PushDeer 推送通知开关和密钥

**架构特点**：这是一种跨模块共享可变状态的非标准模式。`get_one_page()` 和 `weibo_to_sqlite()` 会修改 `const.CHECK_COOKIE["CHECKED"]` 和 `["EXIT_AFTER_CHECK"]` 实现跨方法的通信。

### 7.3 `util/csvutil.py` — 增量爬取追踪

管理 `users.csv` 中用户的"上次抓取位置"：

- `insert_or_update_user()`：新用户首次插入返回空字符串；已有用户返回上次记录的 `"{weibo_id} {created_at}"`
- `update_last_weibo_id()`：爬取完成后更新为最新微博 ID

这个模块是 append 模式的基础设施——通过 `users.csv` 最后字段存储上次抓取进度。

### 7.4 `util/llm_analyzer.py` — AI 分析扩展

通过 OpenAI 兼容 API 对每条微博进行三项分析：
- 情感分析（积极/中性/消极）
- 摘要生成（50 字以内）
- 异常检测（正常/异常 + 原因）

通过 `config.json` 中 `llm_config` 字段激活，结果直接嵌入 `weibo["llm_analysis"]`。

### 7.5 `service.py` — API 服务

提供 RESTful 接口管理爬取任务，复用 `weibo.py` 的核心逻辑。并发控制通过 `ThreadPoolExecutor(max_workers=1)` + `task_lock` 实现。

### 7.6 `__main__.py` — 定时调度

简单的 `schedule` 循环包装，异常不会终止进程。

---

## 8. 配置项和环境变量

### 8.1 Cookie 配置优先级

```
WEIBO_COOKIE 环境变量（最高优先级）
    ↓ 若未设置
config.json 中的 cookie 字段
    ↓ 提取
SUB → 核心认证 cookie（用于 session 预热）
    ↓ 预热失败时
_T_WM / XSRF-TOKEN → 旧版指纹（备用）
```

### 8.2 运行模式

`const.MODE` 的两种模式：

| 模式 | 行为 | 前置条件 |
|------|------|---------|
| `"overwrite"` | 每次运行全量爬取所有微博 | 无 |
| `"append"` | 只抓取上次运行后的新微博 | `"sqlite"` 必须在 `write_mode` 中 |

### 8.3 反封禁配置

```json
{
    "anti_ban_config": {
        "enabled": true,                    // 总开关
        "max_weibo_per_session": 500,       // 单次运行最大微博数 → 触发休息
        "batch_size": 50,                   // 批次大小
        "batch_delay": 30,                  // 批次间延迟（秒）
        "request_delay_min": 8,             // 请求最小延迟（秒）
        "request_delay_max": 15,            // 请求最大延迟（秒）
        "max_session_time": 600,            // 最大运行时间（秒）→ 触发休息
        "max_api_errors": 5,                // 最大 API 错误数 → 触发休息
        "rest_time_min": 180,               // 休息时间最小值（秒）
        "random_rest_probability": 0.01,    // 每次休息后随机额外休息概率
        "user_agents": [...],               // UA 池
        "accept_languages": [...],          // Accept-Language 池
        "referer_list": [...]               // Referer 池
    }
}
```

### 8.4 关键环境变量

| 变量 | 用途 | 影响的文件 |
|------|------|-----------|
| `WEIBO_COOKIE` | 微博认证 Cookie（优先于 config.json） | `weibo.py` |
| `schedule_interval` | Docker 容器中的循环间隔（分钟） | `__main__.py` (Docker CMD) |

---

## 9. 依赖库说明

```
lxml==6.1.0              # HTML/XML 解析，用于提取微博正文中的结构化内容
pymongo==4.6.3            # MongoDB 驱动（可选，仅 write_mode 含 "mongo" 时）
PyMySQL==1.1.1            # MySQL 驱动（可选，仅 write_mode 含 "mysql" 时）
Requests==2.33.0          # HTTP 客户端，所有 API 请求的基础
schedule==1.2.1           # 定时任务调度（__main__.py 使用）
tqdm==4.66.3              # 终端进度条
json5>=0.9.25             # JSON5 解析器（支持注释、尾随逗号）
piexif~=1.1.3             # JPEG EXIF 元数据写入（write_time_in_exif 功能）
Flask==3.1.3              # Web 框架（service.py 使用）
```

其中 `lxml`、`Requests`、`tqdm`、`json5` 是核心必需依赖；其他为可选依赖，仅在启用对应功能时使用。

---

## 10. 错误处理、限流和重试机制

### 10.1 网络错误重试

所有 API 请求采用**指数退避重试**策略：

```
最大重试次数: 5
退避公式:   sleep_time = backoff_factor × 2^retry
           (backoff_factor = 5s)
           第1次重试等待: 10s
           第2次重试等待: 20s
           第3次重试等待: 40s
           第4次重试等待: 80s
```

HTTP 层面的重试通过 `requests.adapters.HTTPAdapter(max_retries=5)` 实现。

### 10.2 验证码处理

当 API 返回验证码 URL 时：
1. 自动调用 `webbrowser.open(captcha_url)` 打开浏览器
2. 用户在终端输入 `y`（继续）或 `q`（退出）
3. 输入 `y` 后，重置重试计数器，从当前页重新开始

### 10.3 防封禁限流机制

反封禁机制在三个层面工作：

| 层面 | 机制 | 触发条件 |
|------|------|---------|
| 请求级 | 动态延迟 8~15s，随请求次数递增 | 每次 API 请求前 |
| 批次级 | 每 50 条微博后暂停 30s | `crawl_stats["batch_count"] >= batch_size` |
| 会话级 | 满足任一条件即进入 3~10 分钟休息 | weibo 数量 ≥ 500；运行时间 > 600s；错误 ≥ 5；1% 随机概率 |

休息后自动重置统计计数器并继续爬取。

### 10.4 文件下载完整性校验

下载图片和视频时通过 **Magic Number** 校验文件完整性：

```
JPEG: 起始字节 0xFF 0xD8 0xFF, 结束字节 0xFF 0xD9
PNG:  起始字节 0x89 'PNG' 0x0D 0x0A 0x1A 0x0A, 结束字节 'IEND' 0xAE 0x42 0x60 0x82
```

文件不完整时自动重试（最多 3 次）。下载失败的 URL 写入 `not_downloaded.txt`。

### 10.5 流式下载防卡死

大文件下载采用流式读取（`stream=True`，64KB chunks），带 60 秒无数据超时——连续 60 秒没有收到任何数据则中断下载。

### 10.6 JSON 解码容错

`get_config()` 先尝试 `json5.loads()`（支持注释），失败后回退到 `json.loads()`（标准 JSON）。两种都失败才退出。

### 10.7 日志体系

```
log/
├── all.log       # 按天轮转 (TimedRotatingFileHandler)，保留 5 个备份
│                 # 记录 INFO 及以上级别
│                 # 格式: 时间 - 级别 - 消息
│
└── error.log     # 追加写入
                  # 记录 WARNING 及以上级别
                  # 格式: 时间 - 级别 - 文件名[:行号] - 消息
```

控制台输出 DEBUG 级别，只显示消息本身（无时间前缀）。

`weibo` logger 设置 `propagate=0`，避免与 root logger 重复输出。

---

## 11. 当前项目的优点

### 11.1 功能完整度高

支持从用户信息到微博内容、评论、转发的全量抓取，覆盖了七种输出格式和三种运行模式。开箱即用，配置灵活。

### 11.2 防封禁机制成熟

三层面限流（请求级、批次级、会话级）、UA 池轮换、随机延迟、自动休息——这套机制有效降低了被微博封禁的风险。所有防封禁参数均可通过配置文件调整。

### 11.3 Cookie 管理健壮

Cookie 提取和分段管理（核心 SUB + 备用 _T_WM/XSRF-TOKEN）+ session 预热机制，提高了认证的可靠性。支持环境变量注入，避免敏感信息出现在配置文件中。

### 11.4 配置解析灵活

JSON5 格式支持注释和尾随逗号，同时向后兼容标准 JSON。旧配置字段名自动映射（如 `filter` → `only_crawl_original`）。

### 11.5 文件下载可靠

流式下载 + Magic Number 完整性校验 + 自动重试 + 失败记录。EXIF 时间和文件系统时间注入是增值功能。

### 11.6 SQLite 集成深度好

SQLite 模式不仅存储微博，还独立抓取和存储评论、转发，甚至可选存储二进制数据。这为后续的 API 服务和查询功能提供了坚实的数据基础。

### 11.7 三种运行模式覆盖全场景

- 单次执行（测试/手动批量）
- 定时循环（生产环境持续运行）
- API 服务（系统集成）

---

## 12. 当前项目的风险和不足

### 12.1 单体文件过于庞大（结构风险）

`weibo.py` 约 3538 行，单一文件包含了 HTTP 请求、JSON 解析、字段提取、去重过滤、反封禁控制、六种格式写入、文件下载全部逻辑。这导致：

- 修改一个功能需要跨越数百行寻找相关代码
- 不同职责的代码高度耦合（例如 `get_one_page()` 同时处理 cookie 校验、日期过滤、防封禁检查）
- 难以编写单元测试

### 12.2 全局可变状态（设计风险）

`const.py` 通过 `import const; const.MODE = "overwrite"` 模式暴露可变全局状态。`const.CHECK_COOKIE` 的内部标志位（`CHECKED`, `EXIT_AFTER_CHECK`）在 `get_one_page()` 和 `weibo_to_sqlite()` 中被修改，作为跨方法的通信手段。这种模式难以追踪和调试。

### 12.3 无测试覆盖（质量风险）

项目没有任何自动化测试（唯一接近的是 `test_llm.py`，但它只是手动测试脚本）。修改代码后只能靠 `python weibo.py` 手动验证。

### 12.4 无代码质量工具（维护风险）

没有 linter、typechecker、formatter 配置。代码风格靠开发者自律维护。

### 12.5 错误处理不一致

- 致命错误通过 `sys.exit()` 直接退出（如配置验证失败）
- 网络错误通过重试+日志处理
- `get_one_page()` 的 `except Exception` 捕获了所有异常但只记录日志不返回任何信号

### 12.6 config.json 中存在真实 Cookie（安全风险）

仓库中的 `config.json` 包含一个真实的认证 Cookie。任何能访问仓库的人都可以使用这个 Cookie 操作对应微博账号。

### 12.7 Markdown 模式与其他模式的图片下载逻辑分裂

存在两套独立的图片下载逻辑：
- 非 Markdown 模式：`download_files()` → `handle_download()`（文件名含微博 ID）
- Markdown 模式：`download_markdown_images()` → `_download_weibo_images()`（文件名含时间戳）

两套逻辑的命名规则和目录结构不同，增加了维护成本。

### 12.8 Docker 挂载路径与配置不一致

`docker-compose.yml` 中挂载的是 `./weibo:/app/weibo`，但默认配置 `output_directory` 为 `"weibo_data"`。用户可能困惑为什么 Docker 里的数据没有出现在预期的目录。

### 12.9 Python 2 遗留代码

代码中保留了 `if sys.version < "3"` 的条件分支和 Python 2 的 `codecs.BOM_UTF8` 处理，但实际依赖（`lxml==6.1.0` 等）已不支持 Python 2。

---

## 13. 适合添加新功能的位置

根据代码的现有架构模式，以下位置最适合扩展新功能：

### 13.1 添加新输出格式

**位置**：`weibo.py` 中的两个位置

- `write_data()` 方法（行 3285）：在 `if "xxx" in self.write_mode:` 分支中添加新的写入调用
- `validate_config()` 方法（行 462）：在 `write_mode` 列表中注册新格式名

参考现有模式（如 `write_csv`、`write_json`）实现即可。

### 13.2 添加微博新字段提取

**位置**：`parse_weibo()` 方法（行 1524）

在现有字段提取后添加新的解析逻辑。注意同步更新 `get_result_headers()`（CSV 表头）、`parse_sqlite_weibo()`（SQLite 映射）、MySQL 表结构（如有需要）。

### 13.3 扩展 LLM 分析能力

**位置**：`util/llm_analyzer.py`

当前只有情感分析、摘要、异常检测三项。可以添加：
- 微博话题分类
- 用户画像标签
- 影响力评估

新增分析方法遵循 `analyze_sentiment()` 的模式，接收文本返回字典，然后在 `analyze_weibo()` 中调用并合并结果。

### 13.4 添加新的反封禁策略

**位置**：`Weibo` 类的防封禁方法组（行 255–432）

参考 `calculate_dynamic_delay()` 和 `should_pause_session()` 的模式。新的策略需要在 `get_one_page()` 或 `get_pages()` 的适当位置调用。

### 13.5 添加新的 API 端点

**位置**：`service.py`（行 113 之后）

参考 `/refresh`、`/weibos` 的现有模式即可。注意所有端点共享同一个 `ThreadPoolExecutor(max_workers=1)`。

### 13.6 添加新的数据库支持

**位置**：`weibo.py`

参考 `weibo_to_mysql()` / `weibo_to_mongodb()` 的模式。需要：
1. 在 `validate_config()` 中注册新格式名
2. 实现 `weibo_to_xxx()` 和 `user_to_xxx()` 方法
3. 在 `write_data()` 中调用

### 13.7 添加新的调度方式

**位置**：`__main__.py`

如果需要在 `schedule` 之外添加新的调度策略（如基于系统 crontab 的信号驱动），可在 `__main__.py` 中修改 `main()` 函数。

---

## 14. 后续功能开发建议

### 14.1 短期改进（提升稳定性）

**补齐测试框架**：为 `parse_weibo()`、`get_config()`、`validate_config()` 等纯函数添加 pytest 单元测试。这是投入产出比最高的改进。

**添加代码质量工具**：
```
# 推荐配置
ruff check .          # Python linter
mypy weibo.py         # 类型检查
```

**分离敏感配置**：将 `config.json` 中的 cookie 移到 `.env` 文件或环境变量，并更新 `.gitignore`。

**统一图片下载逻辑**：合并 Markdown 模式和非 Markdown 模式的图片下载，使用统一的接口处理。

**清理 Python 2 遗留代码**：移除 `sys.version < "3"` 分支和 `codecs.BOM_UTF8` 等 Python 2 兼容代码。

### 14.2 中期改进（提升可维护性）

**拆分 `weibo.py`**：按职责拆分为独立模块：

```
weibo/
├── __init__.py
├── config.py       # get_config(), validate_config()
├── session.py      # cookie 管理, session 预热, 随机 headers
├── client.py       # get_json(), get_weibo_json(), get_long_weibo()
├── parser.py       # parse_weibo(), standardize_info(), get_pics()...
├── crawler.py      # start(), get_pages(), get_one_page(), get_one_weibo()
├── writers/
│   ├── csv_writer.py
│   ├── json_writer.py
│   ├── sqlite_writer.py
│   ├── mysql_writer.py
│   ├── mongo_writer.py
│   └── markdown_writer.py
├── downloader.py   # download_files(), download_one_file()
└── anti_ban.py     # 防封禁控制逻辑
```

**消除 `const.py` 全局可变状态**：将运行模式、cookie 校验配置、通知配置改为通过 `Weibo` 构造函数参数注入。

### 14.3 长期改进（提升架构能力）

**支持多线程并行爬取**：当前是严格的单线程串行。可以为不同的用户分配独立的线程，共享全局的反封禁状态管理器。

**实现断点续传**：将爬取进度持久化到 SQLite，支持程序崩溃后从中断处恢复。当前只有 `start_page` 手动设置和 append 模式的 `last_weibo_id` 作为部分替代。

**添加 Web 管理界面**：在 Flask API 基础上增加前端页面，实现可视化的用户管理、爬取状态监控、数据浏览。

**实现微博实时监控**：除定时全量刷新外，增加高频轮询模式，监控目标用户的最新微博并实时通知。

**插件化输出格式**：将 write_mode 的扩展从硬编码改为插件注册机制，允许用户通过配置引入自定义输出处理器。

---

> **文档版本**: v1.0  
> **生成日期**: 2026-05-26  
> **适用范围**: weibo-crawler 项目当前代码版本
