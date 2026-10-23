# -*- coding: utf-8 -*-
"""seed_beat：把 pm/celery.py 的调度表同步到 django_celery_beat 的 PeriodicTask。

背景
----
deploy/pm-celery-beat.service 用的是 `DatabaseScheduler`，此时 Celery **只认数据库
里的 PeriodicTask**，`pm/celery.py` 里的静态 `beat_schedule` 不会被读取。
本命令就是两者的桥：以 `pm.celery.BEAT_SCHEDULES` 为唯一可信源，把同名调度
统一写成 PeriodicTask，并清理已经不再登记的历史条目，避免双份调度互相打架。

用法
----
    python manage.py seed_beat            # 同步
    python manage.py seed_beat --dry-run  # 只看将要做什么
    python manage.py seed_beat --clear    # 只删除本命令曾写入的全部调度
"""
import json

from django.core.management.base import BaseCommand

from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask

from pm.celery import BEAT_SCHEDULES

# 由本命令写入的 PeriodicTask 统一加这个前缀，便于识别与清理
DESCRIPTION = 'synced-by-seed_beat (source: pm/celery.py BEAT_SCHEDULES)'


class Command(BaseCommand):
    help = '把 pm/celery.py 的 BEAT_SCHEDULES 同步为 django_celery_beat 的 PeriodicTask'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='只打印将要执行的改动')
        parser.add_argument('--clear', action='store_true', help='删除本命令写入的全部调度')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        owned = {name for name, _task, _cron, _kw in BEAT_SCHEDULES}
        legacy = PeriodicTask.objects.filter(description=DESCRIPTION)

        if options['clear']:
            count = legacy.count()
            if not dry_run:
                legacy.delete()
            self.stdout.write(self.style.SUCCESS('已删除 %s 条同步调度' % count))
            return

        created = updated = skipped = 0
        for name, task, cron, kwargs in BEAT_SCHEDULES:
            # get_or_create 的 kwargs 必须完整，否则会命中/混乱既有行
            defaults = dict.fromkeys(['minute', 'hour', 'day_of_week', 'day_of_month',
                                      'month_of_year'], '*')
            defaults.update(cron)
            schedule, _ = CrontabSchedule.objects.get_or_create(**defaults)
            payload = {
                'task': task, 'crontab': schedule, 'interval': None,
                'kwargs': json.dumps(kwargs, ensure_ascii=False),
                'args': '[]', 'enabled': True, 'description': DESCRIPTION,
            }
            existing = PeriodicTask.objects.filter(name=name).first()
            if existing is None:
                created += 1
                if not dry_run:
                    PeriodicTask.objects.create(name=name, **payload)
                self.stdout.write('  [新建] %s -> %s (%s)' % (name, task, self._fmt(cron)))
            else:
                same = (existing.task == task and existing.crontab_id == schedule.id
                        and existing.kwargs == payload['kwargs']
                        and existing.description == DESCRIPTION)
                if same:
                    skipped += 1
                    continue
                updated += 1
                if not dry_run:
                    for field, value in payload.items():
                        setattr(existing, field, value)
                    existing.save()
                self.stdout.write('  [更新] %s -> %s (%s)' % (name, task, self._fmt(cron)))

        # 清理：本命令写进去、但已不在 BEAT_SCHEDULES 里的残留项
        stale = legacy.exclude(name__in=owned)
        stale_names = list(stale.values_list('name', flat=True))
        for stale_name in stale_names:
            self.stdout.write('  [删除] %s（调度表中已移除）' % stale_name)
        if not dry_run:
            stale.delete()

        self.stdout.write(self.style.SUCCESS(
            'Beat 调度同步完成：新建 %s / 更新 %s / 无变化 %s / 清理 %s'
            % (created, updated, skipped, len(stale_names))))
        self.stdout.write(
            '共 %s 条调度，来源 pm/celery.py BEAT_SCHEDULES（唯一可信源）。'
            % len(BEAT_SCHEDULES))

    @staticmethod
    def _fmt(cron):
        params = dict.fromkeys(['minute', 'hour', 'day_of_month', 'month_of_year',
                                'day_of_week'], '*')
        params.update(cron)
        return '%s %s %s %s %s' % (params['minute'], params['hour'],
                                   params['day_of_month'], params['month_of_year'],
                                   params['day_of_week'])
