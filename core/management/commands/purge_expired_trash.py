# -*- coding: utf-8 -*-
"""回收站到期自动清理（对齐 Laravel trash:purge-expired）。

保留期限由 settings.PM_TRASH_RETENTION_DAYS 控制（默认 15 天）。
处理范围 = 全系统所有纳入回收站的分类，规则统一。

容器型内容（项目 / 项目模板 / 知识库）在硬删父记录前，
会先逐条彻底删除其子内容（含物理文件与历史快照），
避免数据库外键 CASCADE 硬删记录时留下无人引用的孤儿文件。
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.services.operation_log import log_operation
from core.services.trash_service import (
    TYPES,
    force_delete,
    force_delete_deep,
)


class Command(BaseCommand):
    help = '彻底删除回收站中超过保留期限的内容（含物理文件与历史快照）'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=0,
                            help='覆盖配置中的保留天数')
        parser.add_argument('--dry-run', action='store_true',
                            help='只列出将被清理的内容，不实际删除')

    def handle(self, *args, **opts):
        cfg_days = int(getattr(settings, 'PM_TRASH_RETENTION_DAYS', 15))
        days = int(opts.get('days') or cfg_days)
        dry_run = bool(opts.get('dry_run'))

        if days < 1:
            self.stderr.write(self.style.ERROR('保留天数必须大于 0，已中止。'))
            return

        cutoff = timezone.now() - timedelta(days=days)
        prefix = '[预演] ' if dry_run else ''

        self.stdout.write(self.style.NOTICE(
            f'{prefix}保留期限：{days} 天（将清理 {cutoff:%Y-%m-%d %H:%M:%S} 之前删除的内容）'
        ))

        summary: list[str] = []
        purged_total = 0

        # 1. 语音会议里上传的临时演示文件（有效期 7 天）不属于回收站分类，
        #    但同样需要按期清理，否则 voice_demo 目录会持续膨胀。
        purged_total += self._purge_expired_demo_files(dry_run, prefix, summary)

        # 2. 全系统所有纳入回收站的分类
        for type_key, cfg in TYPES.items():
            before = purged_total
            self._purge_type(type_key, cfg, cutoff, dry_run)
            count = purged_total - before
            if count:
                summary.append(f'{cfg["label"]} {count} 条')
                self.stdout.write(f'  {prefix}{cfg["label"]}：{count} 条')

        if purged_total == 0:
            self.stdout.write(self.style.NOTICE(f'{prefix}没有超过保留期限的内容，无需清理。'))
            return

        self.stdout.write(self.style.SUCCESS(f'{prefix}共清理 {purged_total} 条。'))

        if not dry_run:
            log_operation('回收站', '自动清理',
                          f'系统自动彻底删除超过 {days} 天的回收站内容，共 {purged_total} 条：'
                          f'{"、".join(summary)}')

    def _purge_type(self, type_key: str, cfg: dict, cutoff, dry_run: bool):
        model_cls = cfg['model']
        qs = model_cls.all_objects.filter(deleted_at__lt=cutoff).order_by('pk')
        for item in qs.iterator(chunk_size=100):
            self._purge_one(type_key, item, dry_run)

    def _purge_one(self, type_key: str, item, dry_run: bool) -> int:
        try:
            if not dry_run:
                force_delete_deep(item)
            return 1
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(f'  ✗ {type_key}#{item.pk} 清理失败：{exc}'))
            return 0

    def _purge_expired_demo_files(self, dry_run: bool, prefix: str, summary: list[str]) -> int:
        from core.models import CollabDocument
        expired = (CollabDocument.all_objects
                   .filter(is_temp_demo=True, expires_at__lt=timezone.now())
                   .order_by('pk'))
        count = 0
        for doc in expired:
            try:
                if not dry_run:
                    force_delete(doc)
                count += 1
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(self.style.ERROR(
                    f'  ✗ 临时演示文件#{doc.pk} 清理失败：{exc}'))
        if count:
            self.stdout.write(f'  {prefix}临时演示文件：{count} 条')
            summary.append(f'临时演示文件 {count} 条')
        return count