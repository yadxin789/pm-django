# -*- coding: utf-8 -*-
"""部门支持软删除（对应参考目录 2026_10_15_000002_add_soft_deletes_to_departments_table）。

背景：原 departments 表无 deleted_at 列、Department 模型未使用 SoftDeletes trait，
导致「删部门」实际是物理删除，进而触发 users.department_id 的 RESTRICT 外键冲突。

软删部门：
  - 避开外键冲突，部门可正常删除；
  - 部门成员转为普通用户后仍能保留原身份访问历史项目；
  - 部门本身进回收站 15 天流程，超期由 trash:purge-expired 一并清理。
"""
from django.db import migrations


def add_departments_deleted(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'departments' "
            "AND column_name = 'deleted_at'"
        )
        if cur.fetchone()[0]:
            return
        cur.execute("ALTER TABLE `departments` ADD COLUMN `deleted_at` DATETIME(6) NULL")
        cur.execute("ALTER TABLE `departments` ADD INDEX `departments_deleted_idx` (`deleted_at`)")


def drop_departments_deleted(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'departments' "
            "AND index_name = 'departments_deleted_idx'"
        )
        if cur.fetchone()[0]:
            cur.execute("ALTER TABLE `departments` DROP INDEX `departments_deleted_idx`")
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'departments' "
            "AND column_name = 'deleted_at'"
        )
        if cur.fetchone()[0]:
            cur.execute("ALTER TABLE `departments` DROP COLUMN `deleted_at`")


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0004_alter_file_versions_for_snapshots'),
    ]

    operations = [
        migrations.RunPython(add_departments_deleted, reverse_code=drop_departments_deleted),
    ]