# -*- coding: utf-8 -*-
"""任务服务层：对齐 Laravel TaskService.php 的上传/审核/驳回/更新文件业务逻辑。

文件一致性约定（对齐参考目录）：
  - 「落盘 + 写记录」整体包在 transaction.atomic() 里，DB 失败时立即删除刚落盘的文件；
  - 更新文件时为旧版本**真实复制**一份独立快照到 history/，FileVersion.path 指向快照，
    避免旧文件被删除后历史版本变成「记录还在、文件已丢」的死记录；
  - 事务成功后再调用 prune_file_history 裁剪历史版本，防止反复更新撑爆硬盘；
  - 任何删除入口都不直接物理删文件：一律软删（回收站），物理清理交给 TrashService。
"""
import os
import shutil
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import Task, File, FileVersion, Todo, Notification, ProjectLog
from .state_machine import notify as _notify
from .state_machine import (
    completed_state_for, notify_update_for_audit, task_transition,
    transition_to_completed_or_overdue_completed,
)


ALLOWED_EXTENSIONS = {
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
    'jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp',
    'txt', 'csv', 'zip', 'rar', '7z',
}

STATE_PENDING = 'App\\States\\Task\\Pending'
STATE_FOR_AUDIT = 'App\\States\\Task\\ForAudit'
STATE_COMPLETED = 'App\\States\\Task\\Completed'
STATE_OVERDUE = 'App\\States\\Task\\Overdue'
STATE_OVERDUE_COMPLETED = 'App\\States\\Task\\OverdueCompleted'
STATE_REJECTED = 'App\\States\\Task\\Rejected'


def _remove_media_file(rel_path):
    """删除 MEDIA_ROOT 下的相对路径文件（仅用于「落盘成功但写库失败」的即时回滚）。"""
    if not rel_path:
        return
    try:
        full = os.path.join(settings.MEDIA_ROOT, rel_path)
        if os.path.isfile(full):
            os.remove(full)
    except OSError:
        pass


def _create_file_snapshot(file):
    """为文件生成独立的历史版本快照，返回快照的相对路径（失败返回 None）。

    对齐 Laravel TaskService::createFileSnapshot：
      - 快照真实复制到任务文件目录下的 history/ 子目录，
        命名 project-{pid}/task-{tid}/history/{fileId}_v{version}.{ext}；
      - 源文件不存在时返回 None，调用方据此**不写版本记录**，避免产生死记录。
    """
    if not file or not file.path:
        return None
    src = os.path.join(settings.MEDIA_ROOT, file.path)
    if not os.path.isfile(src):
        return None
    ext = os.path.splitext(file.path)[1]
    snap_rel = os.path.join(
        os.path.dirname(file.path), 'history',
        '%s_v%s%s' % (file.pk, file.version, ext),
    ).replace('\\', '/')
    dst = os.path.join(settings.MEDIA_ROOT, snap_rel)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    except OSError:
        return None
    return snap_rel


def _store_file_to_repository(uploaded, project_id, task_id):
    """把上传文件保存到 MEDIA_ROOT/file_repository/project-{id}/task-{id}/。返回 (rel_path, name)。"""
    ext = (uploaded.name.rsplit('.', 1)[-1].lower() if '.' in uploaded.name else '')
    if ext not in ALLOWED_EXTENSIONS:
        raise RuntimeError('不支持的文件类型：.' + ext)
    filename = uuid.uuid4().hex + '_v1.' + ext
    rel_dir = os.path.join('file_repository', 'project-%s' % project_id, 'task-%s' % task_id)
    abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    rel_path = os.path.join(rel_dir, filename)
    abs_path = os.path.join(abs_dir, filename)
    with open(abs_path, 'wb') as f:
        for chunk in uploaded.chunks():
            f.write(chunk)
    return rel_path, filename


def _store_new_file_to_repository(uploaded, project_id, task_id, version):
    ext = (uploaded.name.rsplit('.', 1)[-1].lower() if '.' in uploaded.name else '')
    if ext not in ALLOWED_EXTENSIONS:
        raise RuntimeError('不支持的文件类型：.' + ext)
    filename = uuid.uuid4().hex + '_v%d.' % version + ext
    rel_dir = os.path.join('file_repository', 'project-%s' % project_id, 'task-%s' % task_id)
    abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    rel_path = os.path.join(rel_dir, filename)
    abs_path = os.path.join(abs_dir, filename)
    with open(abs_path, 'wb') as f:
        for chunk in uploaded.chunks():
            f.write(chunk)
    return rel_path, filename


def _transition(task, to_state, user=None, **ctx):
    """保留的历史别名，统一走状态机（钩子在此触发）。"""
    task_transition(task, to_state, user, **ctx)


def store_file(task, uploaded, user_id):
    """首次/覆盖上传：创建 File(pending)，任务转 ForAudit。

    落盘与写记录包在同一事务里（对齐 Laravel TaskService::storeFile）；
    写库失败时立刻删掉刚落盘的文件，不留孤儿。
    """
    rel_path = None
    try:
        with transaction.atomic():
            rel_path, name = _store_file_to_repository(uploaded, task.project_id, task.id)
            task_file = File.objects.create(
                task=task, uploader_id=user_id, auditor_id=task.auditor_id,
                name=name, original_name=uploaded.name, path=rel_path,
                size=uploaded.size, mime_type=getattr(uploaded, 'content_type', '') or '',
                version=1, status='pending',
            )
            # 转待审核：ForAudit 钩子负责清理执行人待办、给审核人建待办并通知
            task_transition(task, STATE_FOR_AUDIT, user_id)
        return task_file
    except Exception:
        _remove_media_file(rel_path)
        raise


