# -*- coding: utf-8 -*-
"""AI 多供应商 / 多模型 / 多会话：补 ai_providers / ai_models / ai_conversations /
ai_messages / ai_attachments 五张表（对齐参考 database/migrations 中同名表）。

同时把 AiSetting::providers() 的 4 家预设供应商与其预设模型写入 ai_providers / ai_models，
使升级后「AI 设置 → 供应商列表」与参考目录一致。
"""
from django.db import migrations, models
import django.db.models.deletion

# 与 core.models.AiSetting.PROVIDERS 保持一致（迁移内不可直接引用模型类常量，
# 避免将来改默认值时改写历史迁移语义）。
PRESET_PROVIDERS = [
    ('deepseek', 'DeepSeek（深度求索）', 'https://api.deepseek.com/v1', [
        ('deepseek-chat', 'DeepSeek-V3（通用对话）'),
        ('deepseek-reasoner', 'DeepSeek-R1（推理模型）'),
    ]),
    ('qwen', '通义千问（阿里云）', 'https://dashscope.aliyuncs.com/compatible-mode/v1', [
        ('qwen-turbo', 'Qwen-Turbo（快速低成本）'),
        ('qwen-plus', 'Qwen-Plus（均衡）'),
        ('qwen-max', 'Qwen-Max（最强）'),
    ]),
    ('doubao', '豆包（字节跳动）', 'https://ark.cn-beijing.volces.com/api/v3', [
        ('doubao-pro-4k', 'Doubao-Pro-4k'),
        ('doubao-pro-32k', 'Doubao-Pro-32k'),
        ('doubao-pro-128k', 'Doubao-Pro-128k'),
    ]),
    ('hunyuan', '腾讯混元', 'https://api.hunyuan.cloud.tencent.com/v1', [
        ('hunyuan-lite', 'Hunyuan-Lite（免费）'),
        ('hunyuan-pro', 'Hunyuan-Pro'),
        ('hunyuan-standard', 'Hunyuan-Standard'),
    ]),
]


def seed_presets(apps, schema_editor):
    AiProvider = apps.get_model('core', 'AiProvider')
    AiModel = apps.get_model('core', 'AiModel')
    for key, name, base_url, models_ in PRESET_PROVIDERS:
        row, _ = AiProvider.objects.get_or_create(
            key=key, defaults={'name': name, 'base_url': base_url, 'enabled': False})
        order = AiModel.objects.filter(provider=row).count()
        for model_id, label in models_:
            if AiModel.objects.filter(provider=row, model_id=model_id).exists():
                continue
            order += 1
            AiModel.objects.create(
                provider=row, model_id=model_id, name=label,
                enabled=True, sort_order=order,
            )


def unseed_presets(apps, schema_editor):
    apps.get_model('core', 'AiModel').objects.all().delete()
    apps.get_model('core', 'AiProvider').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [('core', '0007_ai_tables')]

    operations = [
        migrations.CreateModel(
            name='AiProvider',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=40, unique=True)),
                ('name', models.CharField(max_length=120)),
                ('base_url', models.CharField(blank=True, max_length=500, null=True)),
                ('api_key', models.CharField(blank=True, max_length=500, null=True)),
                ('enabled', models.BooleanField(default=False)),
                ('is_default', models.BooleanField(default=False)),
                ('note', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'ai_providers', 'ordering': ['id']},
        ),
        migrations.CreateModel(
            name='AiModel',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('model_id', models.CharField(max_length=120)),
                ('name', models.CharField(blank=True, max_length=150, null=True)),
                ('is_vision', models.BooleanField(default=False)),
                ('enabled', models.BooleanField(default=True)),
                ('is_default', models.BooleanField(default=False)),
                ('sort_order', models.IntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('provider', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='models', to='core.aiprovider', db_column='provider_id')),
            ],
            options={'db_table': 'ai_models', 'ordering': ['sort_order', 'id']},
        ),
        migrations.CreateModel(
            name='AiConversation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('title', models.CharField(default='新会话', max_length=200)),
                ('skill_slug', models.CharField(blank=True, max_length=100, null=True)),
                ('message_count', models.IntegerField(default=0)),
                ('last_message_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('model', models.ForeignKey(
                    blank=True, db_column='model_id', null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='conversations', to='core.aimodel')),
                ('user', models.ForeignKey(
                    db_column='user_id', on_delete=django.db.models.deletion.CASCADE,
                    related_name='ai_conversations', to='core.user')),
            ],
            options={'db_table': 'ai_conversations', 'ordering': ['-last_message_at', '-id']},
        ),
        migrations.CreateModel(
            name='AiMessage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(max_length=20)),
                ('content', models.TextField(blank=True, null=True)),
                ('meta', models.JSONField(blank=True, default=dict, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('conversation', models.ForeignKey(
                    db_column='conversation_id', on_delete=django.db.models.deletion.CASCADE,
                    related_name='messages', to='core.aiconversation')),
            ],
            options={'db_table': 'ai_messages', 'ordering': ['id']},
        ),
        migrations.CreateModel(
            name='AiAttachment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('type', models.CharField(max_length=20)),
                ('original_name', models.CharField(max_length=255)),
                ('path', models.CharField(max_length=500)),
                ('mime_type', models.CharField(blank=True, max_length=120, null=True)),
                ('size', models.BigIntegerField(default=0)),
                ('text_extracted', models.BooleanField(default=False)),
                ('extracted_text', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('conversation', models.ForeignKey(
                    db_column='conversation_id', on_delete=django.db.models.deletion.CASCADE,
                    related_name='attachments', to='core.aiconversation')),
                ('message', models.ForeignKey(
                    blank=True, db_column='message_id', null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='attachments', to='core.aimessage')),
                ('user', models.ForeignKey(
                    db_column='user_id', on_delete=django.db.models.deletion.CASCADE,
                    related_name='ai_attachments', to='core.user')),
            ],
            options={'db_table': 'ai_attachments', 'ordering': ['id']},
        ),
        migrations.RunPython(seed_presets, unseed_presets),
    ]
