# -*- coding: utf-8 -*-
"""管理后台视图：对齐 Laravel AdminController.php（权限：管理员全量，项目监督人仅发送通知）。"""
import csv
import io
import os
import shutil
import subprocess
import time

from django.contrib import messages as dj_messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction, connection
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.conf import settings
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import (Department, File, Message, Notification, OperationLog,
                         Role, Status, TaskType, Todo, User, Project, Task,
                         ExtensionApplication, FileVersion, DefaultMember,
                         TemplateTask, ProjectLog, DepartmentFile)
from core.services.operation_log import log_operation

_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pm_admin(view):
    """管理员放行；项目监督人仅允许 send-notification / notification.post；其余 403。"""
    @login_required
    def wrapped(request, *args, **kwargs):
        user = request.user
        rn = ''
        if getattr(request, 'resolver_match', None):
            rn = request.resolver_match.url_name or ''
        if user.is_admin():
            return view(request, *args, **kwargs)
        if user.is_project_manager() and rn in ('admin.send-notification', 'admin.notification.post'):
            return view(request, *args, **kwargs)
        return HttpResponseForbidden('您无权访问管理后台')
    return wrapped


@pm_admin
def index(request):
    stats = {'projects': Project.objects.count(), 'users': User.objects.count(),
             'files': File.objects.count(), 'tasks': Task.objects.count()}
    return render(request, 'admin/index.html', {'stats': stats})


@pm_admin
def users(request):
    # 用户管理 = 基础数据 → 升序（按 id 稳定排序）
    qs = User.objects.filter(deleted_at__isnull=True).select_related('department', 'role').order_by('id')
    return render(request, 'admin/users.html', {'users': Paginator(qs, 10).get_page(request.GET.get('page'))})


@pm_admin
def deleted_users(request):
    """已删除用户列表（关联数据量用于在彻底删除前提示风险，对齐 AdminController::deletedUsers）。"""
    from django.db.models import Q

    from core.models import ExtensionApplication, File, Message, Notification, Project, ProjectLog
    qs = User.objects.filter(deleted_at__isnull=False).select_related('department', 'role').order_by('-deleted_at')
    page = Paginator(qs, 10).get_page(request.GET.get('page'))

    rows = []
    for u in page.object_list:
        rows.append({
            'user': u,
            'project_count': Project.objects.filter(creator_id=u.id).count(),
            'task_count': (Task.objects.filter(executor_id=u.id).count()
                           + Task.objects.filter(auditor_id=u.id).count()),
            'file_count': (File.objects.filter(uploader_id=u.id).count()
                           + File.objects.filter(auditor_id=u.id).count()),
            'message_count': Message.objects.filter(user_id=u.id).count(),
            'notification_count': (Notification.objects.filter(Q(user_id=u.id) | Q(sender_id=u.id)).count()),
            'extension_count': (ExtensionApplication.objects.filter(applicant_id=u.id).count()
                                + ExtensionApplication.objects.filter(approver_id=u.id).count()),
            'log_count': ProjectLog.objects.filter(user_id=u.id).count(),
        })
    return render(request, 'admin/deleted-users.html', {'users': page, 'rows': rows})


@pm_admin
@require_POST
def restore_user(request, pk):
    user = get_object_or_404(User, pk=pk, deleted_at__isnull=False)
    user.deleted_at = None
    user.save(update_fields=['deleted_at', 'updated_at'])
    log_operation('用户管理', '恢复用户', f'管理员恢复了用户「{user.name}」', user=request.user)
    return redirect('admin.users.deleted')


@pm_admin
@require_POST
def force_delete_user(request, pk):
    """彻底删除用户。

    硬删用户会触发大量外键 CASCADE（files / messages / tasks / projects …），
    这些级联由数据库执行、不经过 ORM，因此必须先把其名下的物理文件清干净，
    否则头像、任务文件、聊天附件都会变成无人引用的孤儿文件。
    """
    from core.services.trash_service import purge_user_assets
    user = get_object_or_404(User, pk=pk, deleted_at__isnull=False)
    name = user.name
    purge_user_assets(user)
    User.all_objects.filter(pk=user.pk).delete()
    log_operation('用户管理', '彻底删除用户',
                  f'管理员彻底删除了用户「{name}」及其关联的文件、任务与项目',
                  user=request.user)
    return redirect('admin.users.deleted')


