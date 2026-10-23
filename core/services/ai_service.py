# -*- coding: utf-8 -*-
"""AI 服务：桥接「DB 中的 AI 配置」与「OpenAI 兼容 Chat Completions」。

对齐参考目录 app/Services/AiService.php（原底层为 laravel/ai SDK）。
本实现直接用 requests 调用各家兼容的 /chat/completions 接口，并手工完成
Function Calling 循环：模型要求调用工具 -> 本地执行 -> 把结果回灌 -> 继续作答。

支持提供商（均为 OpenAI 兼容协议）：deepseek / qwen / doubao / hunyuan。
"""
import os
import logging
from pathlib import Path

import requests
from django.conf import settings

from core.models import (
    AiDocTemplate, AiSetting, Department, File, Knowledge, KnowledgeDoc,
    Project, Task, Todo, User,
)

logger = logging.getLogger('pm')

# 状态机类名 -> 中文标签（对齐 app/Ai/Concerns/PmFormats.php）
PROJECT_STATE_LABELS = {
    'NotStarted': '未开始',
    'InProgress': '进行中',
    'Overdue': '已逾期',
    'Completed': '已完成',
    'OverdueCompleted': '逾期完成',
    'Suspended': '已暂停',
}
TASK_STATE_LABELS = {
    'Pending': '待开始',
    'InProgress': '进行中',
    'ForAudit': '待审核',
    'Completed': '已完成',
    'Overdue': '已逾期',
    'OverdueCompleted': '逾期完成',
    'Rejected': '已驳回',
}

_P = 'App\\States\\Project\\'
_T = 'App\\States\\Task\\'

P_INPROGRESS = _P + 'InProgress'
P_OVERDUE = _P + 'Overdue'
P_COMPLETED = _P + 'Completed'
P_SUSPENDED = _P + 'Suspended'
P_NOTSTARTED = _P + 'NotStarted'

T_PENDING = _T + 'Pending'
T_INPROGRESS = _T + 'InProgress'
T_FORAUDIT = _T + 'ForAudit'
T_OVERDUE = _T + 'Overdue'
T_COMPLETED = _T + 'Completed'
T_OVERDUE_COMPLETED = _T + 'OverdueCompleted'

# AI 文档资源根（内置随项目分发，对齐参考 storage/app/ai_docs）
AI_DOCS_ROOT = Path(settings.BASE_DIR) / 'core' / 'ai_docs'


# ---------------------------------------------------------------------------
# 状态标签与工具取数
# ---------------------------------------------------------------------------

def project_state_label(status):
    key = (status or '').split('\\')[-1]
    return PROJECT_STATE_LABELS.get(key, status or '')


def task_state_label(status):
    key = (status or '').split('\\')[-1]
    return TASK_STATE_LABELS.get(key, status or '')


def _fmt_size(size):
    try:
        size = float(size or 0)
    except (TypeError, ValueError):
        return '0B'
    if size >= 1024 * 1024:
        return f'{size / 1024 / 1024:.1f}MB'
    if size >= 1024:
        return f'{size / 1024:.1f}KB'
    return f'{int(size)}B'


def _read_doc_file(rel_path):
    """读取 ai_docs 资源文件；rel_path 容忍带/不带 ai_docs/ 前缀。"""
    if not rel_path:
        return None
    p = str(rel_path).replace('ai_docs/', '').lstrip('/')
    full = (AI_DOCS_ROOT / p).resolve()
    try:
        full.relative_to(AI_DOCS_ROOT.resolve())
    except ValueError:
        return None
    if full.is_file():
        return full.read_text(encoding='utf-8', errors='replace')
    return None


# ---------------------------------------------------------------------------
# 工具实现（对齐 app/Ai/Tools/*.php）
# ---------------------------------------------------------------------------

