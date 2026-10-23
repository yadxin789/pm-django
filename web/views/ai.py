# -*- coding: utf-8 -*-
"""AI 智能助手视图（对齐参考 app/Http/Controllers/AiController.php）。

包含：开通引导 / 未开通提示 / 多会话聊天 / 发送(带工具+技能+附件) /
会话管理(新建·重命名·删除·清空·导出) / 附件预览与下载 /
管理员设置 / 多供应商管理 / 多模型管理 / 自然语言生成文档。

与参考的行为约定：
  - 管理员账号**不使用** AI 聊天与文档生成，仅做管理（聊天/文档页会跳到设置页）；
  - 非管理员不可进入任何设置类路由（403）；
  - 会话/附件严格归属本人（越权 403）。
"""
import os
import re
import uuid

from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.http import (
    FileResponse, HttpResponse, HttpResponseForbidden, JsonResponse, StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import (
    AiAttachment, AiConversation, AiDocTemplate, AiMessage, AiModel, AiProvider,
    AiSetting, AiSkill,
)
from core.services.ai_service import AiService
from core.services.ai_quota import DocGenLock, check_and_consume

# AI 聊天附件物理根目录（对齐 Laravel storage disk 'ai_chat'）
AI_CHAT_ROOT_NAME = 'ai_chat'

_quote_filename = quote  # Content-Disposition 的 RFC5987 文件名编码

# 可从文件里抽取正文的扩展名（对齐 AiController::extractText）
TEXT_EXTENSIONS = {
    'txt', 'md', 'markdown', 'csv', 'json', 'xml', 'log', 'yaml', 'yml', 'ini', 'sql', 'html', 'htm',
}
MAX_EXTRACT_BYTES = 5 * 1024 * 1024
MAX_EXTRACT_CHARS = 20000

IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp'}


# ==================== 通用工具 ====================

def _require_admin(request):
    return request.user.is_authenticated and request.user.is_admin()


def _ai_root():
    """AI 聊天附件的物理根目录（受管媒体根下）。"""
    return os.path.join(str(settings.PM_MEDIA_ROOT), AI_CHAT_ROOT_NAME)


def _is_ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


def _safe_filename(name):
    """去掉路径分隔符，避免写入到预期目录之外。"""
    return re.sub(r'[\\/]+', '_', (name or 'attachment')).strip() or 'attachment'


def _readable_size(size):
    size = float(size or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{round(size, 2)} {unit}'
        size /= 1024
    return '0 B'


def _extract_text(uploaded_file, extension):
    """抽取纯文本文件内容；非文本类型返回空串（对齐 AiController::extractText）。"""
    if (extension or '').lower() not in TEXT_EXTENSIONS:
        return ''
    try:
        if uploaded_file.size and uploaded_file.size > MAX_EXTRACT_BYTES:
            return ''
        raw = uploaded_file.read()
    except Exception:
        return ''

    for encoding in ('utf-8', 'gb18030', 'gbk', 'big5', 'latin-1'):
        try:
            content = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        return ''

    # 去掉控制字符（保留制表符/换行），避免把乱码灌给模型
    content = ''.join(ch for ch in content if ch == '\t' or ch == '\n' or ch == '\r' or ord(ch) >= 32)
    return content.strip()


def _save_upload(uploaded_file, user_id):
    """保存上传文件到 AI 附件目录，返回相对路径。"""
    extension = (uploaded_file.name.rsplit('.', 1)[-1] if '.' in uploaded_file.name else '').lower()
    relative_dir = os.path.join(AI_CHAT_ROOT_NAME, f'u{user_id}', timezone.localdate().strftime('%Y%m%d'))
    absolute_dir = os.path.join(str(settings.PM_MEDIA_ROOT), relative_dir)
    os.makedirs(absolute_dir, exist_ok=True)
    filename = uuid.uuid4().hex + (f'.{extension}' if extension else '')
    absolute_path = os.path.join(absolute_dir, filename)
    with open(absolute_path, 'wb+') as destination:
        for chunk in uploaded_file.chunks():
            destination.write(chunk)
    return os.path.join(relative_dir, filename).replace('\\', '/')


def _title_for_conversation(conversation, message):
    """会话标题：首个用户问题的前 24 个字符。"""
    if conversation.title and conversation.title != '新会话':
        return conversation.title
    first = conversation.messages.filter(role='user').values_list('content', flat=True).first()
    content = (first if first not in (None, '') else (message or '')).strip()
    if not content:
        return conversation.title or '新会话'
    content = content.replace('\r', ' ').replace('\n', ' ')
    return content[:24] + ('…' if len(content) > 24 else '')


def _delete_conversation_files(conversation):
    """清理会话附件（物理文件 + 记录）。"""
    for attachment in conversation.attachments.all():
        path = attachment.absolute_path()
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
    conversation.attachments.all().delete()


def _migrate_legacy_session_history(request):
    """把旧版存在 Session 里的对话一次性迁移为会话记录。"""
    history = request.session.get('ai_chat_history') or []
    if not history:
        return
    user = request.user
    if AiConversation.objects.filter(user=user).exists():
        request.session.pop('ai_chat_history', None)
        return
    conversation = AiConversation.objects.create(
        user=user, title='历史会话（旧版迁移）', last_message_at=timezone.now(),
    )
    for item in history:
        AiMessage.objects.create(
            conversation=conversation,
            role=item.get('role') or 'user',
            content=item.get('content') or '',
        )
    AiConversation.objects.filter(pk=conversation.pk).update(message_count=len(history))
    request.session.pop('ai_chat_history', None)


def _resolve_conversation(request, conversation_id, skill_slug=None):
    """取得当前会话：有 id 且属于本人就用，否则新建。"""
    user = request.user
    if conversation_id:
        found = AiConversation.objects.filter(user=user, pk=conversation_id).first()
        if found:
            if skill_slug and found.skill_slug != skill_slug:
                found.skill_slug = skill_slug
                found.save(update_fields=['skill_slug', 'updated_at'])
            return found
    return AiConversation.objects.create(
        user=user, title='新会话', skill_slug=skill_slug or None, last_message_at=timezone.now(),
    )


def _handle_uploads(request, conversation, ai):
    """处理上传的图片 / 文件。

    返回 (展示元数据, 图片注入上下文标记, 文本上下文, 提示语列表)
    """
    meta, context_parts, notes = [], [], []
    image_names, unsupported_names = [], []
    user_id = request.user.id

    for uploaded in request.FILES.getlist('images'):
        if not uploaded:
            continue
        path = _save_upload(uploaded, user_id)
        attachment = AiAttachment.objects.create(
            conversation=conversation, message=None, user=request.user, type='image',
            original_name=uploaded.name, path=path,
            mime_type=getattr(uploaded, 'content_type', '') or '', size=uploaded.size or 0,
        )
        meta.append({
            'id': attachment.id, 'type': 'image', 'name': attachment.original_name,
            'size': attachment.size, 'readable_size': _readable_size(attachment.size),
            'preview_url': f'/ai/attachments/{attachment.id}/preview',
        })
        if ai.vision_enabled():
            # 图片由 _inject_vision 负责投喂，此处仅登记
            pass
        else:
            image_names.append(attachment.original_name)

    for uploaded in request.FILES.getlist('files'):
        if not uploaded:
            continue
        extension = (uploaded.name.rsplit('.', 1)[-1] if '.' in uploaded.name else '').lower()
        text = _extract_text(uploaded, extension)
        path = _save_upload(uploaded, user_id)
        attachment = AiAttachment.objects.create(
            conversation=conversation, message=None, user=request.user, type='file',
            original_name=uploaded.name, path=path,
            mime_type=getattr(uploaded, 'content_type', '') or '', size=uploaded.size or 0,
            text_extracted=bool(text),
            extracted_text=(text[:MAX_EXTRACT_CHARS] if text else None),
        )
        meta.append({
            'id': attachment.id, 'type': 'file', 'name': attachment.original_name,
            'size': attachment.size, 'readable_size': _readable_size(attachment.size),
            'download_url': f'/ai/attachments/{attachment.id}/download',
        })
        if text:
            context_parts.append(f'=== 文件：{attachment.original_name} ===')
            context_parts.append(text[:MAX_EXTRACT_CHARS])
        else:
            unsupported_names.append(attachment.original_name)

    if image_names:
        notes.append('本次还上传了图片（' + '、'.join(image_names)
                     + '）。当前 AI 未开启视觉识别能力，无法读取图片内容，请结合文字描述回答；'
                       '如需「看图」，请让管理员在 AI 设置中开启视觉模型。')
    if unsupported_names:
        notes.append('以下文件暂不支持自动解析内容（仅作记录）：' + '、'.join(unsupported_names)
                     + '。如需分析其内容，请粘贴文本或说明要点。')

    # 已开启视觉时把所有图片文件名告知决策者可 ├── 由上层通过 ai.vision_enabled() 处理
    return meta, image_names, '\n'.join(context_parts), notes


# ==================== 开通引导 / 聊天 ====================

@login_required
def welcome(request):
    """AI 开通引导页（3 步：选提供商 → 获取 Key → 粘贴并测试）。仅管理员可配置。"""
    if not _require_admin(request):
        return redirect('ai.notice')
    current = AiSetting.current()
    providers = AiSetting.PROVIDERS
    return render(request, 'ai/welcome.html', {
        'current': current, 'providers': providers, 'masked_key': current.masked_api_key(),
    })


@login_required
def notice(request):
    """「管理员尚未开通」提示页（非管理员 / 未启用时展示）。"""
    return render(request, 'ai/notice.html')


@login_required
def chat(request):
    """AI 聊天页（多会话：历史会话 / 模型切换 / 技能 / 附件）。"""
    if _require_admin(request):
        messages.warning(
            request,
            '管理员账号用于管理 AI（供应商、API Key、调用限流等），不提供 AI 聊天功能。'
            '请使用普通业务账号登录使用 AI。')
        return redirect('ai.settings')

    setting = AiSetting.current()
    ai = AiService(setting)
    if not setting.enabled or not ai.is_available():
        return redirect('ai.notice')

    # 兼容旧版：把 Session 里的对话迁移为会话记录
    _migrate_legacy_session_history(request)

    user = request.user
    conversations = (AiConversation.objects.filter(user=user)
                     .order_by('-last_message_at', '-id')[:100])

    conversation = None
    requested = request.GET.get('c')
    if requested:
        conversation = AiConversation.objects.filter(user=user, pk=requested).first()
    if conversation is None:
        conversation = conversations[0] if conversations else None

    msgs = []
    if conversation:
        msgs = (conversation.messages.prefetch_related('attachments').all())

    skills = AiSkill.objects.filter(enabled=True).order_by('id')
    usable_models = AiService.available_models()

    current_model = None
    if conversation is not None and conversation.model_id:
        current_model = AiModel.usable_by_id(conversation.model_id)
    if current_model is None:
        current_model = AiService.available_default()

    return render(request, 'ai/chat.html', {
        'conversations': conversations,
        'conversation': conversation,
        'messages': msgs,
        'skills': skills,
        'provider_info': setting.provider_info(),
        'rate_limit': {
            'minute': int(setting.rate_limit_minute or 30),
            'daily': int(setting.rate_limit_daily or 200),
        },
        'model': setting.model,
        'vision_enabled': ai.vision_enabled(),
        'models': usable_models,
        'current_model': current_model,
    })


@login_required
@require_POST
def ask(request):
    """发送消息（支持会话、技能、图片/文件附件 + PM 数据工具调用），带每用户限流。"""
    if _require_admin(request):
        return JsonResponse({
            'reply': '🚫 管理员账号仅用于管理 AI 设置，不提供 AI 聊天功能，请使用普通业务账号使用 AI。',
        }, status=403)

    setting = AiSetting.current()
    ai = AiService(setting)
    if not setting.enabled or not ai.is_available():
        return JsonResponse({
            'reply': '⚠️ AI 尚未开通，请联系系统管理员在「AI 设置」中配置至少一个供应商与模型。',
        }, status=403)

    message_text = (request.POST.get('message') or '').strip()
    if len(message_text) > 4000:
        return JsonResponse({'reply': '消息最长 4000 字。'}, status=422)

    has_upload = bool(request.FILES.getlist('images') or request.FILES.getlist('files'))
    if not message_text and not has_upload:
        return JsonResponse({'reply': '请输入问题，或上传图片 / 文件后再发送。'}, status=422)

    skill_slug = (request.POST.get('skill') or '').strip() or None
    conversation = _resolve_conversation(request, request.POST.get('conversation_id'), skill_slug)

    # 本次使用的模型：请求指定 > 会话已选 > 系统默认可用模型
    model_id = request.POST.get('model_id') or conversation.model_id
    model = ai.resolve_model(int(model_id) if model_id else None)
    if not model:
        return JsonResponse({
            'reply': '⚠️ 当前没有可用的 AI 模型，请管理员在「AI 设置」中配置至少一个供应商 Key 并启用模型。',
        }, status=422)
    ai.use_model(model)

    # 技能 → 工具子集 + 专项指令
    skill = None
    if skill_slug:
        skill = AiSkill.objects.filter(slug=skill_slug, enabled=True).first()
    tools = ai.tools_for_names((skill.config or {}).get('tools') or []) if skill else None
    extra_instructions = (skill.description or None) if skill else None

    attachment_meta, image_names, file_context, notes = _handle_uploads(request, conversation, ai)

    prompt_parts = []
    if file_context:
        prompt_parts.append('【附件说明】以下是用户本轮上传的文件的原始文本内容，由系统自动解析后提供给你，'
                            '请勿声称看不到或未收到文件内容；可直接引用其中的数据、结论与原文来回答用户提问。\n\n'
                            + file_context)
    if message_text:
        prompt_parts.append(message_text)
    prompt_parts.extend(notes)
    prompt = '\n\n'.join(prompt_parts)

    history = [
        {'role': m.role, 'content': m.content or ''}
        for m in (conversation.messages.filter(role__in=['user', 'assistant'])
                  .order_by('-id')[:20] or [])
    ][::-1]

    ok, reason = check_and_consume(request.user, setting)
    if not ok:
        return JsonResponse({'reply': '⚠️ ' + reason}, status=429)

    reply = ai.chat_with_tools(prompt, history, tools, extra_instructions)

    model_meta = {
        'skill': skill.slug if skill else None,
        'model_id': model.id,
        'model': model.model_id,
        'model_name': model.name,
        'provider': model.provider.name if model.provider_id else None,
    }

    user_message = AiMessage.objects.create(
        conversation=conversation, role='user',
        content=message_text or '（用户仅上传了附件，见下方附件）', meta=model_meta)
    assistant_message = AiMessage.objects.create(
        conversation=conversation, role='assistant', content=reply, meta=model_meta)
    conversation.attachments.filter(message__isnull=True).update(message=user_message)

    conversation.title = _title_for_conversation(conversation, message_text)
    if skill:
        conversation.skill_slug = skill.slug
    conversation.model = model
    conversation.message_count = conversation.messages.count()
    conversation.last_message_at = timezone.now()
    conversation.save()

    return JsonResponse({
        'reply': reply,
        'conversation_id': conversation.id,
        'conversation_title': conversation.title,
        'user_message_id': user_message.id,
        'message_id': assistant_message.id,
        'model_id': model.id,
        'model_name': model.badge(),
        'provider_name': model.provider.name if model.provider_id else None,
        'attachments': attachment_meta,
    })


@login_required
@require_POST
def clear(request):
    """清空对话（兼容旧路由：清空当前 / 指定会话）。"""
    if _require_admin(request):
        return redirect('ai.settings')
    request.session.pop('ai_chat_history', None)

    user = request.user
    conversation = None
    raw_id = request.POST.get('conversation_id')
    if raw_id:
        conversation = AiConversation.objects.filter(user=user, pk=raw_id).first()
    if conversation is None:
        conversation = AiConversation.objects.filter(user=user).order_by('-last_message_at').first()
    if conversation is None:
        messages.success(request, '暂无可清空的会话')
        return redirect('ai.chat')
    return clear_conversation(request, conversation.id)


@login_required
@require_POST
def store_conversation(request):
    """新建会话（空白）。"""
    if _require_admin(request):
        return redirect('ai.settings')
    skill_slug = (request.POST.get('skill') or '').strip() or None
    model = AiModel.usable_by_id(int(request.POST.get('model_id'))) if request.POST.get('model_id') else None
    conversation = AiConversation.objects.create(
        user=request.user, title='新会话', skill_slug=skill_slug,
        model=model, last_message_at=timezone.now(),
    )
    return redirect(reverse('ai.chat') + f'?c={conversation.id}')


@login_required
def rename_conversation(request, pk):
    """重命名会话 / 切换会话使用的模型（AJAX 返回 JSON）。"""
    conversation = get_object_or_404(AiConversation, pk=pk, user=request.user)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'message': '仅支持 POST'}, status=405)

    title = (request.POST.get('title') or '').strip()
    raw_model_id = request.POST.get('model_id')
    patch = {}
    if title:
        if len(title) > 200:
            return JsonResponse({'ok': False, 'message': '会话名称最长 200 字。'}, status=422)
        patch['title'] = title
    if raw_model_id:
        model = AiModel.usable_by_id(int(raw_model_id))
        if not model:
            return JsonResponse({'ok': False, 'message': '该模型不可用，请刷新后重试。'}, status=422)
        patch['model'] = model

    for field, value in patch.items():
        setattr(conversation, field, value)
    if patch:
        conversation.save()

    if _is_ajax(request) or request.content_type == 'application/json':
        return JsonResponse({
            'ok': True,
            'title': conversation.title,
            'model_id': conversation.model_id,
            'model_name': conversation.model.badge() if conversation.model_id else None,
        })
    messages.success(request, '会话名称已更新' if patch.get('title') else '已切换模型')
    return redirect('ai.chat')


