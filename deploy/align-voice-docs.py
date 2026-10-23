# -*- coding: utf-8 -*-
"""把电话会议旧版「临时演示文档」并入统一文档库（media/collab/<项目>/）。

背景：电话会议模块原先把上传文件放在 media/voice_demo/，并标记 is_temp_demo=True、
expires_at=+7 天；协同办公模块用的是 media/collab/<项目>/。两处不一致，而且模型上挂着
「过期」语义的字段。对齐后两个模块统一使用 media/collab/<项目>/、不再有过期标记，
本脚本负责把历史文件搬过去（幂等：只处理 storage_path 以 voice_demo/ 开头的记录）。

用法：
    /var/www/pm-django/venv/bin/python deploy/align-voice-docs.py [--dry-run]
"""
import os
import shutil
import sys
import uuid

import django

PROJECT_DIR = os.environ.get('PM_PROJECT_DIR', '/var/www/pm-django')
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pm.settings')
django.setup()

from django.conf import settings                  # noqa: E402

from core.models import CollabDocument            # noqa: E402

DRY_RUN = '--dry-run' in sys.argv


def main():
    rows = list(CollabDocument.objects.filter(storage_path__startswith='voice_demo/'))
    print(f'{"（演练模式，不做写入）" if DRY_RUN else ""}待迁移的临时演示文档：{len(rows)} 条')
    if not rows:
        print('没有需要迁移的记录，文档库已经是统一的，无需操作。')
        return 0

    media = settings.MEDIA_ROOT
    moved = missing = 0
    for d in rows:
        old_path = os.path.join(media, d.storage_path)
        new_rel = f'collab/{d.project_id}/doc_{uuid.uuid4().hex}.{d.extension}'
        new_path = os.path.join(media, new_rel)
        if os.path.exists(old_path):
            if not DRY_RUN:
                os.makedirs(os.path.dirname(new_path), exist_ok=True)
                shutil.move(old_path, new_path)
                d.storage_path = new_rel
            print(f'  [移动] #{d.id} 《{d.name}》 voice_demo/… → {new_rel}')
            moved += 1
        else:
            print(f'  [警告] #{d.id} 《{d.name}》 原文件不存在（{d.storage_path}），仅清除临时标记')
            missing += 1
        if not DRY_RUN:
            d.is_temp_demo = False
            d.expires_at = None
            d.save(update_fields=['storage_path', 'is_temp_demo', 'expires_at', 'updated_at'])

    if not DRY_RUN:
        left = CollabDocument.objects.filter(storage_path__startswith='voice_demo/').count()
        print(f'\n迁移完成：移动 {moved} 个文件，{missing} 个原文件缺失；仍指向 voice_demo/ 的记录：{left} 条')
    return 0


if __name__ == '__main__':
    sys.exit(main())