@pm_admin
def create_user(request):
    return render(request, 'admin/create-user.html',
                  {'departments': Department.objects.all(), 'roles': Role.objects.all()})


@pm_admin
@require_POST
def store_user(request):
    username = request.POST.get('username', '').strip()
    name = request.POST.get('name', '').strip()
    password = request.POST.get('password', '') or '123456'
    if not username or not name:
        dj_messages.error(request, '用户名和姓名为必填项')
        return redirect('admin.users.create')
    if User.objects.filter(username=username, deleted_at__isnull=True).exists():
        dj_messages.error(request, '用户名已存在')
        return redirect('admin.users.create')
    User.objects.create_user(username=username, password=password, name=name,
                             department_id=request.POST.get('department_id') or None,
                             role_id=request.POST.get('role_id') or None, is_active=True)
    dj_messages.success(request, f'用户「{name}」创建成功' + ('（未设密码，默认密码 123456）' if password == '123456' else ''))
    log_operation('用户管理', '创建用户', f'管理员创建用户「{name}」', user=request.user)
    return redirect('admin.users')


@pm_admin
def edit_user(request, pk):
    user = get_object_or_404(User, pk=pk)
    return render(request, 'admin/edit-user.html',
                  {'user': user, 'departments': Department.objects.all(), 'roles': Role.objects.all()})


@pm_admin
@require_POST
def update_user(request, pk):
    user = get_object_or_404(User, pk=pk)
    username = request.POST.get('username', '').strip()
    name = request.POST.get('name', '').strip()
    if not username or not name:
        dj_messages.error(request, '用户名和姓名为必填项')
        return redirect('admin.users.edit', pk=pk)
    if User.objects.filter(username=username, deleted_at__isnull=True).exclude(pk=user.pk).exists():
        dj_messages.error(request, '用户名已存在')
        return redirect('admin.users.edit', pk=pk)
    user.username = username
    user.name = name
    user.department_id = request.POST.get('department_id') or None
    user.role_id = request.POST.get('role_id') or None
    user.is_active = bool(request.POST.get('is_active'))
    pw = request.POST.get('password')
    if pw:
        user.set_password(pw)
    user.save()
    dj_messages.success(request, f'用户「{user.name}」信息已更新')
    log_operation('用户管理', '更新用户', f'管理员更新用户「{user.name}」信息', user=request.user)
    return redirect('admin.users')


@pm_admin
@require_POST
def delete_user(request, pk):
    user = get_object_or_404(User, pk=pk)
    name = user.name
    user.delete()
    log_operation('用户管理', '删除用户', f'管理员删除用户「{name}」', user=request.user)
    return redirect('admin.users')


@pm_admin
def departments(request):
    qs = Department.objects.order_by('sort_order', 'id')
    return render(request, 'admin/departments.html', {'departments': Paginator(qs, 10).get_page(request.GET.get('page'))})


@pm_admin
def create_department(request):
    return render(request, 'admin/create-department.html')


@pm_admin
@require_POST
def store_department(request):
    name = request.POST.get('name', '').strip()
    code = request.POST.get('code', '').strip()
    if not name or not code:
        dj_messages.error(request, '部门名称与部门编码均为必填项')
        return redirect('admin.departments.create')
    if Department.objects.filter(code=code).exists():
        dj_messages.error(request, f'部门编码「{code}」已存在，请更换后重试')
        return redirect('admin.departments.create')
    if Department.all_objects.filter(name=name).exists():
        dj_messages.error(request, f'该部门名称已存在，请更换后重试')
        return redirect('admin.departments.create')
    Department.objects.create(name=name, code=code, type=request.POST.get('type', 'project'),
                              sort_order=int(request.POST.get('sort_order', 0) or 0))
    dj_messages.success(request, f'部门「{name}」创建成功')
    log_operation('部门管理', '创建部门', f'管理员创建部门「{name}」', user=request.user)
    return redirect('admin.departments')


@pm_admin
def edit_department(request, pk):
    return render(request, 'admin/edit-department.html', {'department': get_object_or_404(Department, pk=pk)})


