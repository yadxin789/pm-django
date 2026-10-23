# -*- coding: utf-8 -*-
"""为纳入回收站的表补充 deleted_by 列（对应参考目录 2026_10_14_000002_add_deleted_by_for_trash）。

用途：
  - 与各模型的「归属人列」共同决定个人回收站的可见范围：
    我删除的（deleted_by = 我） 或 我创建的（归属人 = 我）；
    这样管理员删除的内容，创建人依然能恢复。

约定：
  - 软删除时由 TrashService 自动写入 deleted_by；
  - 恢复时由 TrashService::restore 清空；
  - 历史数据为 NULL，不影响按「归属人」匹配。
"""
from django.db import migrations


TABLE_LIST = [
    'collab_documents',
    'projects',
    'tasks',
    'files',
    'department_files',
    'messages',
    'wecom_files',
    'notifications',
    'knowledges',
    'knowledge_docs',
    'project_templates',
]


def add_deleted_by(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        for table in TABLE_LIST:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s "
                "AND column_name = 'deleted_by'",
                [table],
            )
            if cur.fetchone()[0]:
                continue
            cur.execute(
                f"ALTER TABLE `{table}` "
                f"ADD COLUMN `deleted_by` BIGINT NULL, "
                f"ADD INDEX `{table}_deleted_by_idx` (`deleted_by`)"
            )


def drop_deleted_by(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        for table in TABLE_LIST:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s "
                "AND column_name = 'deleted_by'",
                [table],
            )
            if not cur.fetchone()[0]:
                continue
            cur.execute(f"ALTER TABLE `{table}` DROP COLUMN `deleted_by`")


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0002_add_soft_deletes_for_trash'),
    ]

    operations = [
        migrations.RunPython(add_deleted_by, reverse_code=drop_deleted_by),
    ]