def tool_pm_overview(args):
    p_total = Project.objects.count()
    p_inprog = Project.objects.filter(status=P_INPROGRESS).count()
    p_overdue = Project.objects.filter(status=P_OVERDUE).count()
    p_done = Project.objects.filter(status=P_COMPLETED).count()
    p_susp = Project.objects.filter(status=P_SUSPENDED).count()

    t_total = Task.objects.count()
    t_pending = Task.objects.filter(status=T_PENDING).count()
    t_inprog = Task.objects.filter(status=T_INPROGRESS).count()
    t_audit = Task.objects.filter(status=T_FORAUDIT).count()
    t_overdue = Task.objects.filter(status=T_OVERDUE).count()
    t_done = Task.objects.filter(status__in=[T_COMPLETED, T_OVERDUE_COMPLETED]).count()

    return (
        'PM 系统概览：\n'
        f'- 项目：共 {p_total} 个（进行中 {p_inprog}，逾期 {p_overdue}，已完成 {p_done}，已暂停 {p_susp}）\n'
        f'- 任务：共 {t_total} 个（待开始 {t_pending}，进行中 {t_inprog}，待审核 {t_audit}，'
        f'已逾期 {t_overdue}，已完成 {t_done}）\n'
        f'- 用户：{User.objects.count()} 人，部门：{Department.objects.count()} 个'
    )


def tool_query_projects(args):
    keyword = args.get('keyword')
    status = args.get('status')
    limit = max(1, int(args.get('limit') or 10))

    from django.db.models import Q
    qs = Project.objects.select_related('creator').all()
    if keyword:
        qs = qs.filter(
            Q(name__icontains=keyword) | Q(code__icontains=keyword)
            | Q(product_name__icontains=keyword)
        )
    status_map = {
        'in_progress': P_INPROGRESS, 'overdue': P_OVERDUE,
        'completed': P_COMPLETED, 'suspended': P_SUSPENDED,
        'not_started': P_NOTSTARTED,
    }
    if status:
        qs = qs.filter(status=status_map.get(status, status))

    projects = list(qs[:limit])
    if not projects:
        return '未找到符合条件的项目。'

    out = f'项目列表（{len(projects)} 条）：\n'
    for p in projects:
        creator = p.creator.name if p.creator_id and p.creator else '—'
        sd = p.start_date.isoformat() if p.start_date else '—'
        ed = p.end_date.isoformat() if p.end_date else '—'
        out += (f"- [{p.code}] {p.name}（{p.product_name} {p.product_model}，版本 v{p.version}，"
                f"进度 {p.progress}%，发起人 {creator}，状态 {project_state_label(p.status)}，{sd} ~ {ed}）\n")
    return out.rstrip()


def tool_query_project_detail(args):
    pid = int(args.get('project_id') or 0)
    project = Project.objects.select_related('creator').filter(pk=pid).first()
    if not project:
        return f'未找到项目 ID {pid}。'

    sd = project.start_date.isoformat() if project.start_date else '—'
    ed = project.end_date.isoformat() if project.end_date else '—'
    out = (
        '项目详情：\n'
        f"- 名称：{project.name}（代码 {project.code}，版本 v{project.version}）\n"
        f"- 产品：{project.product_name} {project.product_model}\n"
        f"- 发起人：{project.creator.name if project.creator_id and project.creator else '—'}，"
        f"进度 {project.progress}%，状态 {project_state_label(project.status)}\n"
        f"- 周期：{sd} ~ {ed}"
    )
    tasks = list(project.tasks.select_related('executor', 'auditor', 'department').all())
    if tasks:
        out += f'\n\n任务清单（{len(tasks)} 个）：'
        for t in tasks:
            tsd = t.start_date.isoformat() if t.start_date else '—'
            ted = t.end_date.isoformat() if t.end_date else '—'
            out += (f"\n- {t.name}｜{t.department.name if t.department_id and t.department else '—'}"
                    f"｜执行 {t.executor.name if t.executor_id and t.executor else '—'}"
                    f"｜审核 {t.auditor.name if t.auditor_id and t.auditor else '—'}"
                    f"｜{tsd}~{ted}｜{task_state_label(t.status)}")
    return out


def tool_query_tasks(args):
    keyword = args.get('keyword')
    status = args.get('status')
    executor = args.get('executor')
    limit = max(1, int(args.get('limit') or 10))

    qs = Task.objects.select_related('project', 'executor', 'auditor').all()
    if keyword:
        qs = qs.filter(name__icontains=keyword)
    status_map = {
        'pending': T_PENDING, 'in_progress': T_INPROGRESS,
        'for_audit': T_FORAUDIT, 'overdue': T_OVERDUE, 'completed': T_COMPLETED,
    }
    if status:
        qs = qs.filter(status=status_map.get(status, status))
    if executor:
        qs = qs.filter(executor__name__icontains=executor)

    tasks = list(qs[:limit])
    if not tasks:
        return '未找到符合条件的任务。'

    out = f'任务列表（{len(tasks)} 条）：\n'
    for t in tasks:
        tsd = t.start_date.isoformat() if t.start_date else '—'
        ted = t.end_date.isoformat() if t.end_date else '—'
        out += (f"- {t.name}｜项目 {t.project.name if t.project_id and t.project else '—'}"
                f"｜执行人 {t.executor.name if t.executor_id and t.executor else '—'}"
                f"｜{tsd}~{ted}｜{task_state_label(t.status)}\n")
    return out.rstrip()


