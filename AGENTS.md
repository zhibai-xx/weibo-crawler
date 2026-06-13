# AGENTS.md

## Project

Weibo crawler — scrapes user profiles and weibo posts from m.weibo.cn.

## Three entrypoints

| Command | Purpose |
|---|---|
| `python weibo.py` | One-shot crawl using `config.json` |
| `python __main__.py <interval_minutes>` | Scheduled repeat crawl (used by Docker) |
| `python service.py` | Flask API server on port 5000 |

Docker uses `__main__.py` as CMD, not `weibo.py`.

## Configuration

`config.json` supports **JSON5** (trailing commas, comments) — parsed by the `json5` library. Standard JSON also works.

**Cookie priority:** `WEIBO_COOKIE` env var > `cookie` field in `config.json`.

**Run mode** is set in `const.py`:
- `const.MODE = "overwrite"` — full re-crawl every run
- `const.MODE = "append"` — incremental (requires `sqlite` in `write_mode`)

**Output directory** defaults to `weibo_data`, controlled by `output_directory` in config. Historical default was `weibo`.

## Key dependencies

```
lxml, pymongo, PyMySQL, Requests, schedule, tqdm, json5, piexif, Flask
```

Install: `pip install -r requirements.txt`

## Architecture

- `weibo.py` — `Weibo` class (~3500 lines): all crawling, parsing, file/db writing logic. `get_config()` reads config, `main()` drives a single crawl run.
- `const.py` — runtime constants: mode (overwrite/append), cookie check flags, push-deer notify config. Uses `import const` and mutates `const.MODE` / `const.CHECK_COOKIE` / `const.NOTIFY`.
- `__main__.py` — wraps `weibo.main()` in a `schedule` loop for periodic execution.
- `service.py` — Flask API with endpoints `/refresh`, `/task/<id>`, `/weibos`, `/weibos/<id>`. Reads from SQLite. Single-threaded task execution (max 1 concurrent crawl).
- `util/` — `csvutil.py` (csv read/write with last-weibo-id tracking), `dateutil.py`, `notify.py` (push_deer), `llm_analyzer.py` (optional LLM integration).

## Anti-ban behavior

When `anti_ban_config.enabled` is `true` (on by default in `config.json`): random delays between requests, batch pauses, User-Agent rotation, auto-pause after `max_weibo_per_session` weibos or `max_session_time` seconds. These pauses are logged but not surfaced to the calling code.

## Gotchas

- **No tests, no linter, no typechecker, no CI.** Test changes by running `python weibo.py` against a target user.
- `config.json` in the repo contains a real cookie — do NOT commit cookie changes.
- `test_llm.py` requires a `llm_config` section in `config.json` to work (API base, key, model).
- The `append` mode in `const.py` only works when `sqlite` is in `write_mode`.
- In `service.py`, the Flask `use_reloader=False` is set to prevent the scheduler thread from starting twice.
- The logging config is at `logging.conf`; logs go to `log/` directory. The `weibo` logger is separate from `root`.
- `not_downloaded.txt` files are auto-generated in per-user directories for failed image/video downloads.
