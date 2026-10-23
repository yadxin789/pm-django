# -*- coding: utf-8 -*-
"""物理文件巡检（兜底保障，对齐 Laravel files:gc 命令）。

目标：任何情况下都不遗留垃圾文件、不出现「记录与文件不匹配」。

前端的各个删除路径已在代码层面保证清理，但只要有新的删除入口被写出来
或某次删除中途失败，就可能产生孤儿文件。本命令充当最后一道防线，
每天定时做一次双向核对：

  1. 磁盘有文件、数据库无引用  → 孤儿文件（可自动清理）
  2. 数据库有引用、磁盘无文件  → 死记录（默认只报告，需 --purge-dead 才删）
  3. 空目录一律清理，避免目录树无限膨胀

安全阀（避免误删）：
  - 只扫描本系统管理的目录前缀，绝不触碰模板、备份等其它目录；
  - 孤儿文件有宽限期（默认 60 分钟），避免删掉「刚落盘、DB 记录还没写」的上传中文件；
  - 死记录占比异常时会中止清理并告警（很可能磁盘挂载掉了，此时删记录会造成灾难）。
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from fnmatch import fnmatch

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from core.services.operation_log import log_operation
from core.services.trash_service import (
    TYPES,
    DISK_ROOT,
    force_delete,
    _disk_path,
)

logger = logging.getLogger(__name__)


# 本系统管理的目录清单（逻辑分组 => 规则）。
# 物理根统一为 MEDIA_ROOT（见 trash_service.DISK_ROOT 的说明），
# 因此下面的 paths 都是「相对 MEDIA_ROOT」的目录，必须与各写入方的落盘位置严格一致：
#   collab/               协作文档正文 + history/ 历史快照
#   file_repository/      任务文件（project-*/task-*/）、部门资料文件（department-*/）
#   avatars/              用户头像
#   chat_files/           聊天附件
#   wecom-inbox/          企业微信收件箱
#   voice_demo/           电话会议历史遗留目录
# 刻意不把整个 media/ 纳入扫描范围：collab_templates（运行时生成的 OnlyOffice 模板）、
# 静态资源、备份等非受管内容绝不能被当成孤儿文件删除。
MANAGED = {
    'file_repository': {
        'paths': ['collab', 'file_repository'],
        'patterns': [],
    },
    'public': {
        'paths': ['avatars', 'chat_files', 'messages'],
        'patterns': [],
    },
    'wecom_files': {
        'paths': ['wecom-inbox'],
        'patterns': [],
    },
    # 历史遗留目录：当前已无任何代码往这里写文件
    'local': {
        'paths': ['voice_demo'],
        'patterns': [],
    },
}


class Command(BaseCommand):
    help = '物理文件巡检：清理孤儿文件与空目录，核对记录与文件是否一一对应'

    def add_arguments(self, parser):
        parser.add_argument('--fix', action='store_true',
                            help='实际清理孤儿文件与空目录（不加则只检查并报告）')
        parser.add_argument('--purge-dead', action='store_true',
                            help='同时删除「库中有记录、磁盘无文件」的死记录')
        parser.add_argument('--grace', type=int, default=60,
                            help='孤儿文件宽限期（分钟），只处理修改时间早于该时长的文件')

    def handle(self, *args, **opts):
        grace_minutes = max(0, int(opts.get('grace') or 60))
        fix = bool(opts.get('fix'))
        purge_dead = bool(opts.get('purge_dead'))

        mode = '[清理模式] ' if fix else '[检查模式] '
        self.stdout.write(self.style.NOTICE(f'{mode}开始巡检物理文件…'))

        references = self._build_references()
        collab_ids = self._collab_doc_id_set()

        orphans, missing = self._scan(references, collab_ids)
        orphan_count = sum(len(v) for v in orphans.values())
        missing_count = sum(len(v) for v in missing.values())

        self._report('孤儿文件（磁盘有、库中无引用）', orphans, orphan_count)
        self._report('死记录（库中有引用、磁盘无文件）', missing, missing_count)

        if orphan_count == 0 and missing_count == 0:
            self.stdout.write(self.style.SUCCESS('✓ 记录与物理文件完全一致，无需处理。'))
            self._clean_empty_dirs(fix)
            return

        if not fix and not purge_dead:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('以上仅为检查结果，未做任何修改。'))
            self.stdout.write('  清理孤儿文件与空目录：python manage.py audit_files --fix')
            self.stdout.write('  同时清理死记录：      python manage.py audit_files --fix --purge-dead')
            return

        deleted_orphans = 0
        skipped_fresh = 0
        if fix:
            self.stdout.write('')
            for disk_name, paths in orphans.items():
                for path in paths:
                    age = self._age_in_minutes(disk_name, path)
                    if age is not None and age < grace_minutes:
                        skipped_fresh += 1
                        continue
                    try:
                        full = _disk_path(disk_name, path)
                        if os.path.isfile(full):
                            os.remove(full)
                            deleted_orphans += 1
                    except OSError as exc:
                        self.stderr.write(f'  ✗ 删除失败 {disk_name}:{path} — {exc}')
                        logger.exception('orphan delete failed: %s', path)
            self.stdout.write(self.style.SUCCESS(f'已清理孤儿文件：{deleted_orphans} 个'))
            if skipped_fresh > 0:
                self.stdout.write(
                    f'  跳过 {skipped_fresh} 个「{grace_minutes} 分钟内新增」的文件（可能是正在上传中）'
                )
            self._clean_empty_dirs(True)

        deleted_missing = 0
        if purge_dead and missing_count > 0:
            if self._dead_record_check_safe(missing_count):
                deleted_missing = self._purge_dead_records(missing)
                self.stdout.write(self.style.SUCCESS(f'已清理死记录：{deleted_missing} 条'))
            else:
                self.stderr.write(self.style.ERROR(
                    '死记录数量异常偏多，疑似磁盘未挂载或权限异常，已中止删除以防误删。请人工确认后再执行。'
                ))

        if deleted_orphans or deleted_missing:
            log_operation('文件巡检', '自动清理',
                          f'files:gc 自动清理：孤儿文件 {deleted_orphans} 个、'
                          f'死记录 {deleted_missing} 条、空目录若干')

    # -------- 收集「数据库引用的物理文件路径」 --------
    def _build_references(self) -> dict[str, set[str]]:
        refs: dict[str, set[str]] = {disk: set() for disk in MANAGED.keys()}

        def add(disk: str, rel: str | None):
            if not rel:
                return
            rel = str(rel).strip('/')
            if rel:
                refs.setdefault(disk, set()).add(rel)

        from core import models as m  # noqa: WPS433

        # 注意：values_list 必须带 flat=True，否则每项是元组，
        # 拼出来的路径永远对不上，会把正常文件误判为孤儿、正常记录误判为死记录。
        for f in m.File.all_objects.values_list('path', flat=True):
            add('file_repository', f)

        # 历史版本快照（改造后 path 指向独立快照文件）
        with connection.cursor() as cur:
            cur.execute("SELECT path FROM file_versions WHERE path IS NOT NULL")
            for (p,) in cur.fetchall():
                add('file_repository', p)

        for df in m.DepartmentFile.all_objects.values_list('path', flat=True):
            add('file_repository', df)

        for sd in m.CollabDocument.all_objects.values_list('storage_path', flat=True):
            add('file_repository', sd)

        for mp in m.Message.all_objects.values_list('file_path', flat=True):
            add('public', mp)

        for wp in m.WecomFile.all_objects.values_list('file_path', flat=True):
            add('wecom_files', wp)

        # 头像（默认头像放在 static/avatars/defaults/ 下，不算 MEDIA_ROOT 下的真实文件）
        for av in m.User.all_objects.exclude(avatar__isnull=True).exclude(avatar='').values_list('avatar', flat=True):
            if av and not av.startswith('defaults/'):
                add('public', f'avatars/{av}')

        return refs

    def _collab_doc_id_set(self) -> set[int]:
        from core import models as m
        # 必须 flat=True：values_list('id') 返回的是元组集合，会让「历史快照豁免」
        # 判断永远失败，从而把协作文档的历史快照误删成孤儿文件。
        return set(m.CollabDocument.all_objects.values_list('id', flat=True))

    # -------- 扫描受管目录，返回 [孤儿, 死记录] --------
    def _scan(self, references: dict[str, set[str]], collab_ids: set[int]):
        orphans: dict[str, list[str]] = {}
        missing: dict[str, list[str]] = {}

        # 死记录：所有引用路径里、磁盘不存在的
        for disk_name, paths in references.items():
            for p in paths:
                full = _disk_path(disk_name, p)
                if not os.path.exists(full):
                    missing.setdefault(disk_name, []).append(p)

        # 孤儿文件：受管目录下、引用集合里没有的
        for disk_name, spec in MANAGED.items():
            root = DISK_ROOT.get(disk_name, lambda: settings.MEDIA_ROOT)()
            if not os.path.isdir(root):
                continue
            for path in self._list_managed_files(root, spec):
                if path in references.get(disk_name, set()):
                    continue
                # 协作文档的历史快照：文件名 {docId}_v{n}.{ext}，
                # 只要该文档记录仍存在，快照就不算孤儿。
                m = re.match(r'^collab/\d+/history/(\d+)_v\d+\.', path)
                if m and disk_name == 'file_repository':
                    doc_id = int(m.group(1))
                    if doc_id in collab_ids:
                        continue
                orphans.setdefault(disk_name, []).append(path)

        return orphans, missing

    def _list_managed_files(self, root: str, spec: dict) -> list[str]:
        result: list[str] = []
        for d in self._managed_roots(root, spec):
            for cur_root, _, files in os.walk(d):
                for name in files:
                    if name.startswith('.'):
                        continue
                    rel = os.path.relpath(os.path.join(cur_root, name), root).replace('\\', '/')
                    result.append(rel)
        return result

    def _managed_roots(self, root: str, spec: dict) -> list[str]:
        roots: list[str] = []
        for d in spec.get('paths', []):
            if d == '':
                if not os.path.isdir(root):
                    continue
                for s in os.listdir(root):
                    sub = os.path.join(root, s)
                    if os.path.isdir(sub):
                        roots.append(sub)
            else:
                full = os.path.join(root, d)
                if os.path.isdir(full):
                    roots.append(full)

        for pattern in spec.get('patterns', []):
            if not os.path.isdir(root):
                continue
            for s in os.listdir(root):
                if not fnmatch.fnmatch(s, pattern):
                    continue
                sub = os.path.join(root, s)
                if os.path.isdir(sub):
                    roots.append(sub)

        # 去重
        return sorted(set(roots))

    # -------- 文件年龄 --------
    def _age_in_minutes(self, disk: str, rel: str) -> int | None:
        try:
            full = _disk_path(disk, rel)
            mtime = os.path.getmtime(full)
            return int((time.time() - mtime) / 60)
        except OSError:
            return None

    # -------- 死记录占比异常时的安全阀 --------
    def _dead_record_check_safe(self, missing_count: int) -> bool:
        from core import models as m
        total = (m.File.all_objects.count()
                 + m.DepartmentFile.all_objects.count()
                 + m.CollabDocument.all_objects.count()
                 + m.Message.all_objects.count())
        if total == 0:
            return True
        return (missing_count / total) <= 0.20

    def _purge_dead_records(self, missing: dict[str, list[str]]) -> int:
        from core import models as m
        deleted = 0
        for disk_name, paths in missing.items():
            for path in paths:
                try:
                    # 注意：必须把 path 传给 _purge_by_path，否则会静默什么都不删。
                    if disk_name == 'file_repository':
                        deleted += self._purge_by_path(m.File, 'path', path)
                        deleted += self._purge_by_path(m.DepartmentFile, 'path', path)
                        deleted += self._purge_by_path(m.CollabDocument, 'storage_path', path)
                        deleted += self._purge_file_version_by_path(path)
                    elif disk_name == 'public':
                        deleted += self._purge_by_path(m.Message, 'file_path', path)
                    elif disk_name == 'wecom_files':
                        deleted += self._purge_by_path(m.WecomFile, 'file_path', path)
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(f'  ✗ 清理死记录失败 {disk_name}:{path} — {exc}')
                    logger.exception('purge dead failed: %s', path)
        return deleted

    def _purge_by_path(self, model_cls, column: str, path: str | None = None) -> int:
        from core.services.trash_service import force_delete as _force_delete
        if path is None:
            return 0
        count = 0
        for item in model_cls.all_objects.filter(**{column: path}):
            _force_delete(item)
            count += 1
        return count

    def _purge_file_version_by_path(self, path: str) -> int:
        with connection.cursor() as cur:
            cur.execute("SELECT id FROM file_versions WHERE path = %s", [path])
            ids = [r[0] for r in cur.fetchall()]
            if ids:
                cur.execute("DELETE FROM file_versions WHERE id IN (" +
                            ','.join(['%s'] * len(ids)) + ")", ids)
        return len(ids)

    # -------- 输出 --------
    def _report(self, title: str, groups: dict[str, list[str]], total: int):
        self.stdout.write('')
        self.stdout.write(f'【{title}】{total} 个')
        if total == 0:
            return
        for disk_name, paths in groups.items():
            for p in paths[:20]:
                self.stdout.write(f'  {disk_name}:{p}')
            rest = len(paths) - 20
            if rest > 0:
                self.stdout.write(f'  … 另有 {rest} 个同类项')

    # -------- 空目录清理（自底向上） --------
    def _clean_empty_dirs(self, fix: bool):
        if not fix:
            return
        removed = 0
        for disk_name, spec in MANAGED.items():
            root = DISK_ROOT.get(disk_name, lambda: settings.MEDIA_ROOT)()
            if not os.path.isdir(root):
                continue
            all_dirs: list[str] = []
            for d in self._managed_roots(root, spec):
                if os.path.isdir(d):
                    for cur_root, dirs, _ in os.walk(d):
                        for sub in dirs:
                            all_dirs.append(os.path.join(cur_root, sub))
            # 深的在前
            all_dirs.sort(key=lambda x: -x.count(os.sep))
            for d in all_dirs:
                if not os.path.isdir(d):
                    continue
                try:
                    if not os.listdir(d):
                        os.rmdir(d)
                        removed += 1
                except OSError as exc:
                    logger.warning('remove empty dir failed: %s | %s', d, exc)
        if removed:
            self.stdout.write(f'已清理空目录：{removed} 个')