def tool_query_users(args):
    keyword = args.get('keyword')
    limit = max(1, int(args.get('limit') or 20))
    from django.db.models import Q
    qs = User.objects.select_related('role', 'department').filter(is_active=True)
    if keyword:
        qs = qs.filter(Q(name__icontains=keyword) | Q(username__icontains=keyword))
    users = list(qs[:limit])
    if not users:
        return '未找到用户。'
    out = f'用户列表（{len(users)} 条）：\n'
    for u in users:
        out += (f"- {u.name}（@{u.username}）｜{u.role.name if u.role_id and u.role else '—'}"
                f"｜{u.department.name if u.department_id and u.department else '—'}\n")
    return out.rstrip()


def tool_query_departments(args):
    depts = list(Department.objects.order_by('sort_order', 'id'))
    if not depts:
        return '暂无部门。'
    out = f'部门列表（{len(depts)} 个）：\n'
    for d in depts:
        out += f'- {d.name}（{d.users.count()} 人）\n'
    return out.rstrip()


def tool_query_knowledge(args):
    keyword = args.get('keyword')
    limit = max(1, int(args.get('limit') or 5))
    from django.db.models import Q
    qs = Knowledge.objects.all()
    if keyword:
        qs = qs.filter(
            Q(title__icontains=keyword) | Q(desc__icontains=keyword)
            | Q(docs__title__icontains=keyword)
        )
    items = list(qs.distinct()[:limit])
    if not items:
        return '知识库中未找到相关内容。'
    out = f'知识库命中（{len(items)} 条）：\n'
    for k in items:
        cat = KnowledgeCategory_name(k.cate_id)
        author = User.objects.filter(pk=k.admin_id).first()
        out += f'- [{cat}] {k.title}（作者 {author.name if author else "—"}）\n'
        for doc in list(k.docs.filter(type=KnowledgeDoc.TYPE_DOC))[:3]:
            body = (doc.md_content or doc.content or '').strip()
            snippet = body[:80]
            out += f'    · {doc.title}：{snippet or "（无正文）"}\n'
    return out.rstrip()


def KnowledgeCategory_name(cate_id):
    from core.models import KnowledgeCategory
    cat = KnowledgeCategory.objects.filter(pk=cate_id).first()
    return cat.title if cat else '未分类'


def tool_query_files(args):
    keyword = args.get('keyword')
    status = args.get('status')
    limit = max(1, int(args.get('limit') or 10))
    from django.db.models import Q
    qs = File.objects.select_related('task', 'task__project', 'uploader').all()
    if keyword:
        qs = qs.filter(Q(name__icontains=keyword) | Q(original_name__icontains=keyword))
    if status:
        qs = qs.filter(status=status)
    files = list(qs[:limit])
    if not files:
        return '未找到符合条件的文件。'
    out = f'文件列表（{len(files)} 条）：\n'
    for f in files:
        out += (f"- {f.original_name}（v{f.version}，{_fmt_size(f.size)}）"
                f"｜任务 {f.task.name if f.task_id and f.task else '—'}"
                f"｜项目 {f.task.project.name if f.task_id and f.task.project_id and f.task.project else '—'}"
                f"｜上传 {f.uploader.name if f.uploader_id and f.uploader else '—'}"
                f"｜状态 {f.status}\n")
    return out.rstrip()


def tool_query_todos(args):
    completed = args.get('completed')
    limit = max(1, int(args.get('limit') or 20))
    qs = Todo.objects.select_related('user').all()
    if completed in (True, 'true', 'completed'):
        qs = qs.filter(status='completed')
    elif completed in (False, 'false', 'pending'):
        qs = qs.exclude(status='completed')
    todos = list(qs[:limit])
    if not todos:
        return '暂无待办事项。'
    out = f'待办事项（{len(todos)} 条）：\n'
    for t in todos:
        mark = '已完成' if t.status == 'completed' else '待办'
        out += f"- [{mark}] {t.title}（{t.user.name if t.user_id and t.user else '—'}）\n"
    return out.rstrip()


