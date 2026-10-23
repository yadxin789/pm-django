# -*- coding: utf-8 -*-
"""知识库视图：对齐 Laravel KnowledgeController.php。"""
import markdown
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from core.models import Knowledge, KnowledgeCategory, KnowledgeDoc, User
from core.services.operation_log import log_operation

from .feedback import deny, ok


def _md_render(md):
    return markdown.markdown(md or '', extensions=['fenced_code', 'tables'])


def _visible_qs(uid):
    return Knowledge.objects.filter(Q(is_share=1) | Q(admin_id=uid))


def _item_ctx(k):
    return {
        'author': User.objects.filter(pk=k.admin_id).first(),
        'category': KnowledgeCategory.objects.filter(pk=k.cate_id).first() if k.cate_id else None,
    }


@login_required
def index(request):
    uid = request.user.id
    qs = _visible_qs(uid).filter(is_share=1) if request.GET.get('scope') == 'share' else _visible_qs(uid)
    if request.GET.get('scope') == 'mine':
        qs = _visible_qs(uid).filter(is_share=0, admin_id=uid)
    keyword = request.GET.get('keyword', '').strip()
    if keyword:
        qs = qs.filter(title__icontains=keyword)
    cate_id = request.GET.get('cate_id', '')
    if cate_id:
        qs = qs.filter(cate_id=cate_id)
    qs = qs.annotate(docs_count=Count('docs')).order_by('-sort', '-id')
    knowledges = Paginator(qs, 12).get_page(request.GET.get('page', 1))
    categories = KnowledgeCategory.objects.order_by('sort', 'id')
    items = [dict(_item_ctx(k), **{'k': k, 'docs_count': k.docs_count}) for k in knowledges]
    return render(request, 'knowledge/index.html', {
        'knowledges': knowledges, 'items': items, 'categories': categories,
        'keyword': keyword, 'cate_id': cate_id, 'scope': request.GET.get('scope', ''),
    })


@login_required
def show(request, pk):
    knowledge = get_object_or_404(Knowledge, pk=pk)
    if not knowledge.is_visible_to(request.user.id):
        return deny(request, '无权查看该知识库。', fallback_view='knowledge.index')
    knowledge.read += 1
    knowledge.save(update_fields=['read', 'updated_at'])
    docs = list(KnowledgeDoc.objects.filter(knowledge_id=knowledge.id).order_by('sort', 'id'))
    root_docs = [d for d in docs if d.pid == 0]
    children_map = {}
    for d in docs:
        children_map.setdefault(d.pid, []).append(d)
    author = User.objects.filter(pk=knowledge.admin_id).first()
    category = KnowledgeCategory.objects.filter(pk=knowledge.cate_id).first() if knowledge.cate_id else None
    open_id = request.session.get('openDoc')
    active = next((d for d in docs if d.id == open_id), None)
    return render(request, 'knowledge/show.html', {
        'knowledge': knowledge, 'author': author, 'category': category,
        'docs': docs, 'root_docs': root_docs, 'children_map': children_map,
        'active': active,
        # 模板的 {% if %} 不支持带参方法调用，权限判断在这里预先算好
        'can_edit': knowledge.is_editable_by(request.user.id),
    })


@login_required
def create(request):
    categories = KnowledgeCategory.objects.order_by('sort', 'id')
    return render(request, 'knowledge/form.html', {'knowledge': None, 'categories': categories})


@login_required
def store(request):
    title = request.POST.get('title', '').strip()
    if not title:
        return deny(request, '知识库标题不能为空。', fallback_view='knowledge.create')
    cate_id = request.POST.get('cate_id') or None
    desc = request.POST.get('desc', '')
    md = request.POST.get('md_content', '')
    rendered = _md_render(md)
    import re
    plain = re.sub(r'<[^>]+>', '', rendered).strip()[:100]
    k = Knowledge.objects.create(
        admin_id=request.user.id, cate_id=cate_id, title=title,
        desc=plain or desc, is_share=1 if request.POST.get('is_share') else 0,
        sort=int(request.POST.get('sort') or 0),
    )
    log_operation('知识库', '创建', '创建知识库「%s」' % title, user=request.user)
    ok(request, '知识库「%s」创建成功。' % title)
    return redirect('knowledge.show', k.id)


@login_required
def edit(request, pk):
    knowledge = get_object_or_404(Knowledge, pk=pk)
    if not knowledge.is_editable_by(request.user.id):
        return deny(request, '只有创建人或管理员可编辑该知识库。',
                    fallback_view='knowledge.show', pk=pk)
    categories = KnowledgeCategory.objects.order_by('sort', 'id')
    return render(request, 'knowledge/form.html', {'knowledge': knowledge, 'categories': categories})


