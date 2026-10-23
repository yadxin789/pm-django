# -*- coding: utf-8 -*-
"""登录限流（对齐 Laravel RateLimiter：PM_LOGIN_MAX_ATTEMPTS / PM_LOGIN_DECAY_MINUTES）。"""
from django.core.cache import cache


def key_for(username, ip):
    return f'login:{username}|{ip}'


def too_many(key, max_attempts):
    return (cache.get(key, 0) or 0) >= max_attempts


def hit(key, decay_seconds):
    cache.set(key, (cache.get(key, 0) or 0) + 1, decay_seconds)


def clear(key):
    cache.delete(key)


def available_in(key):
    ttl = cache.ttl(key)
    return ttl if (ttl is not None and ttl > 0) else 0