def tool_get_doc_template(args):
    tid = int(args.get('template_id') or 0)
    tpl = AiDocTemplate.objects.filter(enabled=True, pk=tid).first()
    if not tpl:
        return f'未找到文档模板 ID {tid}（或已停用）。'
    out = f'文档模板：{tpl.name}（分类：{tpl.category}）\n'
    out += f'用途说明：{tpl.description or ""}\n\n'
    if tpl.prompt_guide:
        out += f'【生成指引】\n{tpl.prompt_guide}\n\n'
    body = _read_doc_file(tpl.template_path)
    if body:
        out += f'【模板文件内容】\n{body}\n\n'
    else:
        out += '【模板文件】无（请依据用途说明与指引自行组织结构）\n\n'
    example = _read_doc_file(tpl.example_path)
    if example:
        out += f'【范例文件内容】\n{example}\n\n'
    for sp in (tpl.support_files or []):
        content = _read_doc_file(sp)
        if content:
            out += f'【支援文件：{os.path.basename(sp)}】\n{content}\n\n'
    return out.rstrip()


def tool_search_knowledge_base(args):
    keyword = (args.get('keyword') or '').strip()
    limit = max(1, int(args.get('limit') or 3))
    if not keyword:
        return '请提供检索关键词。'
    from django.db.models import Q
    docs = KnowledgeDoc.objects.filter(type=KnowledgeDoc.TYPE_DOC).select_related('knowledge')
    docs = docs.filter(
        Q(title__icontains=keyword) | Q(md_content__icontains=keyword)
        | Q(content__icontains=keyword) | Q(knowledge__title__icontains=keyword)
    )
    docs = list(docs.distinct()[:limit])
    if not docs:
        return f'知识库中未检索到与「{keyword}」相关的资料。'
    out = f'知识库参考资料（{len(docs)} 条）：\n'
    for d in docs:
        title = d.knowledge.title if d.knowledge_id and d.knowledge else '—'
        body = (d.md_content or d.content or '').strip()
        out += f'【{title} - {d.title}】\n{body[:1500]}\n'
    return out.rstrip()