@pm_admin
@require_POST
def update_department(request, pk):
    dept = get_object_or_404(Department, pk=pk)
    name = request.POST.get('name', '').strip()
    code = request.POST.get('code', '').strip()
    if not name or not code:
        dj_messages.error(request, '部门名称与部门编码均为必填项')
        return redirect('admin.departments.edit', pk=pk)
    if Department.all_objects.filter(name=name).exclude(pk=pk).exists():
        dj_messages.error(request, f'该部门名称已存在，请更换后重试')
        return redirect('admin.departments.edit', pk=pk)
    if Department.objects.filter(code=code).exclude(pk=pk).exists():
        dj_messages.error(request, f'部门编码「{code}」已存在，请更换后重试')
        return redirect('admin.departments.edit', pk=pk)
    dept.name = name
    dept.code = code
    dept.type = request.POST.get('type', 'project')
    dept.sort_order = int(request.POST.get('sort_order', 0) or 0)
    dept.save()
    dj_messages.success(request, f'部门「{name}」已更新')
    log_operation('部门管理', '更新部门', f'管理员更新部门「{dept.name}」', user=request.user)
    return redirect('admin.departments')


@pm_admin
@require_POST
@transaction.atomic
def delete_department(request, pk):
    """软删除部门（与原硬删方案语义完全不同）。

    业务规则（用户决策）：
      - 部门本身：仅软删（进回收站，15 天后由 trash:purge-expired 自动彻底删除）；
      - 部门成员：保留账号，仅把 department_id 置空 → 转为普通用户；
      - 部门资料文件 / 任务类型 / 默认成员模板：随部门一起物理删除
        （部门专属，部门没了也无意义）；
      - 项目 / 任务 / 聊天记录 / 通知 / 文件：完全不动；
      - 成员仍可通过原身份（creator_id / executor_id / auditor_id）访问自己此前参入的项目。

    软删方案既避开 users.department_id 的 RESTRICT 外键冲突，
    又完整保留成员历史数据。
    """
    dept = get_object_or_404(Department, pk=pk)
    name = dept.name

    # 提前统计，便于操作日志与提示
    user_count = dept.users.filter(deleted_at__isnull=True).count()
    file_count = dept.department_files.filter(deleted_at__isnull=True).count()
    task_type_count = dept.task_types.count() if hasattr(dept, 'task_types') else TaskType.objects.filter(department_id=dept.id).count()
    default_member_count = DefaultMember.objects.filter(department_id=dept.id).count()

    from core.services.trash_service import force_delete
    # 1. 部门成员 → 普通用户：仅清空 department_id，账号与历史数据全部保留
    User.all_objects.filter(department_id=dept.id, deleted_at__isnull=True).update(department_id=None)

    # 2. 部门资料文件 → 物理删除（含物理文件，避免遗留孤儿）
    for dept_file in dept.department_files.all():
        force_delete(dept_file)

    # 3. 部门任务类型 → 物理删除（无物理文件）
    TaskType.objects.filter(department_id=dept.id).delete()

    # 4. 部门默认成员模板 → 物理删除（无物理文件）
    DefaultMember.objects.filter(department_id=dept.id).delete()

    # 5. 软删部门本身（Department 已加 SoftDeletes，进回收站 15 天）
    dept.delete()

    log_operation('部门管理', '软删除部门',
                  f'管理员软删除部门「{name}」：{user_count} 名成员转为普通用户、'
                  f'{file_count} 个资料文件、{task_type_count} 个任务类型、'
                  f'{default_member_count} 个默认成员模板随部门进入回收站'
                  f'（项目、任务、聊天记录、文件等成员历史数据均保留，'
                  f'成员仍可通过原身份访问自己此前参入的项目）',
                  user=request.user)

    dj_messages.success(request,
                        f'部门「{name}」已删除，{user_count} 名成员转为普通用户'
                        f'（项目、任务不受影响，成员仍可访问自己此前参入的项目）')
    return redirect('admin.departments')


@pm_admin
def roles(request):
    qs = Role.objects.order_by('id')
    return render(request, 'admin/roles.html', {'roles': Paginator(qs, 10).get_page(request.GET.get('page'))})


