# -*- coding: utf-8 -*-
"""自定义认证后端：以 username 登录，区分「密码错误」与「账号未激活」。"""
from django.contrib.auth.backends import ModelBackend

from core.models import User


class UsernameModelBackend(ModelBackend):
    """仅校验 username + password 与未删除状态，返回 user（含未激活用户）。

    激活状态由视图单独判断并提示（对齐 AuthController::login 逻辑）。
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        try:
            user = User.objects.all_with_deleted().get(username=username)
        except User.DoesNotExist:
            # 时间均衡，避免通过响应时差探测用户名
            User().set_password(password)
            return None
        if user.check_password(password) and user.deleted_at is None:
            return user
        return None
