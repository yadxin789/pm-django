# -*- coding: utf-8 -*-
"""项目 / 任务状态机（`beforeTransition` / `afterTransition` 钩子）。

参考目录用 spatie/laravel-model-states，任务/项目状态流转时会自动触发钩子：

    Task\\Pending::afterTransition     给执行人建待办
    Task\\ForAudit::afterTransition    清执行人待办 / 给审核人建待办 / 通知审核人
    Task\\Rejected::afterTransition    给执行人建「已驳回」待办 / 通知执行人
    Task\\Completed::beforeTransition  重算项目进度，全部任务完成则项目自动完成
    Task\\Completed::afterTransition   写 task_completed 项目日志
    Task\\OverdueCompleted             同 OverdueCompleted 标签（无钩子）

本项目不依赖该扩展，这里用「目标状态 → 钩子」的等价表实现，并提供统一的
:func:`transition` 入口，保证服务层、管理命令、定时任务等所有流转路径都触发
同一份副作用；同时把操作人写进 status_transitions.user_id（对齐 Laravel 的
`auth()->id()`）。
"""
from __future__ import annotations

from django.utils import timezone

from ..models import Notification, ProjectLog, Todo

from .task_state_service import (  # noqa: F401  常量集中在这里，避免多处硬编码
    P_COMPLETED,
    P_INPROGRESS,
    P_NOT_STARTED,
    P_OVERDUE,
    P_OVERDUE_COMPLETED,
    P_SUSPENDED,
    T_COMPLETED,
    T_FORAUDIT,
    T_INPROGRESS,
    T_OVERDUE,
    T_OVERDUE_COMPLETED,
    T_PENDING,
    T_REJECTED,
)


# ---------------------------------------------------------------- 工具函数

def notify(user_id, title, content, related_type, related_id, sender_id, ntype='info'):
    """创建站内通知（对齐 Laravel SystemNotification / BaseNotification::buildData）。

    通知失败不能阻断主流程（对齐 Laravel Rejected 钩子里 try/catch 的行为）。
    """
    if not user_id:
        return
    try:
        Notification.objects.create(
            user_id=user_id, sender_id=sender_id,
            type=ntype, title=title, content=content,
            related_type=related_type, related_id=related_id,
        )
    except Exception:
        pass


def _uid(user):
    """把 user / user_id / None 统一转成 user_id。"""
    if user is None:
        return None
    return getattr(user, 'id', user)


def _project_name(task):
    try:
        return task.project.name if task.project_id else ''
    except Exception:
        return ''


def rejection_reason(task):
    """任务最近一次被驳回的原因（对齐 Task\\Rejected::getRejectionReason）。"""
    f = task.files.filter(status='rejected').order_by('-id').first()
    return f.audit_reason if f and f.audit_reason else None


def completed_state_for(task):
    """任务完成时应落到哪个状态（对齐 Laravel TaskService::completedStateFor）。

    已逾期（状态为已逾期 / is_overdue 标记 / 截止日期早于今天）→ 逾期完成，否则 → 已完成。
    """
    if task.status == T_OVERDUE or task.is_overdue:
        return T_OVERDUE_COMPLETED
    if task.end_date and timezone.localdate() > task.end_date:
        return T_OVERDUE_COMPLETED
    return T_COMPLETED


# ---------------------------------------------------------------- Task 钩子

def _task_pending_after(task, user_id, ctx):
    """Task\\Pending::afterTransition —— 给执行人创建待办。"""
    if task.executor_id:
        Todo.objects.create(
            user_id=task.executor_id, type='task',
            title='【%s】%s' % (_project_name(task), task.name),
            related_type='task', related_id=task.id,
        )


def notify_update_for_audit(task, user_id, version):
    """「文件更新」的审核待办 + 通知（对齐 Laravel TaskService::updateFile 尾段）。"""
    if not task.auditor_id:
        return
    Todo.objects.create(
        user_id=task.auditor_id, type='audit',
        title='文件更新审核：【%s】%s' % (_project_name(task), task.name),
        related_type='task', related_id=task.id,
    )
    notify(
        task.auditor_id, '您有文件更新需要审核',
        '任务「%s」已更新文件（v%s），请审核' % (task.name, version),
        'task', task.id, user_id, 'audit',
    )


def _task_foraudit_after(task, user_id, ctx):
    """Task\\ForAudit::afterTransition —— 清执行人待办，给审核人建待办并通知。

    ctx 支持 is_update / version：由「更新文件」触发时，待办标题与通知文案走
    TaskService::updateFile 的变体。参考目录在该路径下会额外再建一条待办，
    这里收敛为一条，避免重复待办 / 重复通知。
    """
    is_update = bool(ctx.get('is_update'))
    version = ctx.get('version')
    if task.executor_id:
        Todo.objects.filter(
            related_type='task', related_id=task.id,
            user_id=task.executor_id, type='task',
        ).delete()
    if not task.auditor_id:
        return
    if is_update:
        notify_update_for_audit(task, user_id, version)
        return
    Todo.objects.create(
        user_id=task.auditor_id, type='audit',
        title='审核：【%s】%s' % (_project_name(task), task.name),
        related_type='task', related_id=task.id,
    )
    notify(
        task.auditor_id, '您有新文件需要审核',
        '任务「%s」已提交文件，请审核' % task.name,
        'task', task.id, user_id, 'audit',
    )


