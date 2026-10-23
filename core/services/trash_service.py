# -*- coding: utf-8 -*-
"""统一的软删除 / 恢复 / 彻底删除入口（对齐 Laravel app/Services/TrashService.php）。

设计约定（全系统统一）：
  - 软删除：只写 deleted_at 与 deleted_by，**绝不触碰物理文件**，保证可恢复；
  - 恢复：清空 deleted_at 与 deleted_by；
  - 彻底删除：仅管理员可触发（由 AdminTrashController 保证权限），
    先删除数据库记录，**成功后再清理物理文件**；
  - 物理文件字段与磁盘的对应关系集中在 PURGE_MAP 维护；
  - 回收站分类与「归属人」关系集中在 TYPES 维护。

【为什么必须先删记录、后删文件】
若先删文件再删记录，一旦记录删除失败（外键约束、并发等），
就会出现「记录还在、文件已丢」——这是**不可恢复**的损坏。
反之若记录已删、文件删除失败，只会留下「孤儿文件」，
由 files:gc 巡检命令兜底清理即可。宁可留下可回收的孤儿，不可丢失数据。

【容器型内容的孤儿文件】
- 对应 Django 的 on_delete=CASCADE，外键删除由数据库执行、
  不经过 ORM 层（不会触发任何文件清理逻辑）。
- 删除项目/任务/部门/项目模板这类「容器」前，必须先用 purge_descendants()
  逐条彻底删除其下持有文件的子记录，再删父记录（force_delete_deep）。

【个人回收站可见范围（scope_for_user）】
- 我删除的（deleted_by = 我） 或 我创建的（归属人 = 我）。
- 因此管理员删除的内容，创建人依然能在自己的回收站里恢复。
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Optional

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


# ============== TYPES：回收站分类配置 ==============

# 每个分类：
#   model       对应模型类
#   label       中文展示名
#   icon        FontAwesome 图标
#   field       列表展示用的字段（与 model.get_field_value() 配合）
#   owner       「创建人/归属人」列名；None 表示无归属人列，
#               个人回收站只能按 deleted_by 匹配
TYPES: dict[str, dict[str, Any]] = {}

# ============== PURGE_MAP：模型 => 物理文件所在磁盘与字段 ==============
#   disk      Django 内的根目录别名（file_repository / public / wecom_files）
#   column    数据库里存放路径的列
#   prefix    列值需要补的目录前缀（users.avatar 只存文件名，真实文件在 avatars/ 下）
#   history   历史版本快照目录类型
#       'collab'   → collab/{projectId}/history/{docId}_v{n}.{ext}
#       'taskfile' → {任务文件目录}/history/{fileId}_v{n}.{ext}
PURGE_MAP: dict[str, dict[str, Any]] = {}


def _bootstrap() -> None:
    """初始化 TYPES 与 PURGE_MAP。

    模块末尾会自动调用一次；各公开函数内部也保留防御式调用，
    以便在交互式环境或测试中被直接导入时仍能正常访问 TYPES。
    """
    if TYPES:
        return
    from core import models as m  # noqa: WPS433

    TYPES.update({
        'collab-documents':  {'model': m.CollabDocument,  'label': '协作文档', 'icon': 'fa-file-text-o', 'field': 'name',          'owner': 'created_by'},
        'projects':          {'model': m.Project,         'label': '项目',     'icon': 'fa-folder-o',    'field': 'name',          'owner': 'creator_id'},
        'tasks':             {'model': m.Task,            'label': '任务',     'icon': 'fa-tasks',       'field': 'name',          'owner': None},
        'files':             {'model': m.File,            'label': '任务文件', 'icon': 'fa-paperclip',   'field': 'original_name', 'owner': 'uploader_id'},
        'department-files':  {'model': m.DepartmentFile,  'label': '资料文件', 'icon': 'fa-folder-open', 'field': 'original_name', 'owner': 'uploader_id'},
        'messages':          {'model': m.Message,         'label': '聊天记录', 'icon': 'fa-comments',    'field': 'content',       'owner': 'user_id'},
        'wecom-files':       {'model': m.WecomFile,       'label': '企微文件', 'icon': 'fa-inbox',       'field': 'original_name', 'owner': 'user_id'},
        'notifications':     {'model': m.Notification,    'label': '通知',     'icon': 'fa-bell',        'field': 'title',         'owner': 'user_id'},
        'knowledges':        {'model': m.Knowledge,       'label': '知识库',   'icon': 'fa-book',        'field': 'title',         'owner': 'admin_id'},
        'knowledge-docs':    {'model': m.KnowledgeDoc,    'label': '知识文档', 'icon': 'fa-file-text-o', 'field': 'title',         'owner': None},
        'project-templates': {'model': m.ProjectTemplate, 'label': '项目模板', 'icon': 'fa-clone',       'field': 'name',          'owner': 'user_id'},
    })

    PURGE_MAP.update({
        m.CollabDocument.__name__:  {'disk': 'file_repository', 'column': 'storage_path', 'history': 'collab'},
        # 任务文件刻意**不配 history**：版本快照归任务所有、由保留上限统一裁剪，
        # 单个文件记录被彻底删除时不应连带删掉历史版本；任务删除时整目录清理。
        m.File.__name__:            {'disk': 'file_repository', 'column': 'path'},
        m.DepartmentFile.__name__:  {'disk': 'file_repository', 'column': 'path'},
        m.Message.__name__:         {'disk': 'public',          'column': 'file_path'},
        m.WecomFile.__name__:       {'disk': 'wecom_files',     'column': 'file_path'},
        m.User.__name__:            {'disk': 'public',          'column': 'avatar', 'prefix': 'avatars/'},
    })


# ============== 磁盘根映射（Django settings） ==============

# 【存储约定：全系统只有一个物理根 MEDIA_ROOT】
# 参考目录 Laravel 把物理存储拆成 storage/app/{private,public,file-repository,wecom-files}
# 四个 disk，数据库里保存的是「相对各自 disk 根」的路径。
# 本项目不做多挂载点隔离，因此**统一为一个物理根 MEDIA_ROOT**：
#   数据库里一律保存「相对 MEDIA_ROOT」的路径，例如
#     CollabDocument.storage_path = 'collab/1/doc_x.docx'
#     File.path                   = 'file_repository/project-1/task-2/x_v1.pdf'
#     Message.file_path           = 'chat_files/1/x.jpg'
#     User.avatar                 = 'u.jpg'（配 PURGE_MAP 的 prefix 'avatars/'）
#     WecomFile.file_path         = 'wecom-inbox/20261015/x.jpg'
# 下面的 disk 名只作为**逻辑分组**（TrashService 定位字段、巡检分目录扫描），
# 不再对应四个独立的物理根。
#
# 历史教训：此前这里把四个 disk 分别指向 media/{file_repository,public,wecom_files}，
# 而写入方全都落在 MEDIA_ROOT 下 —— 于是 _disk_path() 算出的路径永远不存在，
# 造成「彻底删除删不掉物理文件」「巡检把正常记录误判为死记录并可能真删记录」。
_MEDIA_ROOT = lambda: getattr(settings, 'PM_MEDIA_ROOT', None) or settings.MEDIA_ROOT

DISK_ROOT = {
    'file_repository': _MEDIA_ROOT,
    'public':          _MEDIA_ROOT,
    'wecom_files':     _MEDIA_ROOT,
    'local':           _MEDIA_ROOT,
}


def _disk_path(disk_name: str, *parts: str) -> str:
    """组合磁盘根与相对路径，避免路径穿越（越界自动回退到根）。"""
    root = DISK_ROOT.get(disk_name, lambda: settings.MEDIA_ROOT)()
    if not parts:
        return root
    rel = os.path.join(*parts).replace('\\', '/').lstrip('/')
    full = os.path.normpath(os.path.join(root, rel))
    # 防止越界写入磁盘根之外的位置
    if not full.startswith(os.path.normpath(root)):
        return root
    return full


# ============== TYPES 工具方法 ==============

def type_or_fail(type_key: str) -> dict[str, Any]:
    """校验并取出分类配置，未知分类 404。"""
    _bootstrap()
    if type_key not in TYPES:
        from django.http import Http404
        raise Http404('未知的回收站分类')
    return TYPES[type_key]


def all_types() -> dict[str, dict[str, Any]]:
    _bootstrap()
    return TYPES


def has_deleted_by_column(model) -> bool:
    """判断模型对应的表是否存在 deleted_by 列（每张表只在单次请求内查询一次）。"""
    _bootstrap()
    table = model._meta.db_table
    if not hasattr(has_deleted_by_column, '_cache'):
        has_deleted_by_column._cache = {}
    cache = has_deleted_by_column._cache
    if table in cache:
        return cache[table]
    with connection.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s "
            "AND column_name = 'deleted_by'",
            [table],
        )
        cache[table] = bool(cur.fetchone()[0])
    return cache[table]


def scope_for_user(qs, type_key: str, user_id: int):
    """限定为「某个人的回收站」范围：我删除的 或 我创建的。

    传入的查询必须已经只包含软删记录（manager 链上已加 only_deleted）。
    """
    _bootstrap()
    cfg = TYPES[type_key]
    owner = cfg.get('owner')
    cond = qs.filter(deleted_by=user_id)
    if owner:
        cond = cond | qs.filter(**{owner: user_id})
    return cond.distinct()


def count_for_user(user_id: int) -> int:
    """统计某人的个人回收站条数（用于导航栏徽章）。"""
    _bootstrap()
    total = 0
    for cfg in TYPES.values():
        model_cls = cfg['model']
        total += scope_for_user(model_cls.all_objects.filter(deleted_at__isnull=False),
                                _key_of(cfg['model']), user_id).count()
    return total


def _key_of(model_cls) -> Optional[str]:
    """反查某模型对应的回收站分类 key（找不到时返回 None）。"""
    _bootstrap()
    for k, cfg in TYPES.items():
        if cfg['model'] is model_cls:
            return k
    return None


def display_name(type_key: str, model) -> str:
    """生成记录在回收站里的展示名称（统一实现）。"""
    _bootstrap()
    cfg = TYPES.get(type_key, {})
    field = cfg.get('field', 'name')

    if type_key == 'collab-documents':
        ext = f".{model.extension}" if getattr(model, 'extension', '') else ''
        return _truncate(f"{getattr(model, 'name', '') or '未命名'}{ext}", 60)

    if type_key == 'messages':
        text = (getattr(model, 'content', '') or '').strip()
        if not text:
            file_name = getattr(model, 'file_name', '') or ''
            text = f'[文件] {file_name}' if file_name else '（空消息）'
        return _truncate(text, 60)

    raw = getattr(model, field, None) or '未命名'
    return _truncate(str(raw), 60)


def _truncate(text: str, length: int) -> str:
    if len(text) > length:
        return text[:length - 1] + '…'
    return text


# ============== 软删 / 恢复 / 彻底删除 ==============

def soft_delete(model, user_id: Optional[int] = None) -> bool:
    """软删除：只标记删除时间与删除人，保留物理文件以便恢复。

    deleted_by 由调用方在视图中传入（与 AppServiceProvider 等价）。
    """
    now = timezone.now()
    update_fields = ['deleted_at']
    if has_deleted_by_column(model):
        model.deleted_by = user_id
        update_fields.append('deleted_by')
    model.deleted_at = now
    model.save(update_fields=update_fields)
    return True


def restore(model) -> bool:
    """恢复软删除的记录，并清空「删除人」。"""
    model.deleted_at = None
    update_fields = ['deleted_at']
    if has_deleted_by_column(model):
        model.deleted_by = None
        update_fields.append('deleted_by')
    model.save(update_fields=update_fields)
    return True


def force_delete(model) -> bool:
    """彻底删除单条记录（不做子内容处理，调用方负责）。

    顺序：先删数据库记录，确认成功后清理物理文件。
    """
    targets = collect_purge_targets(model)
    with transaction.atomic():
        # Django ORM 的硬删除（绕过 SoftDeleteManager）
        model.__class__.all_objects.filter(pk=model.pk).delete()
    purge_targets(targets)
    return True


def force_delete_deep(model) -> bool:
    """深度彻底删除：先清理容器型内容的所有子记录，再删除自己。

    用于项目 / 任务 / 部门 / 项目模板 / 知识库 / 用户，
    避免数据库 CASCADE 硬删子记录时留下无人引用的孤儿文件。
    """
    purge_descendants(model)
    return force_delete(model)


# ============== 容器级联清理 ==============

def purge_descendants(model) -> None:
    """逐条彻底删除「容器型」记录的直接与间接子内容。

    子记录使用 all_objects 取出（含软删），覆盖「已进回收站」与「仍在用」两种状态，
    确保 CASCADE 之前其物理文件已被正确登记清理。
    """
    _bootstrap()
    from core import models as m  # noqa: WPS433

    if isinstance(model, m.Project):
        _purge_project_children(model)
        return

    if isinstance(model, m.Task):
        _purge_task_children(model)
        return

    if isinstance(model, m.Department):
        for f in m.DepartmentFile.all_objects.filter(department_id=model.pk):
            force_delete(f)
        for t in m.Task.all_objects.filter(department_id=model.pk):
            force_delete_deep(t)
        return

    if isinstance(model, m.ProjectTemplate):
        # ProjectTemplate 当前没有关联子内容表，保留扩展点
        return

    if isinstance(model, m.Knowledge):
        # knowledge_docs CASCADE 软删后会变 null；显式强制删除避免孤儿记录
        for d in m.KnowledgeDoc.all_objects.filter(knowledge_id=model.pk):
            force_delete(d)
        return

    if isinstance(model, m.KnowledgeDoc):
        for child in m.KnowledgeDoc.all_objects.filter(pid=model.pk):
            force_delete(child)
        return

    if isinstance(model, m.User):
        purge_user_assets(model)


def _purge_project_children(project) -> None:
    from core import models as m  # noqa: WPS433
    for d in m.CollabDocument.all_objects.filter(project_id=project.pk):
        force_delete(d)
    for msg in m.Message.all_objects.filter(project_id=project.pk):
        force_delete(msg)
    for t in m.Task.all_objects.filter(project_id=project.pk):
        force_delete_deep(t)


def _purge_task_children(task) -> None:
    from core import models as m  # noqa: WPS433
    for f in m.File.all_objects.filter(task_id=task.pk):
        force_delete(f)
    # 任务即将消失，整个文件目录（含历史版本快照）已无任何引用，
    # 整目录清理；file_versions 记录由 task_id 外键 CASCADE 自动清除。
    _purge_task_directory(task)


def _purge_task_directory(task) -> None:
    """删除任务的文件目录 file_repository/project-{projectId}/task-{taskId}/。"""
    try:
        dir_rel = f'file_repository/project-{task.project_id}/task-{task.pk}'
        full = _disk_path('file_repository', dir_rel)
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
        remove_if_empty('file_repository', f'file_repository/project-{task.project_id}')
    except Exception as exc:  # noqa: BLE001
        logger.warning('purge task dir failed: %s', exc)


# ============== 用户资产清理（删用户时调用） ==============

def purge_user_assets(user) -> None:
    """彻底删除用户前，清理其名下所有会连带消失的物理文件。

    users 被硬删时会触发大量 CASCADE（files / messages / projects / tasks …），
    这些级联不经过 ORM，必须在这里提前把文件清干净。
    """
    from core import models as m  # noqa: WPS433

    # 1. 头像
    if getattr(user, 'avatar', None):
        purge_targets([{'disk': 'public', 'path': f'avatars/{user.avatar}'}])

    # 2. 该用户上传/审核的任务文件
    for f in m.File.all_objects.filter(uploader_id=user.pk) | m.File.all_objects.filter(auditor_id=user.pk):
        force_delete(f)

    # 3. 聊天记录（含附件）
    for msg in (m.Message.all_objects.filter(user_id=user.pk)
                | m.Message.all_objects.filter(receiver_id=user.pk)):
        force_delete(msg)

    # 4. 该用户执行/审核的任务
    for t in (m.Task.all_objects.filter(executor_id=user.pk)
              | m.Task.all_objects.filter(auditor_id=user.pk)):
        force_delete_deep(t)

    # 5. 该用户创建的项目
    for p in m.Project.all_objects.filter(creator_id=user.pk):
        force_delete_deep(p)

    # 6. 该用户创建的协作文档
    for d in m.CollabDocument.all_objects.filter(created_by_id=user.pk):
        force_delete(d)


# ============== 物理文件清理 ==============

def collect_purge_targets(model) -> list[dict[str, Any]]:
    """收集模型对应的物理文件清理目标（不执行删除）。

    返回形如：
      {'disk': 'file_repository', 'path': 'project-1/task-3/xxx.pdf'}
      {'disk': 'file_repository', 'dir': '.../history', 'prefix': '12_v'}
    """
    _bootstrap()
    cfg = PURGE_MAP.get(model.__class__.__name__)
    if not cfg:
        return []

    targets: list[dict[str, Any]] = []
    rel = getattr(model, cfg['column'], None)
    if rel:
        rel = str(rel).lstrip('/')
        targets.append({
            'disk': cfg['disk'],
            'path': (cfg.get('prefix') or '') + rel,
        })

    history_type = cfg.get('history')
    if history_type:
        dir_rel = history_dir(model, history_type)
        if dir_rel:
            targets.append({
                'disk': cfg['disk'],
                'dir': dir_rel,
                'prefix': f'{model.pk}_v',
            })

    return targets


def history_dir(model, history_type: str) -> Optional[str]:
    if history_type == 'collab':
        pid = getattr(model, 'project_id', None)
        return f'collab/{pid}/history' if pid else None
    if history_type == 'taskfile':
        path = getattr(model, 'path', '') or ''
        if not path:
            return None
        return os.path.dirname(path) + '/history'
    return None


def purge_targets(targets: list[dict[str, Any]]) -> None:
    """按清理目标删除磁盘文件；单个失败不影响其余，异常只记录不上抛。"""
    for target in targets:
        try:
            disk = target['disk']
            if target.get('path'):
                full = _disk_path(disk, target['path'])
                if os.path.isfile(full):
                    os.remove(full)
            if target.get('dir'):
                purge_dir_by_prefix(disk, target['dir'], target.get('prefix', ''))
        except Exception as exc:  # noqa: BLE001
            logger.warning('purge_targets failed: %s | %s', target, exc)


def purge_dir_by_prefix(disk: str, dir_rel: str, prefix: str = '') -> None:
    """删除目录下匹配前缀的文件；目录清空后一并移除，不留空目录。"""
    full_dir = _disk_path(disk, dir_rel)
    if not os.path.isdir(full_dir):
        return
    for name in os.listdir(full_dir):
        if prefix and not name.startswith(prefix):
            continue
        fp = os.path.join(full_dir, name)
        if os.path.isfile(fp) or os.path.islink(fp):
            try:
                os.remove(fp)
            except OSError as exc:
                logger.warning('remove file failed: %s | %s', fp, exc)
    remove_if_empty(disk, dir_rel)


def remove_if_empty(disk: str, dir_rel: str) -> None:
    """目录及其各级父目录为空时逐级删除，避免残留空目录。"""
    dir_rel = dir_rel.strip('/')
    if not dir_rel:
        return
    try:
        while dir_rel and dir_rel != '.':
            full_dir = _disk_path(disk, dir_rel)
            if not os.path.isdir(full_dir):
                dir_rel = os.path.dirname(dir_rel)
                continue
            # 若目录下还有文件或子目录，则停止上溯
            if os.listdir(full_dir):
                return
            os.rmdir(full_dir)
            dir_rel = os.path.dirname(dir_rel)
    except Exception as exc:  # noqa: BLE001
        logger.warning('remove_if_empty failed: %s | %s', dir_rel, exc)


# ============== 任务文件历史版本裁剪 ==============

def prune_file_history(task) -> int:
    """裁剪任务的历史版本快照，每个任务只保留最近 N 个版本。

    保留上限由 settings.PM_FILE_VERSION_KEEP 控制（默认 3）。
    同时删除过期的快照文件与对应 file_versions 记录，
    保证「记录与文件」始终一一对应。
    """
    keep = max(1, int(getattr(settings, 'PM_FILE_VERSION_KEEP', 3)))

    # 直接读取 file_versions 行记录（skip老的外键路径），按创建时间倒序
    with connection.cursor() as cur:
        cur.execute(
            "SELECT id, path FROM file_versions WHERE task_id = %s "
            "ORDER BY created_at DESC, id DESC",
            [task.pk],
        )
        rows = cur.fetchall()
    if len(rows) <= keep:
        return 0

    deleted = 0
    for vid, path in rows[keep:]:
        try:
            if path:
                full = _disk_path('file_repository', path)
                if os.path.isfile(full):
                    os.remove(full)
            with connection.cursor() as cur:
                cur.execute("DELETE FROM file_versions WHERE id = %s", [vid])
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning('prune_file_history: %s | %s', vid, exc)

    remove_if_empty(
        'file_repository',
        f'file_repository/project-{task.project_id}/task-{task.pk}/history',
    )
    return deleted


# ============== 兼容旧调用 ==============

def purge_files(model) -> None:
    """兼容旧调用：清理模型关联的物理文件。"""
    purge_targets(collect_purge_targets(model))


def clear_deleted_by(model) -> None:
    """清空记录的 deleted_by（已在 restore() 中处理，此处保留以兼容旧调用）。"""
    if not has_deleted_by_column(model):
        return
    try:
        model.deleted_by = None
        model.save(update_fields=['deleted_by'])
    except Exception as exc:  # noqa: BLE001
        logger.warning('clear_deleted_by failed: %s', exc)


# 模块加载时自动初始化一次，保证 TYPES / PURGE_MAP 始终可用
_bootstrap()