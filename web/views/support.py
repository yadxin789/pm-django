# -*- coding: utf-8 -*-
"""支援中心：对齐 Laravel SupportController.php（部门资料文件 + 部门通知）。"""
import os
import uuid

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import redirect, render

from core.models import DepartmentFile, User
from core.services.task_service import _notify

from .feedback import deny, ok

MEDIA = settings.MEDIA_ROOT


def _visible_q(user):
    return Q(visibility__in=('all', 'department')) | Q(visibility='private', uploader_id=user.id)


def _files(user):
    return (DepartmentFile.objects.select_related('uploader')
            .filter(department_id=user.department_id)
            .filter(_visible_q(user)).order_by('-created_at'))


@login_required
def index(request):
    user = request.user
    files = Paginator(_files(user), 10).get_page(request.GET.get('page'))
    base = DepartmentFile.objects.filter(department_id=user.department_id)
    stats = {'total_files': base.filter(_visible_q(user)).count(),
             'my_files': base.filter(uploader_id=user.id).count()}
    return render(request, 'support/index.html', {'files': files, 'stats': stats})


@login_required
def department_files(request):
    """本部门资料文件列表（支持按文件名 / 分类筛选，对齐支援中心的整体设计）。"""
    user = request.user
    qs = _files(user)
    keyword = (request.GET.get('keyword') or '').strip()
    category = (request.GET.get('category') or '').strip()
    if keyword:
        qs = qs.filter(original_name__icontains=keyword)
    if category in DepartmentFile.category_options():
        qs = qs.filter(category=category)
    else:
        category = ''
    files = Paginator(qs, 10).get_page(request.GET.get('page'))
    return render(request, 'support/department-files.html', {
        'files': files,
        'categories': DepartmentFile.category_options(),
        'keyword': keyword,
        'category': category,
    })


@login_required
def send_notification(request):
    return render(request, 'support/send-notification.html')


@login_required
def post_notification(request):
    title = (request.POST.get('title') or '').strip()
    content = (request.POST.get('content') or '').strip()
    if not title or not content:
        return deny(request, '标题和内容都不能为空。', fallback_view='support.send-notification')
    user = request.user
    count = 0
    for u in User.objects.filter(department_id=user.department_id):
        _notify(u.id, title, content, 'department', 0, user.id, ntype='department')
        count += 1
    ok(request, '部门通知已发送给 %d 位同事。' % count)
    return redirect('support.send-notification')


@login_required
def upload_resource_file(request):
    return render(request, 'support/upload-resource-file.html',
                  {'categories': DepartmentFile.category_options(),
                   'visibility_options': DepartmentFile.visibility_options()})


@login_required
def store_resource_file(request):
    user = request.user
    uploaded = request.FILES.get('file')
    category = request.POST.get('category', '')
    visibility = request.POST.get('visibility', '')
    if not uploaded:
        return deny(request, '请先选择要上传的文件。', fallback_view='support.index')
    if category not in DepartmentFile.category_options():
        return deny(request, '请选择有效的资料分类。', fallback_view='support.index')
    if visibility not in DepartmentFile.visibility_options():
        return deny(request, '请选择有效的可见范围。', fallback_view='support.index')
    original_name = uploaded.name
    existing = (DepartmentFile.objects.filter(original_name=original_name, department_id=user.department_id)
                .order_by('-version').first())
    version = existing.version + 1 if existing else 1
    ext = original_name.rsplit('.', 1)[-1].lower() if '.' in original_name else ''
    file_name = '%s_v%d.%s' % (uuid.uuid4().hex[:13], version, ext)
    # 与 file.py::store_department_file 保持一致：部门资料文件统一落在
    # file_repository/department-{id}/ 下，DB 存 MEDIA_ROOT 相对路径
    rel = 'file_repository/department-%d/%s' % (user.department_id, file_name)
    full = os.path.join(MEDIA, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'wb') as f:
        for chunk in uploaded.chunks():
            f.write(chunk)
    try:
        with transaction.atomic():
            DepartmentFile.objects.create(
                department_id=user.department_id, uploader_id=user.id, name=file_name,
                original_name=original_name, path=rel, size=os.path.getsize(full),
                mime_type=uploaded.content_type, category=category, visibility=visibility, version=version)
    except Exception:
        try:
            os.remove(full)
        except OSError:
            pass
        raise
    ok(request, '资料文件「%s」已上传（v%d）。' % (original_name, version))
    return redirect('support.index')