def _task_rejected_after(task, user_id, ctx):
    """Task\\Rejected::afterTransition —— 给执行人建「已驳回」待办并通知。

    仅「首次上传被驳回」会走到 Rejected；「更新文件被驳回」在 TaskService::reject
    里是回到已完成状态，由调用方自行发通知（参考目录同此路径）。
    """
    if not task.executor_id:
        return
    reason = rejection_reason(task)
    Todo.objects.create(
        user_id=task.executor_id, type='task',
        title='【%s】%s（已驳回）' % (_project_name(task), task.name),
        related_type='task', related_id=task.id,
    )
    notify(
        task.executor_id, '您上传的文件已驳回',
        '任务「%s」的文件已被驳回%s' % (task.name, ('：' + reason) if reason else ''),
        'task', task.id, user_id or task.auditor_id, 'audit_result',
    )


def _task_completed_before(task, user_id, ctx):
    """Task\\Completed::beforeTransition —— 重算进度，全完成时自动完成项目。

    注意：Laravel 统计「已完成」时只看 Completed，不含 OverdueCompleted，这里保持一致。
    """
    project = task.project if task.project_id else None
    if project is None:
        return
    try:
        project.calculate_progress()
    except Exception:
        pass
    try:
        from django.db.models import Q
        total = project.tasks.count()
        # 钩子在落库前触发，当前任务此刻还是 ForAudit；与参考目录
        # Completed::beforeTransition 的「全部任务完成即完成项目」语义对齐，
        # 统计时把当前任务视作已完成计入分子。
        done = project.tasks.filter(Q(status=T_COMPLETED) | Q(id=task.id)).count()
        if total > 0 and done == total and project.status != P_COMPLETED:
            project_transition(project, P_COMPLETED, user_id)
    except Exception:
        pass


def _task_completed_after(task, user_id, ctx):
    """Task\\Completed::afterTransition —— 写 task_completed 项目日志。"""
    if not task.project_id:
        return
    try:
        ProjectLog.objects.create(
            project_id=task.project_id, user_id=user_id,
            action='task_completed', content='任务「%s」已完成' % task.name,
        )
    except Exception:
        pass


TASK_HOOKS = {
    T_PENDING: {'after': _task_pending_after},
    T_FORAUDIT: {'after': _task_foraudit_after},
    T_REJECTED: {'after': _task_rejected_after},
    T_COMPLETED: {'before': _task_completed_before, 'after': _task_completed_after},
}


# ---------------------------------------------------------------- Project 钩子

PROJECT_HOOKS = {}


# ---------------------------------------------------------------- 统一入口

def _apply_hook(hook, obj, user_id, ctx):
    if hook:
        hook(obj, user_id, ctx)


def task_transition(task, target, user=None, **ctx):
    """任务状态流转（对齐 Laravel `$task->status->transitionTo(...)`）。

    源状态等于目标状态时是 no-op（Laravel 侧会抛异常，因此各处都先 equals 判重，
    这里直接吸收）。返回是否真的发生了流转。

    ctx 会透传给钩子，目前 ForAudit 支持：
      is_update / version —— 「更新文件」路径的标题与文案变体。
    """
    if task.status == target:
        return False
    hooks = TASK_HOOKS.get(target, {})
    user_id = _uid(user)
    _apply_hook(hooks.get('before'), task, user_id, ctx)
    task._transition_user = user_id
    task.status = target
    task.save()
    _apply_hook(hooks.get('after'), task, user_id, ctx)
    return True


def project_transition(project, target, user=None, **ctx):
    """项目状态流转（对齐 Laravel `$project->status->transitionTo(...)`）。"""
    if project.status == target:
        return False
    hooks = PROJECT_HOOKS.get(target, {})
    user_id = _uid(user)
    _apply_hook(hooks.get('before'), project, user_id, ctx)
    project._transition_user = user_id
    project.status = target
    project.save()
    _apply_hook(hooks.get('after'), project, user_id, ctx)
    return True


def transition(obj, target, user=None, **ctx):
    """按对象类型分派；Task → task_transition，Project → project_transition。"""
    from ..models import Project
    if isinstance(obj, Project):
        return project_transition(obj, target, user, **ctx)
    return task_transition(obj, target, user, **ctx)


def transition_to_completed_or_overdue_completed(task, user=None):
    """完成任务：先判归档归属再落完成时间（对齐 Laravel transitionToCompletedOrOverdueCompleted）。"""
    target = completed_state_for(task)
    task.completed_at = task.completed_at or timezone.now()
    task.save()
    task_transition(task, target, user)
