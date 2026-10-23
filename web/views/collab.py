# -*- coding: utf-8 -*-
"""协同办公：对齐 Laravel CollabDocumentController.php（OnlyOffice 集成）。"""
import json
import logging
import os
import shutil
import uuid

import requests
from django.conf import settings
from django.contrib import messages as dj_messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt

from core.models import CollabDocument, Project, User
from core.services import collab_office as office

from .feedback import deny, ok

MEDIA = settings.MEDIA_ROOT
logger = logging.getLogger(__name__)

# Document Server 通常部署在内网 http 上，requests 会因 verify=False 刷证书告警，这里关掉
try:
    import urllib3
    from urllib3.exceptions import InsecureRequestWarning

    urllib3.disable_warnings(InsecureRequestWarning)
except Exception:   # pragma: no cover
    pass


def _can_view_project(user, project):
    if user.is_admin() or user.is_project_manager():
        return True
    return project.participants().filter(id=user.id).exists()


def _project_or_403(user, pk):
    project = get_object_or_404(Project, pk=pk)
    if not _can_view_project(user, project):
        raise Http404
    return project


# 文档库共用的两件事：可编辑格式白名单、删除权限判定。
# 电话会议模块（web/views/voice.py）直接复用这里的实现，避免两处逻辑漂移。
DOC_EXTENSIONS = ('docx', 'xlsx', 'pptx')


def can_delete_document(user, project, doc):
    """删除权限：管理员 / 项目创建者 / 文档创建者（协同办公与电话会议完全一致）。"""
    return bool(user.is_admin() or user.id == project.creator_id or user.id == doc.created_by_id)


def create_document(project, user, mode='template', name='', extension='docx', uploaded=None):
    """新建/上传一份项目文档，落在同一个文档库（media/collab/<项目>/）。

    返回 (doc, error)；error 为 None 表示成功。协同办公页与电话会议页共用这一份实现，
    保证「存储目录、可编辑格式、命名、版本起点」两处完全一致。
    """
    name = (name or '').strip()
    if not name and uploaded is not None and getattr(uploaded, 'name', ''):
        name = uploaded.name.rsplit('.', 1)[0].strip()
    if not name:
        return None, '缺少文档名称'
    collab_dir = os.path.join(MEDIA, 'collab', str(project.id))
    os.makedirs(collab_dir, exist_ok=True)
    rel = None
    try:
        with transaction.atomic():
            if mode == 'upload':
                if uploaded is None:
                    return None, '缺少上传文件'
                ext = (uploaded.name.rsplit('.', 1)[-1].lower() if '.' in uploaded.name else '')
                if ext not in DOC_EXTENSIONS:
                    return None, '只支持 docx / xlsx / pptx 三种可协同编辑的格式'
                rel = f'collab/{project.id}/doc_{uuid.uuid4().hex}.{ext}'
                with open(os.path.join(MEDIA, rel), 'wb') as f:
                    for chunk in uploaded.chunks():
                        f.write(chunk)
            else:
                ext = extension if extension in DOC_EXTENSIONS else 'docx'
                template_dir = os.path.join(MEDIA, 'collab_templates')
                office.ensure_templates(template_dir)
                tpl = os.path.join(template_dir, 'template.' + ext)
                if not os.path.exists(tpl):
                    return None, f'缺少 {ext} 模板文件'
                rel = f'collab/{project.id}/doc_{uuid.uuid4().hex}.{ext}'
                with open(os.path.join(MEDIA, rel), 'wb') as f:
                    f.write(open(tpl, 'rb').read())
            doc = CollabDocument.objects.create(
                project_id=project.id, name=name, extension=ext, storage_path=rel,
                doc_key=uuid.uuid4().hex, size=os.path.getsize(os.path.join(MEDIA, rel)), version=1,
                created_by=user, updated_by=user)
    except Exception:
        # 落盘成功但写库失败：立即清掉刚落盘的文件，不留孤儿
        if rel:
            try:
                full = os.path.join(MEDIA, rel)
                if os.path.isfile(full):
                    os.remove(full)
            except OSError:
                pass
        raise
    return doc, None


def delete_document(doc):
    """彻底删除文档正文 + 同步清理协作文档所在目录（历史版本、临时演示文件等）。

    由 TrashService::force_delete 通过 collect_purge_targets 接管，
    本函数保留以兼容旧调用方（如 voice.show 等）。
    """
    from core.services.trash_service import force_delete
    force_delete(doc)