@login_required
def update(request, pk):
    knowledge = get_object_or_404(Knowledge, pk=pk)
    if not knowledge.is_editable_by(request.user.id):
        return deny(request, '只有创建人或管理员可编辑该知识库。',
                    fallback_view='knowledge.show', pk=pk)
    knowledge.cate_id = request.POST.get('cate_id') or None
    knowledge.title = request.POST.get('title', '').strip()
    knowledge.desc = request.POST.get('desc', '')
    knowledge.is_share = 1 if request.POST.get('is_share') else 0
    knowledge.sort = int(request.POST.get('sort') or 0)
    knowledge.save()
    log_operation('知识库', '修改', '修改知识库「%s」' % knowledge.title, user=request.user)
    ok(request, '知识库「%s」已更新。' % knowledge.title)
    return redirect('knowledge.show', knowledge.id)


@login_required
def destroy(request, pk):
    knowledge = get_object_or_404(Knowledge, pk=pk)
    if not knowledge.is_editable_by(request.user.id):
        return deny(request, '只有创建人或管理员可删除该知识库。',
                    fallback_view='knowledge.show', pk=pk)
    if knowledge.docs.count() > 0:
        return deny(request, '该知识库下还有文档，请先删除全部文档后再删除知识库。',
                    fallback_view='knowledge.show', pk=pk)
    log_operation('知识库', '删除', '删除知识库「%s」' % knowledge.title, user=request.user)
    from core.services.trash_service import soft_delete
    soft_delete(knowledge, user_id=request.user.id)
    ok(request, '知识库「%s」已移入回收站。' % knowledge.title)
    return redirect('knowledge.index')


@login_required
def doc_store(request, pk):
    knowledge = get_object_or_404(Knowledge, pk=pk)
    if not knowledge.is_editable_by(request.user.id):
        return deny(request, '只有创建人或管理员可新增该知识库的文档。',
                    fallback_view='knowledge.show', pk=pk)
    title = request.POST.get('title', '').strip()
    md = request.POST.get('md_content', '')
    KnowledgeDoc.objects.create(
        knowledge_id=knowledge.id, pid=int(request.POST.get('pid') or 0),
        type=int(request.POST.get('type') or KnowledgeDoc.TYPE_DOC),
        title=title, md_content=md,
        content=_md_render(md) or None,
        link=request.POST.get('link') or None,
        file_ids=request.POST.get('file_ids') or None,
        sort=int(request.POST.get('sort') or 0),
    )
    log_operation('知识库', '新增文档', '「%s」新增文档「%s」' % (knowledge.title, title), user=request.user)
    ok(request, '文档「%s」已新增。' % title)
    return redirect('knowledge.show', knowledge.id)


@login_required
def doc_update(request, pk, doc):
    knowledge = get_object_or_404(Knowledge, pk=pk)
    kdoc = get_object_or_404(KnowledgeDoc, pk=doc)
    if not knowledge.is_editable_by(request.user.id) or kdoc.knowledge_id != knowledge.id:
        return deny(request, '无权修改该文档。', fallback_view='knowledge.show', pk=pk)
    md = request.POST.get('md_content', '')
    kdoc.pid = int(request.POST.get('pid') or 0)
    kdoc.type = int(request.POST.get('type') or KnowledgeDoc.TYPE_DOC)
    kdoc.title = request.POST.get('title', '').strip()
    kdoc.md_content = md
    kdoc.content = _md_render(md) or None
    kdoc.link = request.POST.get('link') or None
    kdoc.file_ids = request.POST.get('file_ids') or None
    kdoc.sort = int(request.POST.get('sort') or 0)
    kdoc.save()
    log_operation('知识库', '修改文档', '「%s」修改文档「%s」' % (knowledge.title, kdoc.title), user=request.user)
    ok(request, '文档「%s」已更新。' % kdoc.title)
    return redirect('knowledge.show', knowledge.id)


@login_required
def doc_destroy(request, pk, doc):
    knowledge = get_object_or_404(Knowledge, pk=pk)
    kdoc = get_object_or_404(KnowledgeDoc, pk=doc)
    if not knowledge.is_editable_by(request.user.id) or kdoc.knowledge_id != knowledge.id:
        return deny(request, '无权删除该文档。', fallback_view='knowledge.show', pk=pk)
    if KnowledgeDoc.objects.filter(pid=kdoc.id).count() > 0:
        return deny(request, '该文档下还有子文档，请先删除子文档。',
                    fallback_view='knowledge.show', pk=pk)
    log_operation('知识库', '删除文档', '「%s」删除文档「%s」' % (knowledge.title, kdoc.title), user=request.user)
    from core.services.trash_service import soft_delete
    soft_delete(kdoc, user_id=request.user.id)
    ok(request, '文档「%s」已移入回收站。' % kdoc.title)
    return redirect('knowledge.show', knowledge.id)


@login_required
def doc_read(request, pk, doc):
    knowledge = get_object_or_404(Knowledge, pk=pk)
    if not knowledge.is_visible_to(request.user.id):
        return deny(request, '无权查看该知识库。', fallback_view='knowledge.index')
    kdoc = get_object_or_404(KnowledgeDoc, pk=doc)
    kdoc.read += 1
    kdoc.save(update_fields=['read', 'updated_at'])
    request.session['openDoc'] = kdoc.id
    return redirect('knowledge.show', knowledge.id)
