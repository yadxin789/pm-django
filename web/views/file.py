# -*- coding: utf-8 -*-
"""文件模块视图：对齐 Laravel FileController.php 的预览/下载/资源文件逻辑。"""
import os
import uuid

from django.conf import settings
from django.contrib import messages as dj_messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import FileResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.models import File, DepartmentFile

from .feedback import deny, ok

_MIME = {
    'pdf': 'application/pdf',
    'doc': 'application/msword',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xls': 'application/vnd.ms-excel',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif',
    'bmp': 'image/bmp', 'svg': 'image/svg+xml', 'webp': 'image/webp',
}


def _abs_path(rel_path):
    return os.path.join(settings.MEDIA_ROOT, rel_path)


@login_required
def index(request):
    from core.services import wecom_service
    files = Paginator(File.objects.select_related('task', 'task__project', 'uploader').order_by('-id'), 10)
    return render(request, 'file/index.html',
                  {'files': files.get_page(request.GET.get('page', 1)),
                   'wecom_enabled': wecom_service.is_enabled()})


def _apply_visibility_scope(qs, user):
    from django.db.models import Q
    return qs.filter(Q(visibility='all')
                     | (Q(visibility='department') & Q(department_id=user.department_id))
                     | (Q(visibility='private') & Q(uploader_id=user.id)))


@login_required
def resource_files(request):
    user = request.user
    categories = DepartmentFile.category_options()
    category_files = {}
    for key in categories:
        qs = DepartmentFile.objects.filter(category=key).select_related('department', 'uploader')
        qs = _apply_visibility_scope(qs, user)
        category_files[key] = {'name': categories[key], 'files': qs.order_by('-created_at')}
    return render(request, 'file/resource-files.html', {'category_files': category_files})


@login_required
def manage_resource_files(request):
    categories = DepartmentFile.category_options()
    category_files = {}
    for key in categories:
        category_files[key] = {
            'name': categories[key],
            'files': DepartmentFile.objects.filter(category=key)
                     .select_related('department', 'uploader').order_by('-created_at'),
        }
    return render(request, 'file/manage-resource-files.html', {'category_files': category_files})


@login_required
def preview(request, pk):
    file = get_object_or_404(File, pk=pk)
    if not file.can_preview(request.user.id):
        return HttpResponseForbidden('您没有权限查看此文件')
    if not os.path.exists(_abs_path(file.path)):
        return HttpResponseForbidden('文件不存在')
    from core.services import wecom_service
    ext = (file.original_name.rsplit('.', 1)[-1].lower() if '.' in file.original_name else '')
    return render(request, 'file/preview.html', {
        'file': file,
        'abs_path': _abs_path(file.path),
        'ext': ext,
        'wecom_enabled': wecom_service.is_enabled(),
        'wecom_type': 'image' if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp') else 'file',
    })


@login_required
def preview_file_content(request, pk):
    file = get_object_or_404(File, pk=pk)
    if not file.can_preview(request.user.id):
        return HttpResponseForbidden('您没有权限访问此文件')
    path = _abs_path(file.path)
    if not os.path.exists(path):
        return HttpResponseForbidden('文件不存在')
    ext = (file.original_name.rsplit('.', 1)[-1].lower() if '.' in file.original_name else '')
    mime = _MIME.get(ext, 'application/octet-stream')
    resp = FileResponse(open(path, 'rb'), content_type=mime)
    resp['Content-Disposition'] = 'inline; filename="%s"' % file.original_name
    return resp


@login_required
def download(request, pk):
    file = get_object_or_404(File, pk=pk)
    if not file.can_download(request.user.id):
        return HttpResponseForbidden('您没有权限下载此文件')
    path = _abs_path(file.path)
    if not os.path.exists(path):
        return HttpResponseForbidden('文件不存在')
    resp = FileResponse(open(path, 'rb'), as_attachment=True, filename=file.original_name)
    return resp


@login_required
def upload_department_file(request):
    categories = DepartmentFile.category_options()
    visibility_options = DepartmentFile.visibility_options()
    return render(request, 'file/upload-department.html',
                  {'categories': categories, 'visibility_options': visibility_options})