@login_required
def destroy_conversation(request, pk):
    """删除会话（含附件物理文件）。"""
    conversation = get_object_or_404(AiConversation, pk=pk, user=request.user)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'message': '仅支持 POST'}, status=405)
    _delete_conversation_files(conversation)
    conversation.hard_delete()
    if _is_ajax(request):
        return JsonResponse({'ok': True})
    messages.success(request, '会话已删除')
    return redirect('ai.chat')


@login_required
@require_POST
def clear_conversation(request, pk):
    """清空某个会话的消息（保留会话本身）。"""
    conversation = get_object_or_404(AiConversation, pk=pk, user=request.user)
    _delete_conversation_files(conversation)
    conversation.messages.all().delete()
    conversation.message_count = 0
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=['message_count', 'last_message_at', 'updated_at'])
    messages.success(request, '当前会话消息已清空')
    return redirect('ai.chat')


@login_required
def export_conversation(request, pk):
    """导出会话为 Markdown。"""
    conversation = get_object_or_404(AiConversation, pk=pk, user=request.user)
    conversation.messages.prefetch_related('attachments').all()

    lines = ['# ' + conversation.title, '',
             '> 导出时间：' + timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'), '']
    for msg in conversation.messages.all():
        who = '👤 我' if msg.role == 'user' else '🤖 AI 助手'
        lines += [f'## {who}', '', msg.content or '']
        names = [a.original_name for a in msg.attachments.all()]
        if names:
            lines += ['', '附件：' + '、'.join(names)]
        lines.append('')

    filename = 'AI会话_' + _safe_filename(conversation.title)[:40] + '.md'

    def _stream():
        yield '\ufeff'  # BOM，保证 Excel/记事本打开不乱码
        yield '\n'.join(lines)

    response = StreamingHttpResponse(_stream(), content_type='text/markdown; charset=utf-8')
    response['Content-Disposition'] = f"attachment; filename*=UTF-8''{_quote_filename(filename)}"
    return response


@login_required
def preview_attachment(request, pk):
    """预览附件（图片内联展示）。"""
    attachment = get_object_or_404(AiAttachment, pk=pk)
    if attachment.user_id != request.user.id:
        return HttpResponseForbidden('无权访问该附件。')
    path = attachment.absolute_path()
    if not os.path.isfile(path):
        return HttpResponse('文件不存在或已被清理', status=404)
    response = FileResponse(open(path, 'rb'),
                            content_type=attachment.mime_type or 'application/octet-stream')
    response['Content-Disposition'] = f"inline; filename*=UTF-8''{_quote_filename(attachment.original_name)}"
    return response


@login_required
def download_attachment(request, pk):
    """下载附件。"""
    attachment = get_object_or_404(AiAttachment, pk=pk)
    if attachment.user_id != request.user.id:
        return HttpResponseForbidden('无权访问该附件。')
    path = attachment.absolute_path()
    if not os.path.isfile(path):
        return HttpResponse('文件不存在或已被清理', status=404)
    response = FileResponse(open(path, 'rb'), as_attachment=True,
                            filename=attachment.original_name)
    response['Content-Disposition'] = f"attachment; filename*=UTF-8''{_quote_filename(attachment.original_name)}"
    return response


# ==================== 设置 ====================

@login_required
def settings(request):
    """AI 设置页（管理员专用：供应商 / API Key / 模型 / 限流 / 启停）。"""
    if not _require_admin(request):
        return HttpResponseForbidden('仅管理员可进行 AI 设置。')
    setting = AiSetting.current()
    AiProvider.ensure_presets()  # 预设供应商未落库时自动补齐（对齐 AiController::settings）

    rows = AiProvider.objects.prefetch_related('models').order_by('id')
    models = (AiModel.objects.select_related('provider')
              .order_by('provider_id', 'sort_order', 'id'))
    rows_list = list(rows)
    return render(request, 'ai/settings.html', {
        'setting': setting,
        'providers': AiSetting.PROVIDERS,
        'provider_info': setting.provider_info(),
        'masked_key': setting.masked_api_key(),
        'rows': rows_list,
        'models': models,
        'default_model': AiService.available_default(),
        # 概览统计（模板里避免重复跑查询）
        'provider_count': len(rows_list),
        'opened_providers': sum(1 for r in rows_list if r.is_usable()),
        'usable_count': sum(1 for m in models if m.is_usable()),
    })


@login_required
@require_POST
def update_settings(request):
    """保存设置（管理员），并同步到多供应商表。"""
    if not _require_admin(request):
        return HttpResponseForbidden('仅管理员可进行 AI 设置。')

    setting = AiSetting.current()
    provider_key = (request.POST.get('provider') or '').strip()
    if provider_key not in AiSetting.PROVIDERS:
        messages.error(request, '未知的 AI 提供商')
        return redirect('ai.settings')

    info = AiSetting.PROVIDERS[provider_key]
    model = (request.POST.get('model') or '').strip()
    provider_models = list(info['models'].keys())
    # 留空或不属于该提供商时，自动取第一个模型
    if not model or (provider_models and model not in provider_models):
        model = provider_models[0] if provider_models else (setting.model or 'deepseek-chat')

    try:
        temperature = float(request.POST.get('temperature') or setting.temperature or 0.7)
        if not (0 <= temperature <= 2):
            raise ValueError
    except (TypeError, ValueError):
        messages.error(request, '温度需为 0~2 的数字')
        return redirect('ai.settings')
    try:
        max_tokens = int(request.POST.get('max_tokens') or setting.max_tokens or 2000)
        if not (100 <= max_tokens <= 8000):
            raise ValueError
    except (TypeError, ValueError):
        messages.error(request, '最大 Token 需为 100~8000 的整数')
        return redirect('ai.settings')
    try:
        rate_minute = max(1, min(1000, int(request.POST.get('rate_limit_minute') or 30)))
        rate_daily = max(1, min(100000, int(request.POST.get('rate_limit_daily') or 200)))
    except (TypeError, ValueError):
        rate_minute, rate_daily = 30, 200

    api_key = (request.POST.get('api_key') or '').strip()
    base_url = (request.POST.get('base_url') or '').strip()
    old_default_url = AiSetting.PROVIDERS.get(setting.provider, {}).get('base_url')
    if not base_url or (old_default_url and setting.base_url == old_default_url):
        base_url = info['base_url']

    if api_key:
        setting.api_key = api_key
    setting.base_url = base_url
    setting.provider = provider_key
    setting.model = model
    setting.temperature = temperature
    setting.max_tokens = max_tokens
    setting.rate_limit_minute = rate_minute
    setting.rate_limit_daily = rate_daily
    setting.enabled = request.POST.get('enabled') == '1'
    setting.vision_enabled = request.POST.get('vision_enabled') == '1'
    vision_model = (request.POST.get('vision_model') or '').strip()
    if vision_model:
        setting.vision_model = vision_model
    setting.save()

    # 同步到多供应商表
    row = AiProvider.objects.filter(key=provider_key).first()
    if row:
        row.base_url = setting.base_url
        if api_key:
            row.api_key = api_key
        if row.has_real_api_key() or api_key:
            row.enabled = bool(setting.enabled)
        row.save()
        if row.enabled and not AiProvider.objects.filter(is_default=True).exists():
            row.mark_as_default()
        if setting.model:
            m = AiModel.objects.filter(provider=row, model_id=setting.model).first()
            if m:
                m.mark_as_default()

    if request.POST.get('source') == 'welcome':
        messages.success(
            request,
            'AI 已开通！请在本页确认供应商、API Key 与调用限流设置，建议点击「测试连接」验证可用性。')
    else:
        messages.success(request, 'AI 设置已保存')
    return redirect('ai.settings')


@login_required
def test_connection(request):
    """测试连接（管理员，全局默认供应商）。"""
    if not _require_admin(request):
        return HttpResponseForbidden('仅管理员可进行 AI 设置。')
    ai = AiService(AiSetting.current())
    result = ai.test_connection()
    if result['ok']:
        messages.success(request, result['message'])
    else:
        messages.error(request, result['message'])
    return redirect('ai.settings')


# ==================== 多供应商管理 ====================

@login_required
@require_POST
def save_provider(request, provider):
    """保存某个供应商的配置（API Key / Base URL / 启停 / 设为默认）。"""
    if not _require_admin(request):
        return HttpResponseForbidden('仅管理员可进行 AI 设置。')
    catalog = AiSetting.PROVIDERS
    if provider not in catalog:
        messages.error(request, '不支持的供应商。')
        return redirect('ai.settings')

    row, _ = AiProvider.objects.get_or_create(
        key=provider, defaults={'name': catalog[provider]['name']})
    row.name = catalog[provider]['name']
    row.base_url = (request.POST.get('base_url') or '').strip() or catalog[provider]['base_url']
    api_key = (request.POST.get('api_key') or '').strip()
    if api_key:  # 留空保持原值
        row.api_key = api_key
    row.enabled = request.POST.get('enabled') == '1'
    row.save()

    if request.POST.get('is_default') == '1':
        row.mark_as_default()
    row.sync_to_legacy_setting()
    messages.success(request, f'{row.name} 配置已保存。')
    return redirect('ai.settings')


@login_required
@require_POST
def toggle_provider(request, provider):
    """启用 / 停用供应商。"""
    if not _require_admin(request):
        return HttpResponseForbidden('仅管理员可进行 AI 设置。')
    row = get_object_or_404(AiProvider, key=provider)
    row.enabled = not row.enabled
    row.save(update_fields=['enabled', 'updated_at'])
    row.sync_to_legacy_setting()
    messages.success(request, f'{row.name} {"已启用" if row.enabled else "已停用"}。')
    return redirect('ai.settings')


@login_required
@require_POST
def set_default_provider(request, provider):
    """设为默认供应商。"""
    if not _require_admin(request):
        return HttpResponseForbidden('仅管理员可进行 AI 设置。')
    row = get_object_or_404(AiProvider, key=provider)
    row.mark_as_default()
    row.sync_to_legacy_setting()
    messages.success(request, '默认供应商已设为：' + row.name)
    return redirect('ai.settings')


@login_required
@require_POST
def remove_provider(request, provider):
    """清空某个供应商的密钥（保留供应商与模型配置）。"""
    if not _require_admin(request):
        return HttpResponseForbidden('仅管理员可进行 AI 设置。')
    row = get_object_or_404(AiProvider, key=provider)
    row.api_key = None
    row.enabled = False
    row.save(update_fields=['api_key', 'enabled', 'updated_at'])
    messages.success(request, f'{row.name} 的密钥已清除并停用。')
    return redirect('ai.settings')


@login_required
@require_POST
def test_provider(request, provider):
    """测试某个供应商（可指定模型）能否连通。"""
    if not _require_admin(request):
        return HttpResponseForbidden('仅管理员可进行 AI 设置。')
    row = get_object_or_404(AiProvider, key=provider)
    ai = AiService(AiSetting.current())
    model = None
    raw_model_id = request.POST.get('model_id')
    if raw_model_id:
        model = AiModel.objects.filter(provider=row, pk=int(raw_model_id)).first()
    result = ai.test_connection_for(row, model)
    if result['ok']:
        messages.success(request, result['message'])
    else:
        messages.error(request, result['message'])
    return redirect('ai.settings')


@login_required
@require_POST
def sync_provider_models(request, provider):
    """把预设模型列表补齐到该供应商。"""
    if not _require_admin(request):
        return HttpResponseForbidden('仅管理员可进行 AI 设置。')
    row = get_object_or_404(AiProvider, key=provider)
    existing = set(row.models.values_list('model_id', flat=True))
    order = row.models.aggregate(max_order=Max('sort_order'))['max_order'] or 0
    added = 0
    for model_id, label in row.preset_models().items():
        if model_id in existing:
            continue
        order += 1
        AiModel.objects.create(provider=row, model_id=model_id, name=label, enabled=True, sort_order=order)
        added += 1
    messages.success(request, f'已补齐 {added} 个模型。' if added else '预设模型已全部存在，无需补齐。')
    return redirect('ai.settings')


# ==================== 多模型管理 ====================

@login_required
@require_POST
def store_model(request):
    """新增自定义模型。"""
    if not _require_admin(request):
        return HttpResponseForbidden('仅管理员可进行 AI 设置。')
    raw_provider_id = request.POST.get('provider_id')
    model_id = (request.POST.get('model_id') or '').strip()
    provider = AiProvider.objects.filter(pk=raw_provider_id).first() if raw_provider_id else None
    if not provider or not model_id:
        messages.error(request, '请填写所属供应商与模型标识。')
        return redirect('ai.settings')
    if AiModel.objects.filter(provider=provider, model_id=model_id).exists():
        messages.error(request, '该供应商下已存在同名模型标识。')
        return redirect('ai.settings')

    name = (request.POST.get('name') or '').strip() or model_id
    model = AiModel.objects.create(
        provider=provider, model_id=model_id, name=name,
        is_vision=request.POST.get('is_vision') == '1',
        enabled=request.POST.get('enabled', '1') == '1',
        sort_order=(provider.models.aggregate(max_order=Max('sort_order'))['max_order'] or 0) + 1,
    )
    if request.POST.get('is_default') == '1':
        model.mark_as_default()
    messages.success(request, '模型已新增：' + model.name)
    return redirect('ai.settings')


@login_required
@require_POST
def update_model(request, pk):
    """修改模型（名称 / 是否支持看图 / 是否开放给用户）。"""
    if not _require_admin(request):
        return HttpResponseForbidden('仅管理员可进行 AI 设置。')
    model = get_object_or_404(AiModel, pk=pk)
    name = (request.POST.get('name') or '').strip()
    if name:
        model.name = name
    model_id = (request.POST.get('model_id') or '').strip()
    if model_id:
        model.model_id = model_id
    model.enabled = request.POST.get('enabled') == '1'
    model.is_vision = request.POST.get('is_vision') == '1'
    model.save()
    if request.POST.get('is_default') == '1':
        model.mark_as_default()
    messages.success(request, '模型已更新：' + (model.name or model.model_id))
    return redirect('ai.settings')


@login_required
@require_POST
def set_default_model(request, pk):
    """设为默认模型。"""
    if not _require_admin(request):
        return HttpResponseForbidden('仅管理员可进行 AI 设置。')
    model = get_object_or_404(AiModel, pk=pk)
    model.mark_as_default()
    if model.provider_id:
        model.provider.sync_to_legacy_setting()
    messages.success(request, '默认模型已设为：' + model.full_label())
    return redirect('ai.settings')


@login_required
@require_POST
def destroy_model(request, pk):
    """删除模型。"""
    if not _require_admin(request):
        return HttpResponseForbidden('仅管理员可进行 AI 设置。')
    model = get_object_or_404(AiModel, pk=pk)
    name = model.name or model.model_id
    model.delete()
    messages.success(request, '模型已删除：' + name)
    return redirect('ai.settings')


# ==================== 自然语言生成文档 ====================

@login_required
def docs(request):
    """文档生成页（列出模板）。"""
    if _require_admin(request):
        messages.warning(
            request,
            '管理员账号仅用于管理 AI 设置，不提供 AI 文档生成功能。')
        return redirect('ai.settings')
    templates = AiDocTemplate.objects.filter(enabled=True).order_by('category', 'id')
    generated = request.session.get('ai_doc_result')
    request.session.pop('ai_doc_result', None)
    return render(request, 'ai/docs.html', {
        'templates': templates,
        'generated': generated,
        'models': AiService.available_models(),
        'default_model': AiService.available_default(),
    })


@login_required
@require_POST
def generate_doc(request):
    """生成文档（服务端在途锁防重复/并发提交，带每用户限流）。"""
    if _require_admin(request):
        messages.warning(
            request,
            '管理员账号仅用于管理 AI 设置，不提供 AI 文档生成功能。')
        return redirect('ai.settings')

    template_id = request.POST.get('template_id')
    description = (request.POST.get('description') or '').strip()
    template = AiDocTemplate.objects.filter(enabled=True, pk=template_id).first()
    if not template:
        messages.error(request, '请选择有效的文档模板')
        return redirect('ai.docs')
    if not description:
        messages.error(request, '请填写自然语言描述')
        return redirect('ai.docs')
    if len(description) > 4000:
        messages.error(request, '描述最长 4000 字')
        return redirect('ai.docs')

    setting = AiSetting.current()
    ai = AiService(setting)
    if not ai.is_available():
        messages.error(request, 'AI 尚未配置，请先在「AI 智能助手」中配置 API Key 并启用。')
        return redirect('ai.docs')

    ok, reason = check_and_consume(request.user, setting)
    if not ok:
        messages.error(request, reason)
        return redirect('ai.docs')

    raw_model_id = request.POST.get('model_id')
    if model := ai.resolve_model(int(raw_model_id) if raw_model_id else None):
        ai.use_model(model)

    with DocGenLock(request.user) as acquired:
        if not acquired:
            messages.error(request, '您有文档正在生成中，请稍候再试。')
            return redirect('ai.docs')
        result = ai.generate_document(int(template.id), description)

    request.session['ai_doc_result'] = {
        'template_id': template.id,
        'template_name': template.name,
        'description': description,
        'content': result,
        'at': timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'),
    }
    messages.success(request, '文档已生成，请在下方面板查看并复制。')
    return redirect('ai.docs')
