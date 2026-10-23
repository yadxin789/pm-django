# -*- coding: utf-8 -*-
"""认证视图（对齐 AuthController）。"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from core.models import Department, Role, User
from core.services.operation_log import log_operation
from core.services.project_refresh import refresh_projects_on_login
from core.services.throttle import available_in, clear, hit, key_for, too_many


def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def show_login(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    return render(request, 'auth/login.html')


@never_cache
def login_view(request):
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        ip = _client_ip(request)
        key = key_for(username, ip)
        max_attempts = getattr(settings, 'PM_LOGIN_MAX_ATTEMPTS', 5)

        if too_many(key, max_attempts):
            messages.error(request, f'登录尝试次数过多，请在 {available_in(key)} 秒后重试')
            return redirect('login')

        user = authenticate(request, username=username, password=password)
        if user is not None:
            clear(key)
            if not user.is_active:
                messages.error(request, '您的账号尚未激活，请联系管理员')
                return redirect('login')
            login(request, user)
            request.session.cycle_key()
            log_operation('认证', '登录', f"用户「{user.name}」登录系统", user, ip)
            refresh_projects_on_login(user)
            return redirect('dashboard')

        hit(key, getattr(settings, 'PM_LOGIN_DECAY_MINUTES', 15) * 60)
        log_operation('认证', '登录失败', f"用户名「{username}」登录失败", None, ip)
        messages.error(request, '用户名或密码错误')
        return redirect('login')

    return show_login(request)


def show_register(request):
    if not getattr(settings, 'PM_REGISTRATION_OPEN', True):
        messages.error(request, '当前未开放注册')
        return redirect('login')
    departments = Department.objects.all()
    roles = Role.objects.all()
    return render(request, 'auth/register.html', {'departments': departments, 'roles': roles})


def register(request):
    if request.method != 'POST':
        return show_register(request)
    ip = _client_ip(request)
    username = request.POST.get('username', '').strip()
    password = request.POST.get('password', '')
    password_conf = request.POST.get('password_confirmation', '')
    name = request.POST.get('name', '').strip()
    department_id = request.POST.get('department_id', '')
    role_id = request.POST.get('role_id', '')

    # 校验（对齐 Laravel validate）
    if not username or not password or not name or not department_id or not role_id:
        messages.error(request, '请填写完整注册信息')
        return redirect('register')
    if len(username) > 50 or len(name) > 50:
        messages.error(request, '用户名或姓名长度不能超过50字符')
        return redirect('register')
    if len(password) < 6:
        messages.error(request, '密码长度不能少于6位')
        return redirect('register')
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        messages.error(request, '密码必须同时包含字母和数字')
        return redirect('register')
    if password != password_conf:
        messages.error(request, '两次输入的密码不一致')
        return redirect('register')
    if User.objects.filter(username=username).exists():
        messages.error(request, '该用户名已被使用')
        return redirect('register')
    if not Department.objects.filter(pk=department_id).exists():
        messages.error(request, '所选部门不存在')
        return redirect('register')
    if not Role.objects.filter(pk=role_id).exists():
        messages.error(request, '所选角色不存在')
        return redirect('register')

    user = User.objects.create_user(
        username=username, password=password, name=name,
        department_id=department_id, role_id=role_id, is_active=False,
    )
    log_operation('认证', '注册', f"新用户「{user.name}」注册成功（待管理员激活）", user, ip)
    messages.success(request, '注册成功，请等待管理员激活账号后登录')
    return redirect('login')


@never_cache
def logout_view(request):
    user = request.user
    user_name = user.name if user.is_authenticated else '未知用户'
    user_id = user.id if user.is_authenticated else None
    ip = _client_ip(request)
    logout(request)
    log_operation('认证', '退出', f"用户「{user_name}」退出登录", user if user.is_authenticated else None, ip)
    return redirect('home')