@pm_admin
def create_role(request):
    return render(request, 'admin/create-role.html')


@pm_admin
@require_POST
def store_role(request):
    name = request.POST.get('name', '').strip()
    code = request.POST.get('code', '').strip()
    if not name or not code:
        dj_messages.error(request, '角色名称与角色编码均为必填项')
        return redirect('admin.roles.create')
    if Role.objects.filter(code=code).exists():
        dj_messages.error(request, f'角色编码「{code}」已存在，请更换后重试')
        return redirect('admin.roles.create')
    if Role.objects.filter(name=name).exists():
        dj_messages.error(request, f'该角色名称已存在，请更换后重试')
        return redirect('admin.roles.create')
    Role.objects.create(name=name, code=code)
    dj_messages.success(request, f'角色「{name}」创建成功')
    log_operation('角色管理', '创建角色', f'管理员创建角色「{name}」', user=request.user)
    return redirect('admin.roles')


@pm_admin
def edit_role(request, pk):
    return render(request, 'admin/edit-role.html', {'role': get_object_or_404(Role, pk=pk)})


@pm_admin
@require_POST
def update_role(request, pk):
    role = get_object_or_404(Role, pk=pk)
    name = request.POST.get('name', '').strip()
    code = request.POST.get('code', '').strip()
    if not name or not code:
        dj_messages.error(request, '角色名称与角色编码均为必填项')
        return redirect('admin.roles.edit', pk=pk)
    if Role.objects.filter(code=code).exclude(pk=pk).exists():
        dj_messages.error(request, f'角色编码「{code}」已存在，请更换后重试')
        return redirect('admin.roles.edit', pk=pk)
    if Role.objects.filter(name=name).exclude(pk=pk).exists():
        dj_messages.error(request, f'该角色名称已存在，请更换后重试')
        return redirect('admin.roles.edit', pk=pk)
    role.name = name
    role.code = code
    role.save()
    dj_messages.success(request, f'角色「{name}」已更新')
    log_operation('角色管理', '更新角色', f'管理员更新角色「{role.name}」', user=request.user)
    return redirect('admin.roles')


@pm_admin
@require_POST
def delete_role(request, pk):
    role = get_object_or_404(Role, pk=pk)
    role.delete()
    log_operation('角色管理', '删除角色', f'管理员删除角色「{role.name}」', user=request.user)
    return redirect('admin.roles')


@pm_admin
def statuses(request):
    qs = Status.objects.order_by('id')
    return render(request, 'admin/statuses.html', {'statuses': Paginator(qs, 15).get_page(request.GET.get('page'))})


@pm_admin
def edit_status(request, pk):
    return render(request, 'admin/edit-status.html', {'status': get_object_or_404(Status, pk=pk)})


@pm_admin
@require_POST
def update_status(request, pk):
    status = get_object_or_404(Status, pk=pk)
    status.custom_label = request.POST.get('custom_label') or None
    status.save()
    dj_messages.success(request, '状态标签已保存')
    log_operation('状态管理', '更新状态标签', f'管理员更新状态「{status.state_class}」的标签', user=request.user)
    return redirect('admin.statuses')


@pm_admin
def task_types(request):
    qs = TaskType.objects.select_related('department').order_by('sort_order', 'id')
    return render(request, 'admin/task-types.html', {
        'task_types': Paginator(qs, 10).get_page(request.GET.get('page')),
        'departments': Department.objects.filter(type='project')})


@pm_admin
def create_task_type(request):
    return render(request, 'admin/create-task-type.html', {'departments': Department.objects.filter(type='project')})


@pm_admin
@require_POST
def store_task_type(request):
    name = request.POST.get('name', '').strip()
    if not name:
        dj_messages.error(request, '任务类型名称为必填项')
        return redirect('admin.task-types.create')
    if TaskType.objects.filter(name=name).exists():
        dj_messages.error(request, f'该任务类型名称已存在，请更换后重试')
        return redirect('admin.task-types.create')
    TaskType.objects.create(name=name, department_id=request.POST.get('department_id') or None,
                            sort_order=int(request.POST.get('sort_order', 0) or 0))
    dj_messages.success(request, f'任务类型「{name}」创建成功')
    log_operation('任务类型管理', '创建任务类型', f'管理员创建任务类型「{name}」', user=request.user)
    return redirect('admin.task-types')


