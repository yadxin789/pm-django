# -*- coding: utf-8 -*-
"""任务文件历史版本快照改造（对应参考目录 2026_10_15_000001_alter_file_versions_for_snapshots）。

原设计的问题：
  - file_versions.path 指向与 files.path 相同的路径（不复制文件）；
  - file_id 外键为 CASCADE，旧版本被彻底删除后版本记录会随之消失。

改造后：
  - task_id（CASCADE）：快照归属任务，任务彻底删除时快照记录一并清理；
  - file_id 改为 SET NULL：原文件记录消失后版本记录依然存在；
  - path 可空：缺失文件不会回滚条目。
"""
from django.db import migrations, models


def alter_file_view_mysql(apps, schema_editor):
    """MySQL：先放宽原字段约束，再加 task_id 列与外键。"""
    conn = schema_editor.connection
    with conn.cursor() as cur:
        # 1. file_id 放宽到可空
        cur.execute("ALTER TABLE `file_versions` MODIFY COLUMN `file_id` BIGINT NULL")
        # 2. path 放宽到可空
        cur.execute("ALTER TABLE `file_versions` MODIFY COLUMN `path` VARCHAR(255) NULL")
        # 3. 检查 task_id 是否已存在
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'file_versions' "
            "AND column_name = 'task_id'"
        )
        if cur.fetchone()[0] == 0:
            cur.execute("ALTER TABLE `file_versions` ADD COLUMN `task_id` BIGINT NULL AFTER `file_id`")
            cur.execute(
                "ALTER TABLE `file_versions` "
                "ADD CONSTRAINT `file_versions_task_id_xxxx` "
                "FOREIGN KEY (`task_id`) REFERENCES `tasks`(`id`) ON DELETE CASCADE"
            )


def revert_file_view_mysql(apps, schema_editor):
    conn = schema_editor.connection
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'file_versions' "
            "AND column_name = 'task_id'"
        )
        if cur.fetchone()[0]:
            cur.execute("ALTER TABLE `file_versions` DROP FOREIGN KEY `file_versions_task_id_xxxx`")
            cur.execute("ALTER TABLE `file_versions` DROP COLUMN `task_id`")
        cur.execute("ALTER TABLE `file_versions` MODIFY COLUMN `file_id` BIGINT NOT NULL")
        cur.execute("ALTER TABLE `file_versions` MODIFY COLUMN `path` VARCHAR(255) NOT NULL")


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0003_add_deleted_by_for_trash'),
    ]

    operations = [
        migrations.RunPython(alter_file_view_mysql, reverse_code=revert_file_view_mysql),
    ]