def approve(task, reason, user_id):
    """审核通过。

    更新场景下旧版本走**软删除**（保留物理文件，可回收站恢复），
    绝不在这里物理删文件 —— 否则 FileVersion 记录会指向已消失的文件。
    """
    with transaction.atomic():
        pending = task.files.filter(status='pending').first()
        if not pending:
            transition_to_completed_or_overdue_completed(task, user_id)
            return
        is_update = pending.version > 1 or task.files.filter(status='approved').exclude(id=pending.id).exists()
        if is_update:
            old_file = task.files.filter(status='approved').first()
            if old_file and old_file.id != pending.id:
                old_file.delete()
        pending.status = 'approved'
        pending.auditor_id = user_id
        pending.audit_time = timezone.now()
        pending.audit_reason = reason
        pending.save()
        transition_to_completed_or_overdue_completed(task, user_id)
        if is_update:
            _notify_project_members(task, pending, user_id)


def reject(task, reason, user_id):
    """审核驳回。

    - 更新文件被驳回：**软删除**待审核文件（保留物理文件供回收站恢复）；
    - 首次上传被驳回：保留记录与物理文件，仅标记为 rejected。
    两种情况都不物理删文件（对齐 Laravel TaskService::reject）。
    """
    with transaction.atomic():
        pending = task.files.filter(status='pending').first()
        if not pending:
            return
        is_update = pending.version > 1 or task.files.filter(status='approved').exclude(id=pending.id).exists()
        if is_update:
            uploader_id = pending.uploader_id
            pending.delete()
            Todo.objects.filter(related_type='task', related_id=task.id,
                                user_id=task.auditor_id, type='audit').delete()
            # 回到原来的完成归属（已完成 / 逾期完成），保证「更新」按钮仍然可用
            task_transition(task, completed_state_for(task), user_id)
            if uploader_id:
                _notify(
                    uploader_id,
                    '您的更新文件已驳回',
                    '任务「%s」的更新文件已被驳回%s' % (task.name, ('：' + reason) if reason else ''),
                    'task', task.id, user_id, 'audit_result',
                )
        else:
            pending.status = 'rejected'
            pending.auditor_id = user_id
            pending.audit_time = timezone.now()
            pending.audit_reason = reason
            pending.save()
            # Rejected 钩子负责给执行人建「已驳回」待办并通知
            _transition(task, STATE_REJECTED, user_id)


def update_file(task, uploaded, user_id):
    """更新已通过的文件：旧版本生成独立快照进 FileVersion，新版本 File(pending)，任务转 ForAudit。"""
    current = task.files.filter(status='approved').first()
    if not current:
        raise RuntimeError('没有可更新的文件')
    rel_path = None
    try:
        with transaction.atomic():
            # 1. 旧版本真实复制一份独立快照，再写版本记录；
            #    快照失败则不写记录，避免留下指向不存在文件的死记录。
            snapshot_rel = _create_file_snapshot(current)
            if snapshot_rel:
                FileVersion.objects.create(
                    file=current, task=task, version=current.version, path=snapshot_rel,
                    uploader_id=current.uploader_id, auditor_id=current.auditor_id,
                    status='approved',
                )
            # 2. 落新文件 + 写新记录
            new_version = current.version + 1
            rel_path, name = _store_new_file_to_repository(uploaded, task.project_id, task.id, new_version)
            new_file = File.objects.create(
                task=task, uploader_id=user_id, auditor_id=task.auditor_id,
                name=name, original_name=uploaded.name, path=rel_path,
                size=uploaded.size, mime_type=getattr(uploaded, 'content_type', '') or '',
                version=new_version, status='pending',
            )
            # 转待审核：ForAudit 钩子负责清理执行人待办、给审核人建待办并通知
            # （is_update/version 让文案走「文件更新」变体，且只产生一条待办/通知）
            changed = task_transition(task, STATE_FOR_AUDIT, user_id,
                                      is_update=True, version=new_version)
            if not changed:
                # 任务本就处于待审核（连续多次更新）：状态未变、钩子不触发，
                # 这里补发一次更新待办/通知，对齐 Laravel 无条件发通知的行为。
                notify_update_for_audit(task, user_id, new_version)
    except Exception:
        _remove_media_file(rel_path)
        raise
    # 3. 事务成功后再裁剪历史快照：超出保留上限的旧版本（快照文件 + 版本记录）
    #    一并清理，避免任务被反复更新导致磁盘无限膨胀（对齐 Laravel）。
    _prune_history(task)
    return new_file


def _prune_history(task):
    """裁剪任务历史版本快照；失败不影响主流程。"""
    try:
        from core.services.trash_service import prune_file_history
        prune_file_history(task)
    except Exception:
        pass


def refresh_project_progress(task):
    try:
        task.project.calculate_progress()
    except Exception:
        pass


def _notify_project_members(task, file, sender_id):
    try:
        project = task.project
        members = project.participants()
        for user in members:
            if user.id == sender_id:
                continue
            _notify(
                user.id,
                '项目文件已更新',
                '【%s】任务「%s」的文件已更新至 v%s，请及时查看' % (project.name, task.name, file.version),
                'file', file.id, sender_id, 'info',
            )
    except Exception:
        pass
