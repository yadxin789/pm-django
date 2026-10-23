# -*- coding: utf-8 -*-
"""Celery 应用实例 + 定时任务注册（对齐参考目录 routes/console.php）。

本项目使用 Celery + Celery Beat 调度，所有调度都集中在此文件。
服务器需运行（注意 App 名是 `pm`，不是参考实现的 `pause`）：
    celery -A pm worker -l info
    celery -A pm beat   -l info

线上 beat 由 deploy/pm-celery-beat.service 以 DatabaseScheduler 启动，
只读 django_celery_beat 库里的 PeriodicTask；本文件的 BEAT_SCHEDULES 通过
`manage.py seed_beat` 同步过去，两者共用同一份描述，改完务必重跑该命令。
"""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pm.settings')

app = Celery('pm')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()


# ===================== 定时任务（对齐 Laravel routes/console.php）=====================
#
# 单一可信源：本表既用于生成 app.conf.beat_schedule（默认调度器），
# 也被 `manage.py seed_beat` 同步成 django_celery_beat 的 PeriodicTask
# （deploy/pm-celery-beat.service 用的是 DatabaseScheduler）。
# 两边共用同一份描述，避免出现「改了一边另一边不生效」或「同名不同 Cron」的双份调度。
#
# 格式：(名称, 任务名, crontab 关键字参数, 任务 kwargs)
BEAT_SCHEDULES = [
    # 每天 00:00 刷新项目/任务状态与进度
    ('projects-refresh-status-daily',
     'core.management.commands.refresh_project_status',
     {'hour': '0', 'minute': '0'}, {}),
    # 每天 01:00 项目逾期检查 + 通知
    ('reminder-overdue-projects-daily',
     'core.services.reminder_service.check_overdue_projects',
     {'hour': '1', 'minute': '0'}, {}),
    # 每天 03:30 回收站到期清理（含物理文件、历史快照、语音临时演示文件）
    ('trash-purge-expired-daily',
     'core.management.commands.purge_expired_trash',
     {'hour': '3', 'minute': '30'}, {}),
    # 每天 04:00 物理文件巡检（孤儿文件 + 死记录兜底）
    ('files-gc-daily',
     'core.management.commands.audit_files',
     {'hour': '4', 'minute': '0'}, {'fix': True, 'purge_dead': True}),
    # 每天 08:00 未来 3 天内到期任务提醒
    ('reminder-upcoming-deadlines-daily',
     'core.services.reminder_service.check_upcoming_deadlines',
     {'hour': '8', 'minute': '0'}, {}),
    # 每天 10:00 / 15:00 审核超时提醒
    ('reminder-audit-overdue-twicedaily',
     'core.services.reminder_service.check_audit_overdue',
     {'hour': '10,15', 'minute': '0'}, {}),
    # 每 30 分钟任务逾期检查
    ('reminder-overdue-tasks-30min',
     'core.services.reminder_service.check_overdue_tasks',
     {'minute': '*/30'}, {}),
]

app.conf.beat_schedule = {
    name: {'task': task, 'schedule': crontab(**cron), 'kwargs': kwargs}
    for name, task, cron, kwargs in BEAT_SCHEDULES
}


@app.task(name='core.management.commands.refresh_project_status')
def refresh_project_status():
    from django.core.management import call_command
    call_command('refresh_project_status')


@app.task(name='core.management.commands.purge_expired_trash')
def purge_expired_trash():
    from django.core.management import call_command
    call_command('purge_expired_trash')


@app.task(name='core.management.commands.audit_files')
def audit_files(fix: bool = False, purge_dead: bool = False):
    from django.core.management import call_command
    call_command('audit_files', fix=fix, purge_dead=purge_dead)


@app.task(name='core.services.reminder_service.check_upcoming_deadlines')
def reminder_check_upcoming_deadlines():
    from core.services.reminder_service import ReminderService
    ReminderService().check_upcoming_deadlines()


@app.task(name='core.services.reminder_service.check_overdue_tasks')
def reminder_check_overdue_tasks():
    from core.services.reminder_service import ReminderService
    ReminderService().check_overdue_tasks()


@app.task(name='core.services.reminder_service.check_audit_overdue')
def reminder_check_audit_overdue():
    from core.services.reminder_service import ReminderService
    ReminderService().check_audit_overdue()


@app.task(name='core.services.reminder_service.check_overdue_projects')
def reminder_check_overdue_projects():
    from core.services.reminder_service import ReminderService
    ReminderService().check_overdue_projects()