def delete_document_hard(doc):
    """彻底删除协作文档（含历史版本目录）。

    与 delete_document 行为一致，作为 TrashService::purge_targets 的可调用入口。
    """
    from core.services.trash_service import force_delete
    force_delete(doc)


def annotate_permissions(documents, user, project):
    """给每份文档挂上 can_delete 标记，供模板（含与电话会议共用的列表片段）统一判断。

    放在视图里算一次，模板里就不必再散落一份权限表达式 —— 两个模块的列表由此
    共用同一个权限来源，不会再出现「协同办公能删、会议里按钮不出现」之类的偏差。
    """
    for d in documents:
        d.can_delete = can_delete_document(user, project, d)
    return documents


@login_required
def index(request, pk):
    project = _project_or_403(request.user, pk)
    documents = annotate_permissions(
        list(CollabDocument.objects.select_related('created_by', 'updated_by')
             .filter(project_id=project.id).order_by('-updated_at')),
        request.user, project)
    return render(request, 'collab/index.html', {'project': project, 'documents': documents})


@login_required
def create(request, pk):
    project = _project_or_403(request.user, pk)
    return render(request, 'collab/create.html', {'project': project})


@login_required
def store(request, pk):
    project = _project_or_403(request.user, pk)
    doc, error = create_document(project, request.user,
                                 mode=request.POST.get('mode', 'template'),
                                 name=request.POST.get('name'),
                                 extension=request.POST.get('extension', 'docx'),
                                 uploaded=request.FILES.get('file'))
    if error:
        logger.warning('collab store: 新建文档失败（project=%s）：%s', project.id, error)
        return deny(request, '新建文档失败：%s' % error,
                    fallback_view='project.collab.index', pk=project.id)
    ok(request, '文档「%s」创建成功。' % doc.name)
    return redirect('project.collab.open', pk=project.id, doc=doc.id)


# 注意：本函数名绝对不能叫 open，否则会在本模块作用域遮蔽内置 open()，
# 使 store()/download()/callback() 里的文件读写变成调用本视图，
# 症状即 'str' object has no attribute 'user'（request 实际收到的是路径字符串）。
# URL name 仍为 project.collab.open，模板与重定向无需改动。
@login_required
def open_doc(request, pk, doc):
    project = _project_or_403(request.user, pk)
    d = get_object_or_404(CollabDocument, pk=doc)
    if d.project_id != project.id:
        raise Http404
    d.refresh_doc_key()
    d.save(update_fields=['doc_key', 'updated_at'])
    config = office.build_editor_config(d, request.user, True)
    return render(request, 'collab/open.html', {'project': project, 'doc': d, 'config': config, 'server_url': office.server_url()})


def _callback_payload(request):
    """回调参数既可能是 JSON body（DS 默认），也可能是表单，两种都兼容。"""
    if request.POST:
        return request.POST.dict()
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        return {}


def _fetch_remote(url, timeout=(10, 60)):
    """从 DS 取回新版本文件；连接/读取超时分开设置（对齐 Laravel 的 cURL 10s/60s）。"""
    resp = requests.get(url, timeout=timeout, verify=False)
    resp.raise_for_status()
    return resp.content


def _same_as_disk(path, content):
    """内容完全一样则视为重复回调（DS 会把同一次保存的 status=2/6 多次送达）。"""
    if not os.path.exists(path) or os.path.getsize(path) != len(content):
        return False
    with open(path, 'rb') as f:
        return f.read() == content


MAX_COLLAB_HISTORY = 2


def _prune_collab_history(d) -> None:
    """每个协作文档最多保留 MAX_COLLAB_HISTORY（2）个历史版本快照。

    与 Laravel `CollabDocumentController::pruneHistory` 行为一致：
    命名规则 collab/{projectId}/history/{docId}_v{n}.{ext}，
    版本号越大越新；超出部分仅清本文件的快照，不影响同项目其他文档。
    """
    history_dir = os.path.join(MEDIA, 'collab', str(d.project_id), 'history')
    if not os.path.isdir(history_dir):
        return
    prefix = f'{d.id}_v'
    snapshots: list[tuple[int, str]] = []
    for name in os.listdir(history_dir):
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix):]
        ver_str = rest.split('.', 1)[0]
        try:
            snapshots.append((int(ver_str), os.path.join(history_dir, name)))
        except ValueError:
            continue
    if len(snapshots) <= MAX_COLLAB_HISTORY:
        return
    snapshots.sort(reverse=True)
    for _, path in snapshots[MAX_COLLAB_HISTORY:]:
        try:
            os.remove(path)
        except OSError as exc:
            logger.warning('prune collab history failed: %s | %s', path, exc)


