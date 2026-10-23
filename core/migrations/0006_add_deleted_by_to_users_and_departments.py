# -*- coding: utf-8 -*-
"""补充分类回收站遗漏的 deleted_by 列与索引。

0.0003_add_deleted_by_for_trash 已通过 RunPython 为大多数表补充了 deleted_by，
但 User 与 Department 当时尚未纳入，因此单独补齐。同时补齐 file_versions 的任务
创建时间索引（fv_task_created_idx）。
"""
from django.db import migrations


COLUMNS_TO_ADD = [
    ('users', 'deleted_by'),
    ('departments', 'deleted_by'),
]

INDEXES = [
    # file_versions 按 task_id + created_at 索引，用于快照清理快速扫描
    ('file_versions', 'fv_task_created_idx', ['task_id', 'created_at']),
]


def add_missing_columns(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        for table, column in COLUMNS_TO_ADD:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
                [table, column],
            )
            if cur.fetchone()[0]:
                continue
            cur.execute(
                f"ALTER TABLE `{table}` ADD COLUMN `{column}` BIGINT NULL, "
                f"ADD INDEX `{table}_deleted_by_idx` (`{column}`)"
            )


def drop_added_columns(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        for table, column in COLUMNS_TO_ADD:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
                [table, column],
            )
            if not cur.fetchone()[0]:
                continue
            cur.execute(f"ALTER TABLE `{table}` DROP COLUMN `{column}`")


def add_missing_indexes(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        for table, idx_name, columns in INDEXES:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s",
                [table, idx_name],
            )
            if cur.fetchone()[0]:
                continue
            cols = ', '.join(f'`{c}`' for c in columns)
            cur.execute(f"CREATE INDEX `{idx_name}` ON `{table}` ({cols})")


def drop_missing_indexes(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        for table, idx_name, columns in INDEXES:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s",
                [table, idx_name],
            )
            if not cur.fetchone()[0]:
                continue
            cur.execute(f"DROP INDEX `{idx_name}` ON `{table}`")


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0005_add_soft_deletes_to_departments_table'),
    ]

    operations = [
        migrations.RunPython(add_missing_columns, reverse_code=drop_added_columns),
        migrations.RunPython(add_missing_indexes, reverse_code=drop_missing_indexes),
    ]
