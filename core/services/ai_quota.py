# -*- coding: utf-8 -*-
"""AI 每用户限流（每分钟/每日，管理员可调）+ 文档生成在途锁。

对齐参考目录：
  - AppServiceProvider 里 RateLimiter::for('ai')：按用户隔离，未登录退化为 IP；
  - AiController::generateDoc 的 Cache::lock('ai_doc_generate:{user}', 180)。

原 Laravel 用缓存驱动（database），gunicorn 多 worker 下本进程内存缓存不共享，
因此这里直接落 ai_rate_counters 表，保证跨 worker 计数与锁一致。
"""
from datetime import timedelta

from django.utils import timezone

from core.models import AiRateCounter


def _bucket_key(user, kind):
    uid = getattr(user, 'id', None)
    if uid:
        return f'u:{uid}:{kind}'
    return f'guest:{kind}'


def check_and_consume(user, setting):
    """检查并消耗一次 AI 调用配额。

    返回 (ok: bool, message: str)。超限返回 False 与中文提示。
    """
    now = timezone.now()
    minute_limit = max(1, int(setting.rate_limit_minute or 30))
    daily_limit = max(1, int(setting.rate_limit_daily or 200))

    for kind, window, limit in (
        ('minute', timedelta(minutes=1), minute_limit),
        ('daily', timedelta(days=1), daily_limit),
    ):
        key = _bucket_key(user, kind)
        row, _ = AiRateCounter.objects.get_or_create(key=key)
        if row.reset_at is None or row.reset_at <= now:
            row.count = 0
            row.reset_at = now + window
        if row.count >= limit:
            remain = int((row.reset_at - now).total_seconds())
            if kind == 'minute':
                return False, f'调用过于频繁：每分钟限 {limit} 次，请 {max(1, remain)} 秒后再试。'
            return False, f'今日调用已达上限 {limit} 次（由管理员在「AI 设置」调整），请明天再试。'
        row.count += 1
        row.save(update_fields=['count', 'reset_at', 'updated_at'])

    return True, ''


class DocGenLock:
    """文档生成在途锁：防止同一用户重复/并发提交浪费共享额度（180 秒）。"""

    TTL = timedelta(seconds=180)

    def __init__(self, user):
        self.key = 'docgen:' + (f'u:{user.id}' if getattr(user, 'id', None) else 'guest')
        self.acquired = False

    def __enter__(self):
        from django.db import transaction
        now = timezone.now()
        with transaction.atomic():
            row = AiRateCounter.objects.select_for_update().filter(key=self.key).first()
            if row and row.reset_at and row.reset_at > now:
                return False
            if row:
                row.count = 1
                row.reset_at = now + self.TTL
                row.save(update_fields=['count', 'reset_at', 'updated_at'])
            else:
                AiRateCounter.objects.create(key=self.key, count=1, reset_at=now + self.TTL)
        self.acquired = True
        return True

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

    def release(self):
        if self.acquired:
            AiRateCounter.objects.filter(key=self.key).delete()
            self.acquired = False