def download(request, doc):
    d = get_object_or_404(CollabDocument, pk=doc)
    if request.GET.get('k') != d.doc_key:
        return JsonResponse({'error': 1, 'message': 'key 不匹配'}, status=403)
    path = os.path.join(MEDIA, d.storage_path)
    if not os.path.exists(path):
        raise Http404
    response = FileResponse(open(path, 'rb'), as_attachment=True, filename=f'{d.name}.{d.extension}')
    return response


# Document Server 是服务器对服务器直接 POST 这个地址的：没有浏览器、不会带 CSRF token，
# 因此必须豁免 CSRF —— 否则 CsrfViewMiddleware 一律返回 403，文档永远保存不回来。
@csrf_exempt
def callback(request, doc):
    d = get_object_or_404(CollabDocument, pk=doc)
    payload = _callback_payload(request)
    if payload.get('key') != d.doc_key:
        logger.warning('collab callback: doc_key 不匹配 doc=%s key=%r', doc, payload.get('key'))
        return JsonResponse({'error': 1, 'message': 'key 不匹配'})
    status = int(payload.get('status', 0) or 0)
    if status not in (2, 6):
        # 1=编辑中、3=保存出错、4=关闭且无修改、7=强制保存出错：都不需要落盘
        logger.info('collab callback: doc=%s status=%s 无需落盘', doc, status)
        return JsonResponse({'error': 0})
    url = str(payload.get('url') or '')
    if not url.startswith(('http://', 'https://')):
        logger.error('collab callback: doc=%s status=%s 缺少可下载的 url：%r', doc, status, url)
        return JsonResponse({'error': 1, 'message': '缺少可下载的 url'})
    try:
        content = _fetch_remote(url)
    except Exception as exc:
        logger.exception('collab callback: doc=%s 取回新文件失败 url=%s', doc, url)
        return JsonResponse({'error': 1, 'message': f'取回新文件失败: {exc}'})

    src = os.path.join(MEDIA, d.storage_path)
    if _same_as_disk(src, content):
        logger.info('collab callback: doc=%s status=%s 内容与磁盘一致，跳过（重复回调）', doc, status)
        return JsonResponse({'error': 0})

    # 覆盖前先把当前版本留档，便于回溯；同时裁剪超出保留数量的旧快照
    if os.path.exists(src):
        history_dir = os.path.join(MEDIA, 'collab', str(d.project_id), 'history')
        os.makedirs(history_dir, exist_ok=True)
        history_path = os.path.join(history_dir, f'{d.id}_v{d.version}.{d.extension}')
        try:
            shutil.copy2(src, history_path)
        except OSError as exc:
            logger.warning('collab callback: doc=%s 历史版本备份失败：%s', doc, exc)
        _prune_collab_history(d)

    # 先写临时文件再原子替换，避免写一半被 DS 读到残缺文件
    tmp = src + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(content)
    os.replace(tmp, src)

    d.size = os.path.getsize(src)
    d.version = d.version + 1
    d.save(update_fields=['size', 'version', 'updated_at'])
    logger.info('collab callback: doc=%s status=%s 已回存 %s 字节，版本 → v%s',
                doc, status, d.size, d.version)
    return JsonResponse({'error': 0})


@login_required
def destroy(request, pk, doc):
    """软删除协作文档（走 TrashService）。

    只标记 deleted_at 与 deleted_by，保留物理文件，
    恢复走个人回收站或管理员统一回收站；彻底删除由 TrashService 同步清理目录。
    """
    project = get_object_or_404(Project, pk=pk)
    d = get_object_or_404(CollabDocument, pk=doc)
    if d.project_id != project.id:
        raise Http404
    if not can_delete_document(request.user, project, d):
        raise Http404
    from core.services.trash_service import soft_delete
    soft_delete(d, user_id=request.user.id)
    dj_messages.success(request, '文档已移入回收站，可随时在回收站中恢复')
    return redirect('project.collab.index', pk=project.id)