@pm_admin
def edit_task_type(request, pk):
    return render(request, 'admin/edit-task-type.html',
                  {'task_type': get_object_or_404(TaskType, pk=pk), 'departments': Department.objects.filter(type='project')})


@pm_admin
@require_POST
def update_task_type(request, pk):
    tt = get_object_or_404(TaskType, pk=pk)
    name = request.POST.get('name', '').strip()
    if not name:
        dj_messages.error(request, '任务类型名称为必填项')
        return redirect('admin.task-types.edit', pk=pk)
    if TaskType.objects.filter(name=name).exclude(pk=pk).exists():
        dj_messages.error(request, f'该任务类型名称已存在，请更换后重试')
        return redirect('admin.task-types.edit', pk=pk)
    tt.name = name
    tt.department_id = request.POST.get('department_id') or None
    tt.sort_order = int(request.POST.get('sort_order', 0) or 0)
    tt.save()
    dj_messages.success(request, f'任务类型「{name}」已更新')
    log_operation('任务类型管理', '更新任务类型', f'管理员更新任务类型「{tt.name}」', user=request.user)
    return redirect('admin.task-types')


@pm_admin
@require_POST
def delete_task_type(request, pk):
    tt = get_object_or_404(TaskType, pk=pk)
    tt.delete()
    log_operation('任务类型管理', '删除任务类型', f'管理员删除任务类型「{tt.name}」', user=request.user)
    return redirect('admin.task-types')


@pm_admin
def file_repository(request):
    qs = File.objects.select_related('task__project', 'uploader').all()
    if request.GET.get('file_name'):
        qs = qs.filter(original_name__icontains=request.GET['file_name'])
    if request.GET.get('project_name'):
        qs = qs.filter(task__project__name__icontains=request.GET['project_name'])
    if request.GET.get('date_from'):
        qs = qs.filter(created_at__date__gte=request.GET['date_from'])
    if request.GET.get('date_to'):
        qs = qs.filter(created_at__date__lte=request.GET['date_to'])
    if request.GET.get('status'):
        qs = qs.filter(status=request.GET['status'])
    qs = qs.order_by('-created_at')
    return render(request, 'admin/file-repository.html', {'files': Paginator(qs, 10).get_page(request.GET.get('page'))})


@pm_admin
def operation_logs(request):
    qs = OperationLog.objects.select_related('user').order_by('-created_at')
    return render(request, 'admin/operation-logs.html', {'logs': Paginator(qs, 10).get_page(request.GET.get('page'))})


@pm_admin
def send_notification(request):
    return render(request, 'admin/send-notification.html')


@pm_admin
@require_POST
def post_notification(request):
    title = request.POST.get('title', '').strip()
    content = request.POST.get('content', '').strip()
    if title and content:
        for u in User.objects.all():
            Notification.objects.create(title=title, content=content, sender_id=request.user.id, user_id=u.id, type='system')
        log_operation('通知管理', '发送通知', f'管理员发送系统通知「{title}」', user=request.user)
    return redirect('admin.send-notification')


@pm_admin
def todos(request):
    qs = Todo.objects.select_related('user').order_by('-created_at')
    return render(request, 'admin/todos.html', {'todos': Paginator(qs, 10).get_page(request.GET.get('page'))})


def _process_running(keyword):
    """检测进程是否在运行。Windows 用 PowerShell，Linux 用 ps。"""
    import platform
    try:
        if platform.system() == 'Windows':
            ps_cmd = (
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                "| Select-Object -ExpandProperty CommandLine"
            )
            out = subprocess.run(
                ['powershell', '-NoProfile', '-Command', ps_cmd],
                capture_output=True, text=True, timeout=10)
        else:
            out = subprocess.run(
                ['ps', '-eo', 'args'],
                capture_output=True, text=True, timeout=10)
        return keyword.lower() in out.stdout.lower()
    except Exception:
        return False


