import random
import threading
import time
from datetime import datetime, timedelta


class GlobalAntiBanState:
    """全局反封禁状态管理器（线程安全）。

    用于替代 Weibo 实例中的 self.crawl_stats，支持多线程共享。
    所有对 stats 的读写操作均通过内部 RLock 保护。
    """

    def __init__(self, config):
        self._lock = threading.RLock()
        self._config = config
        self._enabled = config.get("enabled", False)
        self._pause_event = threading.Event()

        self.weibo_count = 0
        self.request_count = 0
        self.api_errors = 0
        self.start_time = None
        self.batch_count = 0
        self.last_batch_time = None

    # ---- 兼容旧 crawl_stats dict 的 get/set ---- #

    def get(self, key, default=None):
        with self._lock:
            return getattr(self, key, default)

    def set(self, key, value):
        with self._lock:
            setattr(self, key, value)

    def inc(self, key, delta=1):
        with self._lock:
            current = getattr(self, key, 0)
            setattr(self, key, current + delta)

    # ---- 爬取统计记录 ---- #

    def record_weibo(self, count=1):
        with self._lock:
            self.weibo_count += count
            self.batch_count += count

    def record_request(self, count=1):
        with self._lock:
            self.request_count += count

    def record_error(self):
        with self._lock:
            self.api_errors += 1

    # ---- 批次管理 ---- #

    def update_batch_time(self):
        with self._lock:
            self.last_batch_time = time.time()

    def reset_batch_count(self):
        with self._lock:
            self.batch_count = 0

    def ensure_start_time(self):
        with self._lock:
            if self.start_time is None:
                self.start_time = time.time()

    # ---- 重置 ---- #

    def reset(self):
        with self._lock:
            self.weibo_count = 0
            self.request_count = 0
            self.api_errors = 0
            self.start_time = time.time()
            self.batch_count = 0
            self.last_batch_time = None
            self._pause_event.clear()

    # ---- 暂停判断 ---- #

    def should_pause(self):
        if not self._enabled:
            return False, ""

        with self._lock:
            current_time = time.time()

            max_weibo = self._config.get("max_weibo_per_session", 500)
            if self.weibo_count >= max_weibo:
                return True, f"达到单次运行最大微博数({max_weibo})"

            if self.start_time:
                session_time = current_time - self.start_time
                max_time = self._config.get("max_session_time", 600)
                if session_time > max_time:
                    return True, f"单次运行时间过长({int(session_time)}秒)"

            max_errors = self._config.get("max_api_errors", 5)
            if self.api_errors >= max_errors:
                return True, f"API错误过多({self.api_errors}次)"

            random_prob = self._config.get("random_rest_probability", 0.01)
            if random.random() < random_prob:
                return True, "随机休息"

        return False, ""

    # ---- 动态延迟计算 ---- #

    def get_dynamic_delay(self):
        if not self._enabled:
            return 0

        with self._lock:
            base_delay = self._config.get("request_delay_min", 8)

            if self.request_count > 100:
                base_delay += 5
            if self.request_count > 300:
                base_delay += 10

            if self.start_time:
                time_elapsed = time.time() - self.start_time
                if time_elapsed > 300:
                    base_delay += 5

        max_delay = self._config.get("request_delay_max", 15)
        return random.uniform(base_delay, max_delay)

    # ---- 批次延迟 ---- #

    def check_batch_delay(self, batch_size=None, batch_delay=None):
        if not self._enabled:
            return 0

        bs = batch_size or self._config.get("batch_size", 50)
        bd = batch_delay or self._config.get("batch_delay", 30)

        with self._lock:
            if self.batch_count >= bs:
                current_time = time.time()
                if self.last_batch_time:
                    time_since = current_time - self.last_batch_time
                    if time_since < bd:
                        return bd - time_since
                return bd
        return 0

    # ---- 休息时间 ---- #

    def get_rest_time(self):
        rest_time_min = self._config.get("rest_time_min", 600)
        return int(rest_time_min * random.uniform(0.9, 1.1))

    # ---- 全局暂停信号（并行模式下协调所有 worker）---- #

    @property
    def pause_event(self):
        return self._pause_event

    @property
    def enabled(self):
        return self._enabled

    @property
    def config(self):
        return self._config


def create_default_anti_ban_state(config):
    """创建默认的本地（非共享）反封禁状态管理器"""
    anti_ban_config = config.get("anti_ban_config", {})
    return GlobalAntiBanState(anti_ban_config)
