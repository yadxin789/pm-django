# -*- coding: utf-8 -*-
"""为纳入"回收站"的表补 deleted_at 列（对应参考目录 2026_10_14_000001_add_soft_deletes_for_trash）。

目标表（users / projects / tasks / files / messages / collab_documents 在建表阶段
已包含 deleted_at；本迁移负责补齐其余参与回收站的表）：
  - department_files   部门资料文件
  - wecom_files       企微文件
  - notifications     通知
  - knowledges        知识库
  - knowledge_docs    知识文档
  - project_templates 项目模板
"""
from django.db import migrations, models


TABLE_LIST = [
    'department_files',
    'wecom_files',
    'notifications',
    'knowledges',
    'knowledge_docs',
    'project_templates',
]


def add_deleted_at(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        for table in TABLE_LIST:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s "
                "AND column_name = 'deleted_at'",
                [table],
            )
            exists = cur.fetchone()[0]
            if exists:
                continue
            cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `deleted_at` DATETIME(6) NULL")


def drop_deleted_at(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        for table in TABLE_LIST:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s "
                "AND column_name = 'deleted_at'",
                [table],
            )
            exists = cur.fetchone()[0]
            if exists:
                cur.execute(f"ALTER TABLE `{table}` DROP COLUMN `deleted_at`")


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(add_deleted_at, reverse_code=drop_deleted_at),
    ]