@pm_admin
def services(request):
    import platform
    try:
        import django
        django_version = django.get_version()
    except Exception:
        django_version = '5.2'
    try:
        total = shutil.disk_usage(_PROJ_ROOT)
        free, disk_total, used_pct = total.free, total.total, round((1 - total.free / total.total) * 100, 1)
    except Exception:
        free = 0
        disk_total = 0
        used_pct = 0
    db_size = 0
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(data_length + index_length),0) FROM information_schema.tables WHERE table_schema=%s",
                        [connection.settings_dict['NAME']])
            db_size = int(cur.fetchone()[0])
    except Exception:
        db_size = 0
    backups = []
    backup_dir = os.path.join(_PROJ_ROOT, 'backups')
    if os.path.isdir(backup_dir):
        for f in os.listdir(backup_dir):
            if f.endswith(('.sqlite', '.zip', '.sql')):
                fp = os.path.join(backup_dir, f)
                backups.append({'name': f, 'size': os.path.getsize(fp),
                                'date': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(fp)))})
        backups.sort(key=lambda b: b['date'], reverse=True)
    return render(request, 'admin/services.html', {
        'scheduler_running': _process_running('beat'),
        'queue_running': _process_running('worker'),
        'vite_running': False,
        'server_info': {
            'os': platform.system() or 'Linux',
            'python_version': platform.python_version(),
            'django_version': django_version,
            'server_time': timezone.now().strftime('%Y-%m-%d %H:%M:%S'),
            'disk_free': free, 'disk_total': disk_total, 'disk_used_percent': used_pct,
            'db_size': db_size, 'db_name': connection.settings_dict['NAME'],
        },
        'backups': backups,
    })