@login_required
def store_department_file(request):
    uploaded = request.FILES.get('file')
    category = request.POST.get('category')
    visibility = request.POST.get('visibility')
    valid_categories = set(DepartmentFile.category_options())
    valid_vis = set(DepartmentFile.visibility_options())
    if not uploaded:
        return deny(request, '请先选择要上传的文件。', fallback_view='file.upload-department')
    if category not in valid_categories:
        return deny(request, '请选择有效的资料分类。', fallback_view='file.upload-department')
    if visibility not in valid_vis:
        return deny(request, '请选择有效的可见范围。', fallback_view='file.upload-department')
    ext = (uploaded.name.rsplit('.', 1)[-1].lower() if '.' in uploaded.name else '')
    filename = uuid.uuid4().hex + '_v1.' + ext
    rel_dir = os.path.join('file_repository', 'department-%s' % (request.user.department_id or 0))
    abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    rel_path = os.path.join(rel_dir, filename).replace('\\', '/')
    with open(os.path.join(abs_dir, filename), 'wb') as f:
        for chunk in uploaded.chunks():
            f.write(chunk)
    try:
        with transaction.atomic():
            DepartmentFile.objects.create(
                department_id=request.user.department_id or 0, uploader_id=request.user.id,
                name=filename, original_name=uploaded.name, path=rel_path,
                size=uploaded.size, mime_type=getattr(uploaded, 'content_type', '') or '',
                category=category, visibility=visibility, version=1,
            )
    except Exception:
        # 落盘成功但写库失败：立即清掉刚落盘的文件，不留孤儿
        try:
            os.remove(os.path.join(settings.MEDIA_ROOT, rel_path))
        except OSError:
            pass
        raise
    ok(request, '资料文件「%s」已上传。' % uploaded.name)
    return redirect('file.resource-files')


@login_required
def preview_department_file(request, pk):
    file = get_object_or_404(DepartmentFile, pk=pk)
    if not file.can_access(request.user.id):
        return JsonResponse({'error': '您没有权限查看此文件'}, status=403)
    path = _abs_path(file.path)
    if not os.path.exists(path):
        return JsonResponse({'error': '文件不存在'}, status=404)
    ext = (file.original_name.rsplit('.', 1)[-1].lower() if '.' in file.original_name else '')
    if ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'svg', 'webp']:
        resp = FileResponse(open(path, 'rb'))
        return resp
    return render(request, 'file/preview.html', {'file': file})


@login_required
def download_department_file(request, pk):
    file = get_object_or_404(DepartmentFile, pk=pk)
    if not file.can_access(request.user.id):
        return HttpResponseForbidden('您没有权限下载此文件')
    path = _abs_path(file.path)
    if not os.path.exists(path):
        return HttpResponseForbidden('文件不存在')
    return FileResponse(open(path, 'rb'), as_attachment=True, filename=file.original_name)


@login_required
def update_department_file(request, pk):
    """更新部门资料文件。

    顺序：先把新文件落盘（失败会抛异常，旧文件与旧记录保持原样）→
    再更新记录指向新文件 → 最后删除旧文件。
    若先删旧文件而后序失败，会出现「记录还在、文件已丢」的不可恢复损坏。
    """
    file = get_object_or_404(DepartmentFile, pk=pk)
    uploaded = request.FILES.get('file')
    if not uploaded:
        return deny(request, '请先选择新版本的文件。', fallback_view='file.manage-resource-files')

    ext = (uploaded.name.rsplit('.', 1)[-1].lower() if '.' in uploaded.name else '')
    new_version = file.version + 1
    filename = uuid.uuid4().hex + '_v%d.' % new_version + ext
    rel_dir = os.path.join('file_repository', 'department-%s' % (file.department_id or 0))
    abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    abs_path = os.path.join(abs_dir, filename)
    rel_path = os.path.join(rel_dir, filename)

    # 1. 先落盘
    with open(abs_path, 'wb') as f:
        for chunk in uploaded.chunks():
            f.write(chunk)

    # 2. 记录指向新文件
    old_path = file.path
    file.name = filename
    file.original_name = uploaded.name
    file.path = rel_path
    file.size = uploaded.size
    file.mime_type = getattr(uploaded, 'content_type', '') or ''
    file.version = new_version
    file.save()

    # 3. 落盘成功且记录更新成功后，删除旧文件
    if old_path and old_path != rel_path:
        old_abs = _abs_path(old_path)
        if os.path.exists(old_abs):
            try:
                os.remove(old_abs)
            except OSError:
                pass

    ok(request, '资料文件「%s」已更新到 v%d。' % (file.original_name, file.version))
    return redirect('file.manage-resource-files')


@login_required
def delete_department_file(request, pk):
    """软删除部门资料文件（进入回收站，由 TrashService 统一处理）。

    仅管理员或上传人可删除；物理文件保留，等待管理员在回收站恢复或彻底删除。
    """
    file = get_object_or_404(DepartmentFile, pk=pk)
    if not (request.user.is_admin() or file.uploader_id == request.user.id):
        return deny(request, '您没有权限删除该资料文件（仅管理员或上传人可删除）',
                    fallback_view='file.manage-resource-files')

    from core.services.trash_service import soft_delete
    soft_delete(file, user_id=request.user.id)
    dj_messages.success(request, '资料文件已移入回收站')
    return redirect('file.manage-resource-files')
