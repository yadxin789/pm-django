# -*- coding: utf-8 -*-
"""语音会议入场 token 服务（对齐 Laravel app/Services/VoiceTokenService.php）。

为每位入会的用户签发一个短期 token，Swoole 信令服务用它校验入参合法性。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Optional


def _secret() -> str:
    from django.conf import settings
    return getattr(settings, 'VOICE_TOKEN_SECRET', '') or getattr(settings, 'SECRET_KEY', 'pm-secret')


def _ttl() -> int:
    from django.conf import settings
    return int(getattr(settings, 'VOICE_TOKEN_TTL', 600))


def issue_token(*, project_id: int, user_id: int, role: str = '') -> str:
    """签发 token。payload 含 project_id / user_id / role / exp，使用 HMAC-SHA256 防伪。"""
    payload = {
        'project_id': project_id,
        'user_id': user_id,
        'role': role,
        'exp': int(time.time()) + _ttl(),
    }
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')
    sig = hmac.new(_secret().encode('utf-8'), body, hashlib.sha256).hexdigest()
    token_body = body.decode().encode('utf-8').hex()  # 用 hex 编码避免 URL 编码问题
    return f'{token_body}.{sig}'


def parse_token(token: str) -> Optional[dict]:
    """校验并解析 token；非法或过期返回 None。"""
    try:
        body_hex, sig = token.rsplit('.', 1)
        body = bytes.fromhex(body_hex).decode('utf-8')
    except (ValueError, UnicodeDecodeError):
        return None
    expected = hmac.new(_secret().encode('utf-8'), body.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if int(payload.get('exp', 0)) < int(time.time()):
        return None
    return payload