@pm_admin
@require_POST
def backup_database(request):
    try:
        backup_dir = os.path.join(_PROJ_ROOT, 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        backup_name = 'backup_' + time.strftime('%Y%m%d_%H%M%S') + '.sql'
        backup_path = os.path.join(backup_dir, backup_name)
        db = settings.DATABASES['default']
        cmd = ['mysqldump', '-h%s' % db['HOST'], '-P%s' % db['PORT'], '-u%s' % db['USER'],
               '-p%s' % db['PASSWORD'], '--single-transaction', '--routines', '--triggers', db['NAME']]
        with open(backup_path, 'w', encoding='utf-8') as f:
            r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
        if r.returncode != 0 or os.path.getsize(backup_path) == 0:
            os.remove(backup_path)
            dj_messages.error(request, 'mysqldump 执行失败，请确认已安装 mysql 客户端且连接信息正确')
            return redirect('admin.services')
        log_operation('系统管理', '备份数据库', f'创建数据库备份：{backup_name}', user=request.user)
        dj_messages.success(request, f'数据库备份成功：{backup_name}')
    except Exception as e:
        dj_messages.error(request, f'数据库备份失败：{e}')
    return redirect('admin.services')


@pm_admin
def import_users_form(request):
    return render(request, 'admin/import-users.html',
                  {'departments': Department.objects.all(), 'roles': Role.objects.all(),
                   'import_result': request.session.pop('import_result', None)})


@pm_admin
@require_POST
def import_users(request):
    csv_file = request.FILES.get('csv_file')
    success = skipped = failed = 0
    errors = []
    if csv_file:
        data = csv_file.read().decode('utf-8-sig', errors='ignore')
        reader = csv.reader(io.StringIO(data))
        for line_no, row in enumerate(reader, start=1):
            if not any((c or '').strip() for c in row):
                continue
            if len(row) < 5:
                failed += 1
                errors.append(f'第 {line_no} 行：列数不足（需要 5 列，实际 {len(row)} 列），已跳过')
                continue
            username, password, name, dept_id, role_id = [x.strip() for x in row[:5]]
            if not username or not password or not name:
                failed += 1
                errors.append(f'第 {line_no} 行：用户名/密码/姓名不能为空，已跳过')
                continue
            if len(password) < 6 or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
                failed += 1
                errors.append(f'第 {line_no} 行：密码不符合要求（{name}），需 ≥6 位且含字母+数字，已跳过')
                continue
            if not Department.objects.filter(pk=dept_id).exists():
                failed += 1
                errors.append(f'第 {line_no} 行：部门 ID {dept_id} 不存在（{name}），已跳过')
                continue
            if not Role.objects.filter(pk=role_id).exists():
                failed += 1
                errors.append(f'第 {line_no} 行：角色 ID {role_id} 不存在（{name}），已跳过')
                continue
            if User.objects.filter(username=username, deleted_at__isnull=True).exists():
                skipped += 1
                errors.append(f'第 {line_no} 行：用户名 {username} 已存在，已跳过')
                continue
            User.objects.create_user(username=username, password=password, name=name,
                                     department_id=dept_id, role_id=role_id, is_active=True)
            success += 1
    if success > 0:
        log_operation('用户管理', '批量导入用户', f'从 CSV 批量导入：成功 {success}，跳过 {skipped}，失败 {failed}', user=request.user)
    request.session['import_result'] = {
        'success': success, 'skipped': skipped, 'failed': failed, 'errors': errors}
    return redirect('admin.users.import-form')


def _chat_file_size():
    total = 0
    media = os.path.join(_PROJ_ROOT, 'media', 'chat_files')
    if os.path.isdir(media):
        for root, _, files in os.walk(media):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    if total < 1024:
        return f'{total} B'
    if total < 1024 ** 2:
        return f'{round(total / 1024, 2)} KB'
    if total < 1024 ** 3:
        return f'{round(total / (1024 ** 2), 2)} MB'
    return f'{round(total / (1024 ** 3), 2)} GB'


@pm_admin
def chat_messages(request):
    qs = Message.objects.select_related('user', 'receiver', 'project').all()
    if request.GET.get('project_id'):
        qs = qs.filter(project_id=request.GET['project_id'])
    if request.GET.get('chat_type') == 'group':
        qs = qs.filter(is_private=False)
    elif request.GET.get('chat_type') == 'private':
        qs = qs.filter(is_private=True)
    if request.GET.get('date_from'):
        qs = qs.filter(created_at__gte=request.GET['date_from'] + ' 00:00:00')
    if request.GET.get('date_to'):
        qs = qs.filter(created_at__lte=request.GET['date_to'] + ' 23:59:59')
    # 聊天记录统一正序：审计/查看时按"对话顺序"读取更自然
    qs = qs.order_by('created_at')
    page = Paginator(qs, 20).get_page(request.GET.get('page'))
    stats = {'total': Message.objects.count(),
             'group': Message.objects.filter(is_private=False).count(),
             'private': Message.objects.filter(is_private=True).count(),
             'with_file': Message.objects.filter(file_path__isnull=False).count(),
             'total_file_size': _chat_file_size()}
    projects = Project.objects.order_by('name').values('id', 'name')
    return render(request, 'admin/chat-messages.html', {'chat_messages': page, 'stats': stats, 'projects': projects})


@pm_admin
@require_POST
def delete_chat_messages(request):
    ids = request.POST.getlist('ids')
    msgs = Message.objects.filter(pk__in=ids)
    # 软删除：保留附件物理文件，管理员可在回收站恢复或彻底删除（对齐参考目录 AdminController）。
    # 这里**不能**直接 os.remove —— 那样回收站恢复后附件会丢失，
    # 且记录与物理文件的对应关系会被破坏。
    count = msgs.delete()
    log_operation('聊天记录管理', '批量删除聊天记录',
                  f'管理员将 {count} 条聊天记录移入回收站', user=request.user)
    return redirect('admin.chat-messages')


@pm_admin
@require_POST
def clear_project_chat(request):
    project_id = request.POST.get('project_id')
    chat_type = request.POST.get('chat_type', 'all')
    qs = Message.objects.filter(project_id=project_id)
    if chat_type == 'group':
        qs = qs.filter(is_private=False)
    elif chat_type == 'private':
        qs = qs.filter(is_private=True)
    # 软删除：保留附件物理文件，交由回收站统一管理
    count = qs.delete()
    proj = Project.objects.filter(pk=project_id).first()
    proj_name = proj.name if proj else str(project_id)
    log_operation('聊天记录管理', '清空项目聊天记录',
                  f'管理员将项目「{proj_name}」的 {count} 条聊天记录移入回收站', user=request.user)
    return redirect('admin.chat-messages')