# 工具注册表：OpenAI function-calling schema + 本地 handler
PM_TOOLS = [
    {
        'name': 'query_pm_overview',
        'description': '获取 PM 系统经营概览：项目总数、任务总数、进行中/逾期/待审核/已逾期任务数、用户数、部门数等统计。',
        'parameters': {'type': 'object', 'properties': {}},
        'handler': tool_pm_overview,
    },
    {
        'name': 'query_projects',
        'description': '查询项目列表，可按名称关键词、状态筛选。返回项目名称、代码、版本、产品、进度、起止日期、状态。',
        'parameters': {
            'type': 'object',
            'properties': {
                'keyword': {'type': 'string', 'description': '项目名称/代码/产品名关键词'},
                'status': {'type': 'string', 'description': 'in_progress / overdue / completed / suspended / not_started'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
            },
        },
        'handler': tool_query_projects,
    },
    {
        'name': 'query_project_detail',
        'description': '查询单个项目详情，包括项目信息与全部任务清单。参数为项目ID。',
        'parameters': {
            'type': 'object',
            'properties': {'project_id': {'type': 'integer'}},
            'required': ['project_id'],
        },
        'handler': tool_query_project_detail,
    },
    {
        'name': 'query_tasks',
        'description': '查询任务列表，可按任务名称关键词、状态筛选（pending/in_progress/for_audit/overdue/completed）、执行人筛选。',
        'parameters': {
            'type': 'object',
            'properties': {
                'keyword': {'type': 'string'},
                'status': {'type': 'string', 'description': 'pending / in_progress / for_audit / overdue / completed'},
                'executor': {'type': 'string', 'description': '执行人姓名关键词'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
            },
        },
        'handler': tool_query_tasks,
    },
    {
        'name': 'query_users',
        'description': '查询系统用户列表，可按姓名/用户名关键词筛选。返回姓名、用户名、角色、部门。',
        'parameters': {
            'type': 'object',
            'properties': {
                'keyword': {'type': 'string'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
            },
        },
        'handler': tool_query_users,
    },
    {
        'name': 'query_departments',
        'description': '查询部门列表及其成员数量。',
        'parameters': {'type': 'object', 'properties': {}},
        'handler': tool_query_departments,
    },
    {
        'name': 'query_knowledge',
        'description': '搜索知识库，可按标题关键词查找知识条目及其文档内容。用于回答制度、流程、规范类问题。',
        'parameters': {
            'type': 'object',
            'properties': {
                'keyword': {'type': 'string'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
            },
            'required': ['keyword'],
        },
        'handler': tool_query_knowledge,
    },
    {
        'name': 'query_files',
        'description': '查询已交付/已上传的任务文件列表，可按文件名关键词、审核状态筛选。',
        'parameters': {
            'type': 'object',
            'properties': {
                'keyword': {'type': 'string'},
                'status': {'type': 'string', 'description': 'approved / pending / rejected'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
            },
        },
        'handler': tool_query_files,
    },
    {
        'name': 'query_todos',
        'description': '查询待办事项列表，可按是否完成筛选。',
        'parameters': {
            'type': 'object',
            'properties': {
                'completed': {'type': 'boolean'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
            },
        },
        'handler': tool_query_todos,
    },
]

DOC_TOOLS = [
    {
        'name': 'get_doc_template',
        'description': '按文档类型模板ID读取用于生成文档所需的模板文件、范例文件、支援文件及生成指引。生成任何文档前必须先调用本工具获取模板内容。',
        'parameters': {
            'type': 'object',
            'properties': {'template_id': {'type': 'integer'}},
            'required': ['template_id'],
        },
        'handler': tool_get_doc_template,
    },
    {
        'name': 'search_knowledge_base',
        'description': '从 PM 知识库检索制度、流程、规范等参考资料全文片段，用于生成文档时引用正确口径。',
        'parameters': {
            'type': 'object',
            'properties': {
                'keyword': {'type': 'string'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10},
            },
            'required': ['keyword'],
        },
        'handler': tool_search_knowledge_base,
    },
    *PM_TOOLS[1:4],  # query_projects / query_project_detail / query_tasks
    PM_TOOLS[6],     # query_knowledge
    PM_TOOLS[4],     # query_users
]

_PM_INSTRUCTIONS = (
    '你是 PM 产品研发管理系统的智能助手。你可以调用工具查询项目、任务、用户、部门、'
    '知识库、文件、待办等真实数据。回答用户问题时请用简洁的中文。如果问题需要数据，'
    '先调用对应工具，再基于工具返回的结果作答，不要编造数据。'
    '所有数字、项目名、任务名必须来自工具返回结果。'
)

_DOC_INSTRUCTIONS = (
    '你是 PM 系统的文档生成专家。用户会用自然语言描述文档所需的数据、参数和内容，你需要：\n'
    '1) 首先调用 get_doc_template 工具，传入 template_id 读取「模板文件 / 范例文件 / 支援文件 / 生成指引」。\n'
    '2) 如果模板或指引要求引用制度、流程、规范，调用 search_knowledge_base 检索知识库原文以保证口径正确。\n'
    '3) 如文档需要项目/任务等真实数据，可调用 query_projects、query_project_detail、query_tasks、'
    'query_knowledge 等工具获取。\n'
    '4) 严格按照模板的结构与占位符，把用户自然语言提供的数据/参数填入模板，生成完整、可直接交付的文档（用 Markdown 输出）。\n'
    '5) 模板中未提供数据的占位符，若用户未提供，应保留占位并标注【待补充】而不是编造。'
)


# ---------------------------------------------------------------------------
# AI 服务主体
# ---------------------------------------------------------------------------

class AiService:
    def __init__(self, setting=None):
        self.config = setting or AiSetting.current()
        self._active_model = None

    def is_available(self):
        return self.config.is_available()

    # ------------------------------------------------------------------
    # 多供应商 / 多模型（对齐 AiService::availableModels / resolveModel / useModel）
    # ------------------------------------------------------------------

    @staticmethod
    def available_models():
        """用户可切换的模型列表（模型启用 + 供应商启用且有真实 Key）。"""
        from core.models import AiModel
        return AiModel.usable()

    @staticmethod
    def available_default():
        from core.models import AiModel
        return AiModel.usable_default()

    @staticmethod
    def resolve_model(model_id=None):
        """取本次会话使用的模型：指定 id > 默认可用模型。

        对齐 AiService::resolveModel：找不到可用模型时返回 None（由调用方转 422）。
        """
        from core.models import AiModel
        if model_id:
            matched = AiModel.usable_by_id(int(model_id))
            if matched:
                return matched
        return AiModel.usable_default()

    def use_model(self, model):
        """指定本轮使用的模型（后续请求走该模型所属供应商的 Key / Base URL）。"""
        self._active_model = model
        return model

    def active_model(self):
        return self._active_model

    def vision_enabled(self):
        """是否开启视觉识别（明确配置了视觉模型即视为开启）。"""
        model = self._active_model
        if model is not None and model.is_vision:
            return True
        return bool(self.config.vision_enabled and self.config.vision_model)

    def vision_model_key(self):
        model = self._active_model
        if model is not None and model.is_vision:
            return model.model_id
        return self.config.vision_model or self._model_id()

    @staticmethod
    def tools_for_names(names):
        """按技能配置的工具名取工具子集（对齐 AiService::toolsForNames）。"""
        if not names:
            return None
        wanted = set(names)
        picked = [t for t in (PM_TOOLS + DOC_TOOLS) if t['name'] in wanted]
        return picked or None

    @staticmethod
    def tool_names():
        return [t['name'] for t in (PM_TOOLS + DOC_TOOLS)]

    # ------------------------------------------------------------------
    # 请求参数（模型激活时优先走模型所属供应商）
    # ------------------------------------------------------------------

    def _model_id(self):
        if self._active_model is not None:
            return self._active_model.model_id
        return self.config.model or None

    def _base_url(self):
        if self._active_model is not None:
            provider = self._active_model.provider
            if provider and provider.base_url:
                return provider.base_url
        return self.config.base_url or 'https://api.deepseek.com/v1'

    def _api_key(self):
        if self._active_model is not None:
            provider = self._active_model.provider
            if provider and provider.api_key:
                return provider.api_key
        return self.config.api_key

    @staticmethod
    def pm_instructions():
        return _PM_INSTRUCTIONS

    @staticmethod
    def doc_instructions():
        return _DOC_INSTRUCTIONS

    def _endpoint(self):
        return self._base_url().rstrip('/') + '/chat/completions'

    def _headers(self):
        return {
            'Authorization': f'Bearer {self._api_key()}',
            'Content-Type': 'application/json',
        }

    @staticmethod
    def _tool_openai_schema(tool):
        return {
            'type': 'function',
            'function': {
                'name': tool['name'],
                'description': tool['description'],
                'parameters': tool['parameters'],
            },
        }

    def _execute_tool(self, name, arguments):
        for tool in PM_TOOLS + DOC_TOOLS:
            if tool['name'] == name:
                try:
                    return tool['handler'](arguments or {})
                except Exception as e:
                    logger.exception('AI 工具 %s 执行失败', name)
                    return f'工具执行出错：{e}'
        return f'未知工具：{name}'

    def _complete(self, messages, tools=None, timeout=120, max_rounds=6):
        """OpenAI 兼容 function-calling 循环，返回最终文本。"""
        payload = {
            'model': self._model_id(),
            'messages': messages,
            'temperature': float(self.config.temperature or 0.7),
            'max_tokens': int(self.config.max_tokens or 2000),
        }
        if tools:
            payload['tools'] = [self._tool_openai_schema(t) for t in tools]
            payload['tool_choice'] = 'auto'

        for _ in range(max_rounds):
            resp = requests.post(
                self._endpoint(), headers=self._headers(), json=payload, timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            message = data['choices'][0]['message']
            tool_calls = message.get('tool_calls') or []
            if not tool_calls:
                return message.get('content') or ''

            # 把助手的工具调用消息回灌
            payload['messages'].append({
                'role': 'assistant',
                'content': message.get('content'),
                'tool_calls': tool_calls,
            })
            for call in tool_calls:
                fn = call.get('function') or {}
                raw_args = fn.get('arguments') or '{}'
                try:
                    import json
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except (ValueError, TypeError):
                    arguments = {}
                result = self._execute_tool(fn.get('name'), arguments)
                payload['messages'].append({
                    'role': 'tool',
                    'tool_call_id': call.get('id'),
                    'name': fn.get('name'),
                    'content': result,
                })
        return '抱歉，工具调用轮次过多，未能得出结果。'

    def chat(self, user_message, system_prompt=None, history=None):
        """纯文本对话（无工具）。"""
        if not self.is_available():
            logger.warning('AI 未配置或未启用 provider=%s', self.config.provider)
            return None
        messages = [{'role': 'system', 'content': system_prompt or '你是一个智能助手。'}]
        messages += (history or [])
        messages.append({'role': 'user', 'content': user_message})
        try:
            return self._complete(messages, tools=None, timeout=120)
        except Exception as e:
            logger.exception('AI chat 异常')
            return None

    def chat_with_tools(self, user_message, history=None, tools=None, extra_instructions=None):
        """带工具调用的对话（Function Calling）。

        tools=None 表示不限工具（用 PM 全集）；extra_instructions 为技能提供的专项指令。
        """
        if not self.is_available():
            return ('⚠️ AI 助手尚未配置。请前往 侧边栏 → AI 智能助手 → 开通引导，'
                    '配置 API Key 并启用。')
        system = _PM_INSTRUCTIONS
        if extra_instructions:
            system = f'{_PM_INSTRUCTIONS}\n\n【本次任务专项要求】\n{extra_instructions}'
        messages = [{'role': 'system', 'content': system}]
        messages += (history or [])
        messages.append({'role': 'user', 'content': user_message})
        try:
            return self._complete(messages, tools=tools or PM_TOOLS, timeout=120)
        except Exception as e:
            logger.exception('AI chat_with_tools 异常')
            return f'AI 服务异常：{e}'

    def generate_document(self, template_id, user_description):
        """自然语言生成文档（任务5 核心）。"""
        if not self.is_available():
            return '⚠️ AI 尚未配置。请先在 AI 智能助手中配置 API Key 并启用。'
        messages = [{'role': 'system', 'content': _DOC_INSTRUCTIONS}]
        prompt = (
            f'请使用模板ID {template_id} 生成文档。\n'
            f'用户自然语言描述如下：\n{user_description}\n\n'
            '请先调用 get_doc_template 获取模板，再按指引生成完整文档（Markdown）。'
        )
        messages.append({'role': 'user', 'content': prompt})
        try:
            return self._complete(messages, tools=DOC_TOOLS, timeout=180)
        except Exception as e:
            logger.exception('AI generateDocument 异常')
            return f'文档生成失败：{e}'

    def test_connection(self):
        if not self.config.api_key or str(self.config.api_key).startswith('sk-xxxx'):
            return {'ok': False, 'message': '请先填写 API Key'}
        result = self.chat('你好，请只回复四个字：连接成功。')
        if result and '连接成功' in result:
            return {'ok': True, 'message': '连接成功！AI 回复：' + result[:60]}
        return {
            'ok': False,
            'message': '连接失败，请检查 API Key、Base URL 和网络。'
                       + (f'（AI回复：{result[:60]}）' if result else ''),
        }

    def test_connection_for(self, provider, model=None):
        """测试指定供应商（可指定模型）是否可连通（对齐 AiService::testConnectionFor）。

        临时借用该供应商的 Key / Base URL 发一条最简消息，不改变 AiService 的常驻配置。
        """
        if not provider or not provider.has_real_api_key():
            return {'ok': False, 'message': f'{provider.name if provider else "该供应商"} 尚未填写 API Key。'}

        saved_model = self._active_model
        try:
            self._active_model = model
            target_model = (model.model_id if model is not None
                            else (provider.models.filter(enabled=True).first().model_id
                                  if provider.models.filter(enabled=True).exists() else None))
            base_url = (provider.base_url or 'https://api.deepseek.com/v1').rstrip('/')
            endpoint = base_url + '/chat/completions'
            headers = {
                'Authorization': f'Bearer {provider.api_key}',
                'Content-Type': 'application/json',
            }
            payload = {
                'model': target_model,
                'messages': [{'role': 'user', 'content': '你好，请只回复四个字：连接成功。'}],
                'max_tokens': 64,
                'temperature': 0,
            }
            resp = requests.post(endpoint, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            content = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
            label = f'{provider.name}' + (f' / {model.badge()}' if model is not None else '')
            if '连接成功' in content:
                return {'ok': True, 'message': f'{label} 连接成功！AI 回复：{content[:60]}'}
            return {'ok': True, 'message': f'{label} 连通正常（AI 回复：{content[:60]}）'}
        except Exception as e:
            label = provider.name if provider else '该供应商'
            return {'ok': False, 'message': f'{label} 连接失败：{e}'}
        finally:
            self._active_model = saved_model
