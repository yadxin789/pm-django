# -*- coding: utf-8 -*-
"""AI 智能助手四表：ai_settings / ai_doc_templates / ai_skills / ai_rate_counters。

只创建参考目录 2026-10-19 新增的 AI 相关表，并播种默认设置与文档模板。
其余历史表的字段/顺序由库内 SQL 快照维护，本迁移不触碰（避免重复加列报错）。
"""
from django.db import migrations, models


def seed_ai(apps, schema_editor):
    AiSetting = apps.get_model('core', 'AiSetting')
    AiDocTemplate = apps.get_model('core', 'AiDocTemplate')
    AiSkill = apps.get_model('core', 'AiSkill')

    AiSetting.objects.get_or_create(
        pk=1,
        defaults={
            'provider': 'deepseek',
            'base_url': 'https://api.deepseek.com/v1',
            'model': 'deepseek-chat',
            'temperature': 0.70,
            'max_tokens': 2000,
            'enabled': False,
            'rate_limit_minute': 30,
            'rate_limit_daily': 200,
        },
    )

    templates = [
        {
            'name': '项目立项报告（可行性分析）',
            'category': '通用',
            'description': '项目立项/可行性报告',
            'template_path': 'templates/project-proposal.md',
            'example_path': None,
            'prompt_guide': '请严格按照模板结构，把用户提供的项目名称、产品型号、版本、周期、参与部门、目标与资源需求填入对应章节；未提供的信息保留【待补充】占位，不要编造。',
        },
        {
            'name': '产品研发周报',
            'category': '研发过程',
            'description': '研发过程周报（进展/问题/下周计划）',
            'template_path': 'templates/weekly-report.md',
            'example_path': 'templates/weekly-report-example.md',
            'prompt_guide': '先读取范例文件理解口径；从模板中读取本周完成、问题与风险、下周计划；可结合系统中进行中项目与任务真实数据填充，未提供处标【待补充】。',
        },
        {
            'name': '会议纪要',
            'category': '通用',
            'description': '会议纪要（议题/结论/待办）',
            'template_path': 'templates/meeting-minutes.md',
            'example_path': None,
            'prompt_guide': '按模板列出会议时间、参会人、议题结论与待办事项（责任人/截止时间）；未提供的参会人、决议保留占位。',
        },
        {
            'name': '需求文档（PRD 简版）',
            'category': '通用',
            'description': '需求文档 / PRD',
            'template_path': 'templates/requirements-doc.md',
            'example_path': None,
            'prompt_guide': '按模板整理功能需求、非功能需求、验收标准；用户用自然语言给出的需求逐条拆成条目，不要自行扩展未提及的功能。',
        },
        {
            'name': '项目验收报告',
            'category': '通用',
            'description': '项目交付验收报告',
            'template_path': 'templates/acceptance-report.md',
            'example_path': None,
            'prompt_guide': '按模板填写交付范围、完成项、遗留问题、验收结论；可结合已完成项目与已交付文件，未提供处标【待补充】。',
        },
    ]
    tpl_ids = []
    for row in templates:
        obj, _ = AiDocTemplate.objects.get_or_create(
            name=row['name'],
            defaults={
                'category': row['category'],
                'description': row['description'],
                'template_path': row['template_path'],
                'example_path': row['example_path'],
                'prompt_guide': row['prompt_guide'],
                'enabled': True,
            },
        )
        tpl_ids.append(obj.id)

    AiSkill.objects.get_or_create(
        slug='pm-docs-generator',
        defaults={
            'name': 'PM 文档生成器',
            'description': '按模板与知识库自然语言生成 PM 交付文档',
            'type': 'doc-generator',
            'config': {'templates': tpl_ids},
            'enabled': True,
        },
    )


def unseed_ai(apps, schema_editor):
    apps.get_model('core', 'AiSkill').objects.filter(slug='pm-docs-generator').delete()
    apps.get_model('core', 'AiDocTemplate').objects.all().delete()
    apps.get_model('core', 'AiSetting').objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_add_deleted_by_to_users_and_departments'),
    ]

    operations = [
        migrations.CreateModel(
            name='AiSetting',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('provider', models.CharField(default='deepseek', max_length=40)),
                ('api_key', models.CharField(blank=True, max_length=500, null=True)),
                ('base_url', models.CharField(blank=True, max_length=500, null=True)),
                ('model', models.CharField(default='deepseek-chat', max_length=100)),
                ('vision_enabled', models.BooleanField(default=False)),
                ('vision_model', models.CharField(blank=True, max_length=100, null=True)),
                ('temperature', models.DecimalField(decimal_places=2, default=0.7, max_digits=3)),
                ('max_tokens', models.IntegerField(default=2000)),
                ('enabled', models.BooleanField(default=False)),
                ('rate_limit_minute', models.PositiveIntegerField(default=30)),
                ('rate_limit_daily', models.PositiveIntegerField(default=200)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'ai_settings'},
        ),
        migrations.CreateModel(
            name='AiDocTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=150)),
                ('category', models.CharField(default='通用', max_length=80)),
                ('description', models.TextField(blank=True, null=True)),
                ('template_path', models.CharField(blank=True, max_length=300, null=True)),
                ('example_path', models.CharField(blank=True, max_length=300, null=True)),
                ('support_files', models.JSONField(blank=True, default=list, null=True)),
                ('prompt_guide', models.TextField(blank=True, null=True)),
                ('enabled', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'ai_doc_templates', 'ordering': ['category', 'id']},
        ),
        migrations.CreateModel(
            name='AiSkill',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('slug', models.CharField(max_length=100, unique=True)),
                ('description', models.TextField(blank=True, null=True)),
                ('type', models.CharField(default='tool', max_length=30)),
                ('config', models.JSONField(blank=True, default=dict, null=True)),
                ('enabled', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'ai_skills'},
        ),
        migrations.CreateModel(
            name='AiRateCounter',
            fields=[
                ('key', models.CharField(max_length=120, primary_key=True, serialize=False)),
                ('count', models.IntegerField(default=0)),
                ('reset_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'ai_rate_counters'},
        ),
        migrations.RunPython(seed_ai, reverse_code=unseed_ai),
    ]
