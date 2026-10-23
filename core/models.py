# -*- coding: utf-8 -*-
"""core 业务模型（依据参考目录 database/migrations/*.php 的真实表结构编写）。

User 为自定义认证模型（username 登录）。各模型 Meta.db_table 对齐旧库表名，
字段/外键/软删除/索引均按迁移文件保真。
"""
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
import hashlib
import uuid

# ================= 软删除基类（对齐 Eloquent SoftDeletes） =================

class SoftDeleteQuerySet(models.QuerySet):
    def delete(self):
        for obj in self:
            obj.deleted_at = timezone.now()
            obj.save(update_fields=['deleted_at'])

class SoftDeleteManager(models.Manager):
    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).filter(deleted_at__isnull=True)

    def all_with_deleted(self):
        return super().get_queryset()

class SoftDeleteModel(models.Model):
    """提供 deleted_at 软删除的抽象基类。"""
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        self.deleted_at = timezone.now()
        self.save(using=using)

    def hard_delete(self, using=None, keep_parents=False):
        super().delete(using=using, keep_parents=keep_parents)

    def restore(self):
        self.deleted_at = None
        self.save(update_fields=['deleted_at'])


# ================= 认证用户 =================

class UserManager(BaseUserManager):
    """自定义 UserManager：以 username 为登录标识。"""
    use_in_migrations = True

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)

    def all_with_deleted(self):
        return super().get_queryset()

    def _create_user(self, username, password, **extra_fields):
        if not username:
            raise ValueError('必须提供用户名 username')
        user = self.model(username=username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, username, password=None, **extra_fields):
        extra_fields.setdefault('is_active', True)
        return self._create_user(username, password, **extra_fields)

    def create_superuser(self, username, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        return self._create_user(username, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """users 表。username 登录；唯一键为 (username, deleted_at) 支持软删后重用。"""
    objects = models.Manager()
    all_objects = models.Manager()  # 包含已软删用户（回收站使用）

    username = models.CharField(max_length=255, unique=True)
    password = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    avatar = models.CharField(max_length=255, null=True, blank=True, help_text='用户头像文件名')
    department = models.ForeignKey(
        'Department', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='users', db_column='department_id')
    role = models.ForeignKey(
        'Role', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='users', db_column='role_id')
    is_active = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)
    remember_token = models.CharField(max_length=100, null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.BigIntegerField(null=True, blank=True, db_index=True,
                                       help_text='软删除操作人 ID；恢复时清空')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    bio = models.CharField(max_length=500, null=True, blank=True, help_text='个人简介')
    wecom_userid = models.CharField(max_length=255, null=True, blank=True)
    wecom_bound_at = models.DateTimeField(null=True, blank=True)
    wecom_notify_enabled = models.BooleanField(default=True)
    wecom_notify_settings = models.TextField(null=True, blank=True)

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        db_table = 'users'
        ordering = ['id']
        indexes = [
            models.Index(fields=['department_id'], name='users_department_idx'),
            models.Index(fields=['role_id'], name='users_role_idx'),
            models.Index(fields=['wecom_userid'], name='users_wecom_userid_idx'),
        ]

    def __str__(self):
        return self.name or self.username

    # ---- 角色判断（对齐原 User.php isAdmin 等） ----
    @property
    def role_code(self):
        return self.role.code if self.role else None

    def has_role(self, code):
        return bool(self.role and self.role.code == code)

    def is_admin(self):
        return self.has_role('admin')

    def is_project_creator(self):
        return self.has_role('project_creator')

    def is_project_manager(self):
        return self.has_role('project_manager')

    def is_task_executor(self):
        return self.has_role('task_executor')

    def is_task_auditor(self):
        return self.has_role('task_auditor')

    def is_support(self):
        return self.has_role('support')

    # ---- 软删除操作 ----
    def delete(self, using=None, keep_parents=False):
        self.deleted_at = timezone.now()
        self.save(using=using, update_fields=['deleted_at'])

    def hard_delete(self, using=None, keep_parents=False):
        super().delete(using=using, keep_parents=keep_parents)

    def restore(self):
        self.deleted_at = None
        self.save(update_fields=['deleted_at'])


# ================= 组织 =================

class Role(models.Model):
    """roles 表。"""
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=255, unique=True)
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'roles'
        ordering = ['id']

    def __str__(self):
        return self.name


class Department(SoftDeleteModel):
    """departments 表：软删除（Bug A：避免 users.department_id 的 RESTRICT 外键冲突）。"""
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=255, unique=True)
    type = models.CharField(
        max_length=20, choices=[('project', 'project'), ('non-project', 'non-project')],
        default='project')
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'departments'
        ordering = ['sort_order', 'id']
        indexes = [
            models.Index(fields=['deleted_at'], name='departments_deleted_idx'),
        ]

    def __str__(self):
        return self.name


# ================= 状态机 =================

class Status(models.Model):
    """statuses 表（重构后：状态机类名 -> 自定义标签覆盖）。"""
    state_class = models.CharField(max_length=255, unique=True, help_text='状态机完整类名')
    custom_label = models.CharField(max_length=255, null=True, blank=True, help_text='自定义标签名，为空使用默认 label')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'statuses'
        ordering = ['id']

    def __str__(self):
        return self.custom_label or self.state_class


class StatusTransition(models.Model):
    """status_transitions 表：项目/任务状态流转记录。"""
    model_type = models.CharField(max_length=50)          # 'project' / 'task'
    model_id = models.BigIntegerField()
    from_status = models.CharField(max_length=255)
    to_status = models.CharField(max_length=255)
    user = models.ForeignKey('User', null=True, blank=True, on_delete=models.SET_NULL, db_column='user_id')
    note = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'status_transitions'
        ordering = ['-id']
        indexes = [
            models.Index(fields=['model_type', 'model_id'], name='st_model_idx'),
            models.Index(fields=['user_id'], name='st_user_idx'),
        ]


# ================= 任务类型 / 项目模板 =================

class TaskType(models.Model):
    """task_types 表。"""
    name = models.CharField(max_length=255)
    description = models.CharField(max_length=255, null=True, blank=True)
    department = models.ForeignKey(
        'Department', null=True, blank=True, on_delete=models.CASCADE, db_column='department_id')
    is_required = models.SmallIntegerField(default=0)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'task_types'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return self.name


class ProjectTemplate(SoftDeleteModel):
    """project_templates 表。"""
    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    user = models.ForeignKey('User', on_delete=models.PROTECT, db_column='user_id')  # 创建人
    # 软删除扩展字段
    deleted_by = models.BigIntegerField(null=True, blank=True, db_index=True,
                                       help_text='软删除操作人 ID；恢复时清空')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'project_templates'
        ordering = ['-created_at', '-id']

    def __str__(self):
        return self.name


class TemplateTask(models.Model):
    """template_tasks 表：模板任务配置。"""
    project_template = models.ForeignKey('ProjectTemplate', on_delete=models.CASCADE, db_column='project_template_id')
    task_type = models.ForeignKey('TaskType', on_delete=models.PROTECT, db_column='task_type_id')
    executor = models.ForeignKey('User', null=True, blank=True, on_delete=models.CASCADE, related_name='template_executor', db_column='executor_id')
    auditor = models.ForeignKey('User', null=True, blank=True, on_delete=models.CASCADE, related_name='template_auditor', db_column='auditor_id')
    department = models.ForeignKey('Department', null=True, blank=True, on_delete=models.PROTECT, db_column='department_id')
    days = models.IntegerField(default=5)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'template_tasks'
        ordering = ['sort_order', 'id']


class DefaultMember(models.Model):
    """default_members 表：模板任务的默认成员。"""
    department = models.ForeignKey('Department', on_delete=models.CASCADE, db_column='department_id')
    task_template = models.ForeignKey('ProjectTemplate', on_delete=models.CASCADE, db_column='task_template_id')
    executor = models.ForeignKey('User', on_delete=models.CASCADE, related_name='default_executor', db_column='executor_id')
    auditor = models.ForeignKey('User', on_delete=models.CASCADE, related_name='default_auditor', db_column='auditor_id')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'default_members'
        indexes = [
            models.Index(fields=['task_template_id'], name='dm_template_idx'),
        ]


# ================= 项目 / 任务 / 待办 =================

class Project(SoftDeleteModel):
    """projects 表。"""
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=255, unique=True)
    version = models.CharField(max_length=255, default='1.0')
    product_name = models.CharField(max_length=255)
    product_model = models.CharField(max_length=255)
    creator = models.ForeignKey('User', on_delete=models.CASCADE, related_name='created_projects', db_column='creator_id')
    start_date = models.DateField()
    end_date = models.DateField()
    # 存储状态机类名
    status = models.CharField(max_length=255, default='App\\States\\Project\\InProgress')
    progress = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    paused_reason = models.CharField(max_length=500, null=True, blank=True)
    paused_at = models.DateTimeField(null=True, blank=True)
    resumed_at = models.DateTimeField(null=True, blank=True)
    total_paused_days = models.IntegerField(default=0)
    # 软删除扩展字段：进入回收站用
    deleted_by = models.BigIntegerField(null=True, blank=True, db_index=True,
                                       help_text='软删除操作人 ID；恢复时清空')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'projects'
        ordering = ['-id']
        indexes = [
            models.Index(fields=['creator_id'], name='projects_creator_idx'),
            models.Index(fields=['created_at'], name='projects_created_idx'),
        ]

    def __str__(self):
        return self.name

    # 状态快捷属性（供模板判断）
    @property
    def status_key(self):
        return self.status.split('\\')[-1] if self.status else ''

    def save(self, *args, **kwargs):
        """保存时校验状态转换合法性 + 记录状态转换日志（对齐 ProjectState + Project.php booted）。"""
        old_status = None
        if self.pk:
            old_status = Project.all_objects.filter(pk=self.pk).values_list('status', flat=True).first()
        # 状态机白名单校验（对齐 ProjectState::config()）
        if old_status is not None and old_status != self.status:
            self._validate_status_transition(old_status, self.status)
        super().save(*args, **kwargs)
        if old_status is not None and old_status != self.status:
            StatusTransition.objects.create(
                model_type='project', model_id=self.id,
                from_status=old_status, to_status=self.status,
                # 操作人由 services/state_machine.py 的 project_transition() 注入
                user_id=getattr(self, '_transition_user', None),
            )
            self._transition_user = None

    # 状态机白名单（对齐 ProjectState::config()）
    _ALLOWED_TRANSITIONS = {
        'App\\States\\Project\\InProgress': {
            'App\\States\\Project\\Completed',
            'App\\States\\Project\\Overdue',
            'App\\States\\Project\\Suspended',
        },
        'App\\States\\Project\\Overdue': {
            'App\\States\\Project\\Completed',
            'App\\States\\Project\\OverdueCompleted',
            'App\\States\\Project\\Suspended',
        },
        'App\\States\\Project\\Suspended': {
            'App\\States\\Project\\InProgress',
            'App\\States\\Project\\Overdue',
        },
        'App\\States\\Project\\Completed': set(),
        'App\\States\\Project\\OverdueCompleted': set(),
        'App\\States\\Project\\NotStarted': set(),
    }

    @classmethod
    def _validate_status_transition(cls, from_status, to_status):
        """校验状态转换合法性；不在白名单时记录 warning（不 raise，避免破坏历史数据）。"""
        import logging
        allowed = cls._ALLOWED_TRANSITIONS.get(from_status)
        if allowed is None:
            # 未知源状态（如遗留数据）— 放行并记录
            logging.getLogger('pm').warning(
                'Project._validate_status_transition: unknown from_status=%r', from_status)
            return
        if to_status not in allowed:
            logging.getLogger('pm').warning(
                'Project illegal transition: %s -> %s (allowed: %s)',
                from_status, to_status, sorted(allowed))

    # ---- 统一操作权限判断（对齐 Project.php 单一可信源） ----
    def is_inactive(self):
        """项目是否处于已暂停状态（该状态下禁止任务操作）。"""
        return self.status == 'App\\States\\Project\\Suspended'

    def _is_creator_or_admin(self, user):
        """对齐 Project.php canPause/canResume/canChange/update/delete 的角色判断：
        仅项目创建人本人或 admin 可操作；其它角色（项目经理/执行人/审核人/支持）一律无权。"""
        if user is None or not getattr(user, 'is_authenticated', False):
            return False
        return self.creator_id == user.id or user.is_admin()

    def can_pause(self, user=None):
        """只有进行中、逾期可以暂停（创建人或管理员）。"""
        if not self._is_creator_or_admin(user):
            return False
        allowed = ['App\\States\\Project\\InProgress', 'App\\States\\Project\\Overdue']
        return self.status in allowed

    def can_resume(self, user=None):
        """只有已暂停可以恢复（创建人或管理员）。"""
        if not self._is_creator_or_admin(user):
            return False
        return self.status == 'App\\States\\Project\\Suspended'

    def can_change(self, user=None):
        """非终态即可变更（创建人或管理员）。"""
        if not self._is_creator_or_admin(user):
            return False
        finished = ['App\\States\\Project\\Completed', 'App\\States\\Project\\OverdueCompleted']
        return self.status not in finished

    def can_update(self, user=None):
        """编辑/更新/删除项目：创建人或管理员（对齐 ProjectPolicy::update/delete）。"""
        return self._is_creator_or_admin(user)

    def calculate_progress(self):
        from .models import Task
        total = self.tasks.count()
        completed = self.tasks.filter(
            status__in=['App\\States\\Task\\Completed', 'App\\States\\Task\\OverdueCompleted']).count()
        self.progress = round((completed / total) * 100, 2) if total > 0 else 0
        self.save(update_fields=['progress'])
        if total > 0 and completed == total:
            # 自动状态转换：仅当转换合法时才修改（状态机白名单保护）
            if self.status == 'App\\States\\Project\\Overdue':
                new_status = 'App\\States\\Project\\OverdueCompleted'
                if 'App\\States\\Project\\OverdueCompleted' in self._ALLOWED_TRANSITIONS.get(self.status, set()):
                    self.status = new_status
                    self.save()
            elif self.status == 'App\\States\\Project\\InProgress':
                if 'App\\States\\Project\\Completed' in self._ALLOWED_TRANSITIONS.get(self.status, set()):
                    self.status = 'App\\States\\Project\\Completed'
                    self.save()
            # 其他状态（Suspended/Completed/OverdueCompleted/NotStarted）保持不变
        return self.progress

    def get_department_progress(self, department_id):
        tasks = self.tasks.filter(department_id=department_id)
        total = tasks.count()
        completed = tasks.filter(status='App\\States\\Task\\Completed').count()
        return round((completed / total) * 100, 2) if total > 0 else 0

    def participating_departments(self):
        dept_ids = list(self.tasks.values_list('department_id', flat=True).distinct())
        return Department.objects.filter(id__in=dept_ids).order_by('sort_order', 'id')

    def participants(self):
        executor_ids = self.tasks.values_list('executor_id', flat=True)
        auditor_ids = self.tasks.values_list('auditor_id', flat=True)
        user_ids = set(executor_ids) | set(auditor_ids) | {self.creator_id}
        return User.objects.filter(id__in=user_ids)

    def members(self):
        return self.participants()


class Task(SoftDeleteModel):
    """tasks 表。"""

    # 任务状态常量（对齐 App\States\Task\*），供权限判断与归档口径复用
    T_PENDING = 'App\\States\\Task\\Pending'
    T_IN_PROGRESS = 'App\\States\\Task\\InProgress'
    T_FOR_AUDIT = 'App\\States\\Task\\ForAudit'
    T_COMPLETED = 'App\\States\\Task\\Completed'
    T_OVERDUE_COMPLETED = 'App\\States\\Task\\OverdueCompleted'
    T_REJECTED = 'App\\States\\Task\\Rejected'
    T_OVERDUE = 'App\\States\\Task\\Overdue'
    project = models.ForeignKey('Project', on_delete=models.CASCADE, related_name='tasks', db_column='project_id')
    department = models.ForeignKey('Department', on_delete=models.CASCADE, related_name='tasks', db_column='department_id')
    name = models.CharField(max_length=255)
    executor = models.ForeignKey('User', on_delete=models.CASCADE, related_name='executed_tasks', db_column='executor_id')
    auditor = models.ForeignKey('User', on_delete=models.CASCADE, related_name='audited_tasks', db_column='auditor_id')
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=255, default='App\\States\\Task\\Pending')
    task_type = models.ForeignKey('TaskType', null=True, blank=True, on_delete=models.PROTECT, db_column='task_type_id')
    is_overdue = models.BooleanField(default=False)
    extension_days = models.IntegerField(default=0)
    paused_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # 软删除扩展字段
    deleted_by = models.BigIntegerField(null=True, blank=True, db_index=True,
                                       help_text='软删除操作人 ID；恢复时清空')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks'
        ordering = ['-id']
        indexes = [
            models.Index(fields=['project_id', 'status'], name='tasks_project_status_idx'),
            models.Index(fields=['executor_id', 'status'], name='tasks_executor_status_idx'),
            models.Index(fields=['auditor_id', 'status'], name='tasks_auditor_status_idx'),
        ]

    def __str__(self):
        return self.name

    @property
    def status_key(self):
        return self.status.split('\\')[-1] if self.status else ''

    def save(self, *args, **kwargs):
        """保存时若 status 变化则自动记录状态转换日志（对齐 Task.php booted saved）。"""
        old_status = None
        if self.pk:
            old_status = Task.all_objects.filter(pk=self.pk).values_list('status', flat=True).first()
        super().save(*args, **kwargs)
        if old_status is not None and old_status != self.status:
            StatusTransition.objects.create(
                model_type='task', model_id=self.id,
                from_status=old_status, to_status=self.status,
                # 操作人由 services/state_machine.py 的 task_transition() 注入
                user_id=getattr(self, '_transition_user', None),
            )
            self._transition_user = None

    # ---- 业务操作（对齐 Task.php） ----
    def check_overdue(self):
        from datetime import date
        if not self.is_overdue and self.end_date and self.end_date < date.today() \
                and self.status != 'App\\States\\Task\\Completed':
            self.is_overdue = True
            self.status = 'App\\States\\Task\\Overdue'
            self.save()
            return True
        return False

    def extend_days(self, days):
        from datetime import timedelta
        if self.end_date:
            self.end_date = self.end_date + timedelta(days=days)
        self.extension_days += days
        self.is_overdue = False
        self.save()
        # 逾期 → 等待中：Pending 钩子给执行人重建待办（对齐 Laravel Task.php:116）
        if self.status == 'App\\States\\Task\\Overdue':
            from .services.state_machine import T_PENDING, task_transition
            task_transition(self, T_PENDING)

    # ---- 统一操作权限判断（对齐 Task.php 单一可信源） ----
    def _task_forbidden(self):
        return ['App\\States\\Task\\Completed', 'App\\States\\Task\\OverdueCompleted', 'App\\States\\Task\\ForAudit']

    @classmethod
    def _task_auditable(cls):
        """可审核状态（对齐 Task.php:204）：待审核 + 逾期待审核。"""
        return ['App\\States\\Task\\ForAudit', 'App\\States\\Task\\Overdue']

    @classmethod
    def _task_finished(cls):
        """已完成状态（对齐 Task.php:263）：正常完成 + 延期完成 + 逾期（逾期任务同样可重传文件）。"""
        return ['App\\States\\Task\\Completed', 'App\\States\\Task\\OverdueCompleted',
                'App\\States\\Task\\Overdue']

    def can_upload_file(self, user_id=None):
        if not user_id:
            return False
        if not self.project or user_id != self.executor_id:
            return False
        if self.project.is_inactive():
            return False
        # 已有待审核文件时不再重复上传（含已跳过截止日期的逾期任务），避免产生多份待审文件
        if self.has_pending_file():
            return False
        # 已完成（含逾期完成）：已有已通过文件时走「更新」流程；
        # 没有已通过文件（异常数据）时仍允许上传，避免任务彻底卡死。
        if self.status in (Task.T_COMPLETED, Task.T_OVERDUE_COMPLETED):
            return not self.files.filter(status='approved').exists()
        return True

    def can_apply_extension(self, user_id=None):
        if not user_id:
            return False
        if not self.project or user_id != self.executor_id:
            return False
        if self.project.is_inactive():
            return False
        return self.status not in self._task_forbidden()

    def can_audit(self, user_id=None):
        if not user_id:
            return False
        if not self.project:
            return False
        if user_id == self.executor_id:
            return False
        if user_id != self.auditor_id:
            return False
        if self.project.is_inactive():
            return False
        # 对齐 Task.php:204 的可审核状态集合：待审核 + 逾期待审核
        if self.status not in self._task_auditable():
            return False
        return self.has_pending_file()

    def can_update_file(self, user_id=None, is_admin=False):
        if not user_id:
            return False
        if not self.project:
            return False
        allowed_user = user_id == self.executor_id or user_id == self.project.creator_id or is_admin
        if not allowed_user:
            return False
        if self.project.is_inactive():
            return False
        if self.status not in self._task_finished():
            return False
        return self.files.filter(status='approved').exists()

    def has_pending_file(self):
        return self.files.filter(status='pending').exists()

    def pending_file(self):
        return self.files.filter(status='pending').order_by('-id').first()

    def approved_file(self):
        return self.files.filter(status='approved').order_by('-id').first()

    def current_file(self):
        return self.files.filter(status='approved').order_by('-id').first()


class Todo(models.Model):
    """todos 表。"""
    user = models.ForeignKey('User', on_delete=models.CASCADE, related_name='todos', db_column='user_id')
    type = models.CharField(max_length=255)
    title = models.CharField(max_length=255)
    related_type = models.CharField(max_length=255, null=True, blank=True)
    related_id = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=[('pending', 'pending'), ('completed', 'completed')], default='pending')
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'todos'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['user_id'], name='todos_user_idx'),
            models.Index(fields=['related_type', 'related_id'], name='todos_related_idx'),
        ]

    def complete(self):
        from django.utils import timezone
        self.status = 'completed'
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'completed_at', 'updated_at'])


class ExtensionApplication(models.Model):
    """extension_applications 表：任务延期申请。"""
    task = models.ForeignKey('Task', on_delete=models.CASCADE, related_name='extension_applications', db_column='task_id')
    applicant = models.ForeignKey('User', on_delete=models.CASCADE, related_name='extension_applications', db_column='applicant_id')
    approver = models.ForeignKey('User', on_delete=models.CASCADE, related_name='approved_applications', db_column='approver_id')
    days = models.IntegerField()
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=[('pending', 'pending'), ('approved', 'approved'), ('rejected', 'rejected')], default='pending')
    approved_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'extension_applications'
        ordering = ['-id']
        indexes = [
            models.Index(fields=['task_id'], name='ext_app_task_idx'),
            models.Index(fields=['applicant_id'], name='ext_app_applicant_idx'),
            models.Index(fields=['approver_id'], name='ext_app_approver_idx'),
            models.Index(fields=['status'], name='ext_app_status_idx'),
        ]


# ================= 文件 =================

class File(SoftDeleteModel):
    """files 表：任务文件。"""
    task = models.ForeignKey('Task', on_delete=models.CASCADE, related_name='files', db_column='task_id')
    uploader = models.ForeignKey('User', on_delete=models.CASCADE, related_name='uploaded_files', db_column='uploader_id')
    auditor = models.ForeignKey('User', null=True, blank=True, on_delete=models.CASCADE, related_name='audited_files', db_column='auditor_id')
    name = models.CharField(max_length=255)
    original_name = models.CharField(max_length=255)
    path = models.CharField(max_length=255)
    size = models.BigIntegerField()
    mime_type = models.CharField(max_length=255)
    version = models.IntegerField(default=1)
    status = models.CharField(max_length=20, choices=[('pending', 'pending'), ('approved', 'approved'), ('rejected', 'rejected')], default='pending')
    audit_time = models.DateTimeField(null=True, blank=True)
    audit_reason = models.TextField(null=True, blank=True)
    # 软删除扩展字段
    deleted_by = models.BigIntegerField(null=True, blank=True, db_index=True,
                                       help_text='软删除操作人 ID；恢复时清空')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'files'
        ordering = ['-id']
        indexes = [
            models.Index(fields=['task_id', 'status'], name='files_task_status_idx'),
        ]

    def __str__(self):
        return self.name

    def can_preview(self, user_id):
        """预览权限：上传人/审核人/项目创建人/项目总经理。"""
        if self.uploader_id == user_id:
            return True
        if self.auditor_id == user_id:
            return True
        try:
            if self.task_id:
                task = self.task
                if task and task.project_id:
                    if task.project.creator_id == user_id:
                        return True
        except Exception:
            pass
        try:
            user = User.objects.filter(pk=user_id).first()
            if user and user.is_project_manager():
                return True
        except Exception:
            pass
        return False

    def can_download(self, user_id):
        """下载权限：比预览更严格 —— 上传人 / 项目创建人 / 项目总经理直接放行，
        其余用户必须是项目成员且文件已审核通过。

        两点说明（都已在参考目录的基础上修正，故刻意与之不同）：
        1) 参考 File.php:140 写的是 `status !== 'accepted'`，但 files 表的枚举只有
           pending/approved/rejected，'accepted' 永远不成立，等价于「项目成员一律
           下载不了」。这里按实际枚举取 'approved'。
        2) 参考在同方法末尾还有一段「项目成员」校验（File.php:144-159）；旧移植版本
           把这段漏掉了，落到 `return True`，等于任何登录用户都能下载任意 approved
           文件。这里补回成员校验。
        """
        if self.uploader_id == user_id:
            return True
        try:
            if self.task_id:
                task = self.task
                if task and task.project_id:
                    if task.project.creator_id == user_id:
                        return True
        except Exception:
            return False
        try:
            user = User.objects.filter(pk=user_id).first()
            if user and user.is_project_manager():
                return True
        except Exception:
            return False

        # 其他项目成员：只有审核通过的文件才能下载
        if self.status != 'approved':
            return False

        try:
            if not self.task_id:
                return False
            task = self.task
            if not (task and task.project_id):
                return False
            project = task.project
            return bool(project and project.members().filter(id=user_id).exists())
        except Exception:
            return False


class FileVersion(models.Model):
    """file_versions 表：任务文件历史版本快照（独立物理文件）。

    改造要点（与 TrashService::pruneFileHistory 配合）：
      - task_id 必填，任务删除时由数据库外键 CASCADE 自动清理记录；
      - file_id 改为 SET NULL —— 原文件记录被删除后，版本记录依然保留，
        快照文件仍可独立下载，用于事后追溯。
    """
    file = models.ForeignKey(
        'File', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='versions', db_column='file_id')
    task = models.ForeignKey(
        'Task', null=True, blank=True, on_delete=models.CASCADE,
        related_name='file_versions', db_column='task_id')
    version = models.IntegerField()
    path = models.CharField(max_length=255, null=True, blank=True)
    uploader = models.ForeignKey('User', on_delete=models.CASCADE, related_name='file_versions', db_column='uploader_id')
    auditor = models.ForeignKey('User', null=True, blank=True, on_delete=models.CASCADE, related_name='audited_versions', db_column='auditor_id')
    status = models.CharField(max_length=20, choices=[('pending', 'pending'), ('approved', 'approved'), ('rejected', 'rejected')], default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'file_versions'
        ordering = ['-id']
        indexes = [
            models.Index(fields=['task_id', 'created_at'], name='fv_task_created_idx'),
        ]


class DepartmentFile(SoftDeleteModel):
    """department_files 表：部门资料文件。"""
    department = models.ForeignKey('Department', on_delete=models.CASCADE, related_name='department_files', db_column='department_id')
    uploader = models.ForeignKey('User', on_delete=models.PROTECT, related_name='department_files', db_column='uploader_id')
    name = models.CharField(max_length=255)
    original_name = models.CharField(max_length=255)
    path = models.CharField(max_length=255)
    size = models.BigIntegerField()
    mime_type = models.CharField(max_length=255)
    category = models.CharField(max_length=50, default='template')
    visibility = models.CharField(max_length=20, default='all')
    version = models.IntegerField(default=1)
    # 软删除扩展字段：进入回收站用
    deleted_by = models.BigIntegerField(null=True, blank=True, db_index=True,
                                       help_text='软删除操作人 ID；恢复时清空')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'department_files'
        ordering = ['-created_at', '-id']

    def can_access(self, user_id):
        user = User.objects.filter(pk=user_id).first()
        if not user:
            return False
        if self.visibility == 'all':
            return True
        if self.visibility == 'department':
            return user.department_id == self.department_id
        return self.visibility == 'private' and self.uploader_id == user.id

    @staticmethod
    def category_options():
        return {
            'template': '模板文件',
            'example': '范例文件',
            'product_spec': '产品规格书',
            'component_spec': '元器件规格书',
            'material': '物料文件',
            'safety': '安规标准文件',
            'certification': '认证文件',
            'patent': '专利文件',
        }

    @staticmethod
    def visibility_options():
        return {
            'all': '全部用户可见',
            'department': '本部门用户可见',
            'private': '仅本人可见',
        }

    @property
    def category_name(self):
        return self.category_options().get(self.category, '未知')

    @property
    def visibility_name(self):
        return self.visibility_options().get(self.visibility, '未知')

    @property
    def formatted_size(self):
        units = ['B', 'KB', 'MB', 'GB']
        size = self.size
        unit = 0
        while size >= 1024 and unit < len(units) - 1:
            size /= 1024
            unit += 1
        return '%.2f %s' % (size, units[unit])


# ================= 通知 / 日志 / 消息 =================

class Notification(SoftDeleteModel):
    """notifications 表。"""
    user = models.ForeignKey('User', on_delete=models.CASCADE, related_name='notifications', db_column='user_id')
    sender = models.ForeignKey('User', null=True, blank=True, on_delete=models.CASCADE, related_name='sent_notifications', db_column='sender_id')
    type = models.CharField(max_length=255)
    title = models.CharField(max_length=255)
    content = models.TextField(null=True, blank=True)
    related_type = models.CharField(max_length=255, null=True, blank=True)
    related_id = models.BigIntegerField(null=True, blank=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    # 软删除扩展字段：进入回收站用
    deleted_by = models.BigIntegerField(null=True, blank=True, db_index=True,
                                       help_text='软删除操作人 ID；恢复时清空')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'notifications'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['user_id', 'is_read'], name='notif_user_read_idx'),
            models.Index(fields=['user_id', 'created_at'], name='notif_user_created_idx'),
        ]

    def mark_as_read(self):
        from django.utils import timezone
        if not self.is_read:
            self.is_read = True
            self.read_at = timezone.now()
            self.save(update_fields=['is_read', 'read_at', 'updated_at'])


class OperationLog(models.Model):
    """operation_logs 表。"""
    user = models.ForeignKey('User', null=True, blank=True, on_delete=models.PROTECT, db_column='user_id')
    ip = models.CharField(max_length=45, null=True, blank=True)
    module = models.CharField(max_length=255)
    action = models.CharField(max_length=255)
    content = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'operation_logs'
        ordering = ['-created_at', '-id']


class ProjectLog(models.Model):
    """project_logs 表：项目履历。"""
    project = models.ForeignKey('Project', on_delete=models.CASCADE, related_name='logs', db_column='project_id')
    user = models.ForeignKey('User', on_delete=models.CASCADE, related_name='project_logs', db_column='user_id')
    action = models.CharField(max_length=255)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'project_logs'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['project_id'], name='plog_project_idx'),
            models.Index(fields=['user_id'], name='plog_user_idx'),
        ]


class Message(SoftDeleteModel):
    """messages 表：群聊 + 私聊。"""
    project = models.ForeignKey('Project', on_delete=models.CASCADE, related_name='messages', db_column='project_id')
    user = models.ForeignKey('User', on_delete=models.CASCADE, related_name='messages', db_column='user_id')
    receiver = models.ForeignKey('User', null=True, blank=True, on_delete=models.CASCADE, related_name='received_messages', db_column='receiver_id')
    is_private = models.BooleanField(default=False)
    content = models.TextField(null=True, blank=True)
    file_path = models.CharField(max_length=255, null=True, blank=True)
    file_name = models.CharField(max_length=255, null=True, blank=True)
    # 软删除扩展字段
    deleted_by = models.BigIntegerField(null=True, blank=True, db_index=True,
                                       help_text='软删除操作人 ID；恢复时清空')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'messages'
        # 群聊/私聊都按时间正序（最早的在上，最新在最下），
        # 符合"对话从前往后读"的自然阅读顺序；与回收站/通知相反。
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['project_id', 'created_at'], name='messages_project_created_idx'),
        ]


class ChatClearState(models.Model):
    """chat_clear_states 表：聊天清空状态。"""
    user = models.ForeignKey('User', on_delete=models.CASCADE, related_name='chat_clear_states', db_column='user_id')
    project = models.ForeignKey('Project', on_delete=models.CASCADE, related_name='chat_clear_states', db_column='project_id')
    chat_type = models.CharField(max_length=20)
    other_user = models.ForeignKey('User', null=True, blank=True, on_delete=models.CASCADE, related_name='chat_clear_others', db_column='other_user_id')
    conversation_key = models.CharField(max_length=64)
    cleared_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'chat_clear_states'
        constraints = [
            models.UniqueConstraint(
                fields=['user_id', 'project_id', 'chat_type', 'conversation_key'],
                name='chat_clear_unique_conversation'),
        ]


# ================= 知识库 =================

class KnowledgeCategory(models.Model):
    """knowledge_categories 表。"""
    pid = models.BigIntegerField(default=0)
    sort = models.PositiveIntegerField(default=0)
    title = models.CharField(max_length=100)
    desc = models.CharField(max_length=255, null=True, blank=True)
    status = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'knowledge_categories'
        ordering = ['sort', 'id']


class Knowledge(SoftDeleteModel):
    """knowledges 表。"""
    admin_id = models.BigIntegerField()
    cate_id = models.BigIntegerField(null=True, blank=True)
    title = models.CharField(max_length=200)
    desc = models.CharField(max_length=500, null=True, blank=True)
    is_share = models.PositiveSmallIntegerField(default=1)
    read = models.PositiveIntegerField(default=0)
    sort = models.PositiveIntegerField(default=0)
    # 软删除扩展字段
    deleted_by = models.BigIntegerField(null=True, blank=True, db_index=True,
                                       help_text='软删除操作人 ID；恢复时清空')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'knowledges'
        ordering = ['-sort', '-id']

    def is_visible_to(self, user_id):
        return self.is_share == 1 or self.admin_id == user_id

    def is_editable_by(self, user_id):
        return self.admin_id == user_id


class KnowledgeDoc(SoftDeleteModel):
    """knowledge_docs 表。"""
    TYPE_DOC = 1
    TYPE_CHAPTER = 2
    TYPE_LINK = 3

    @staticmethod
    def type_label(t):
        return {1: '文档', 2: '章节', 3: '链接'}.get(t, '未知')

    knowledge = models.ForeignKey('Knowledge', on_delete=models.CASCADE, related_name='docs', db_column='knowledge_id')
    pid = models.BigIntegerField(default=0)
    type = models.PositiveSmallIntegerField(default=1)
    title = models.CharField(max_length=200)
    md_content = models.TextField(null=True, blank=True)
    content = models.TextField(null=True, blank=True)
    link = models.CharField(max_length=500, null=True, blank=True)
    file_ids = models.CharField(max_length=500, null=True, blank=True)
    read = models.PositiveIntegerField(default=0)
    sort = models.PositiveIntegerField(default=0)
    # 软删除扩展字段
    deleted_by = models.BigIntegerField(null=True, blank=True, db_index=True,
                                       help_text='软删除操作人 ID；恢复时清空')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'knowledge_docs'
        ordering = ['sort', 'id']
        indexes = [
            models.Index(fields=['knowledge_id', 'pid'], name='kdoc_knowledge_pid_idx'),
        ]


# ================= 协同办公（OnlyOffice） =================

class CollabDocument(SoftDeleteModel):
    """collab_documents 表。"""
    project = models.ForeignKey('Project', on_delete=models.CASCADE, related_name='collab_documents', db_column='project_id')
    name = models.CharField(max_length=255, help_text='文档显示名（不含扩展名）')
    extension = models.CharField(max_length=10, help_text='docx/xlsx/pptx')
    storage_path = models.CharField(max_length=255)
    doc_key = models.CharField(max_length=255, help_text='OnlyOffice 文档 key')
    is_temp_demo = models.BooleanField(default=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey('User', null=True, blank=True, on_delete=models.SET_NULL, related_name='created_docs', db_column='created_by')
    updated_by = models.ForeignKey('User', null=True, blank=True, on_delete=models.SET_NULL, related_name='updated_docs', db_column='updated_by')
    # 软删除扩展字段
    deleted_by = models.BigIntegerField(null=True, blank=True, db_index=True,
                                       help_text='软删除操作人 ID；恢复时清空')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'collab_documents'
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(fields=['project_id', 'deleted_at'], name='collab_docs_project_idx'),
        ]

    def refresh_doc_key(self):
        self.doc_key = hashlib.md5(uuid.uuid4().hex.encode()).hexdigest()
        return self.doc_key

    @property
    def display_size(self):
        s = self.size
        if s < 1024:
            return f'{s} B'
        if s < 1024 ** 2:
            return f'{round(s / 1024, 2)} KB'
        if s < 1024 ** 3:
            return f'{round(s / (1024 ** 2), 2)} MB'
        return f'{round(s / (1024 ** 3), 2)} GB'

    @property
    def creator(self):
        return self.created_by

    @property
    def updater(self):
        return self.updated_by


# ================= 语音会议 =================

class VoiceRoom(models.Model):
    """voice_rooms 表：一个项目对应一个会议房。"""
    project = models.OneToOneField('Project', on_delete=models.CASCADE, related_name='voice_room', db_column='project_id')
    host = models.ForeignKey('User', on_delete=models.PROTECT, related_name='voice_host_rooms', db_column='host_id')
    current_presenter = models.ForeignKey('User', null=True, blank=True, on_delete=models.SET_NULL, related_name='voice_presenter_rooms', db_column='current_presenter_id')
    janus_room_id = models.BigIntegerField(null=True, blank=True)
    active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'voice_rooms'
        ordering = ['id']

    @classmethod
    def for_project(cls, project):
        room, _ = cls.objects.get_or_create(project_id=project.id,
                                            defaults={'host_id': project.creator_id})
        return room


class VoiceSession(models.Model):
    """voice_sessions 表。"""
    voice_room = models.ForeignKey('VoiceRoom', on_delete=models.CASCADE, related_name='sessions', db_column='voice_room_id')
    started_by = models.ForeignKey('User', on_delete=models.PROTECT, related_name='voice_sessions', db_column='started_by')
    collab_document = models.ForeignKey('CollabDocument', null=True, blank=True, on_delete=models.SET_NULL, related_name='voice_sessions', db_column='collab_document_id')
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'voice_sessions'
        ordering = ['-id']
        indexes = [
            models.Index(fields=['voice_room_id', 'started_at'], name='voice_sessions_room_idx'),
        ]


class VoiceParticipant(models.Model):
    """voice_participants 表。"""
    voice_session = models.ForeignKey('VoiceSession', on_delete=models.CASCADE, related_name='participants', db_column='voice_session_id')
    user = models.ForeignKey('User', on_delete=models.CASCADE, related_name='voice_participations', db_column='user_id')
    joined_at = models.DateTimeField()
    left_at = models.DateTimeField(null=True, blank=True)
    was_presenter = models.BooleanField(default=False)
    leave_reason = models.CharField(max_length=30, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'voice_participants'
        ordering = ['id']
        indexes = [
            models.Index(fields=['voice_session_id', 'user_id'], name='voice_participants_session_idx'),
        ]


# ================= 企业微信 =================

class WecomConfig(models.Model):
    """wecom_configs 表。"""
    corp_id = models.CharField(max_length=64)
    agent_id = models.CharField(max_length=32)
    secret = models.CharField(max_length=128)
    token = models.CharField(max_length=64)
    encoding_aes_key = models.CharField(max_length=64)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wecom_configs'
        ordering = ['id']


class WecomMessageLog(models.Model):
    """wecom_message_logs 表。"""
    user = models.ForeignKey('User', on_delete=models.CASCADE, related_name='wecom_message_logs', db_column='user_id')
    msg_type = models.CharField(max_length=32, default='textcard')
    title = models.CharField(max_length=255, null=True, blank=True)
    content = models.TextField(null=True, blank=True)
    response_code = models.CharField(max_length=16, null=True, blank=True)
    response_body = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wecom_message_logs'
        ordering = ['-id']
        indexes = [
            models.Index(fields=['user_id', 'created_at'], name='wecom_msglog_user_created_idx'),
        ]

    def is_success(self):
        """是否发送成功（对齐 WecomMessageLog::isSuccess）。"""
        return str(self.response_code or '').strip() == '0'


class WecomOauthState(models.Model):
    """wecom_oauth_states 表。"""
    state = models.CharField(max_length=128, unique=True)
    redirect_url = models.CharField(max_length=255, null=True, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'wecom_oauth_states'
        ordering = ['-id']


class WecomFile(SoftDeleteModel):
    """wecom_files 表：企微发来的文件。"""
    user = models.ForeignKey('User', null=True, blank=True, on_delete=models.SET_NULL, related_name='wecom_files', db_column='user_id')
    media_id = models.CharField(max_length=128)
    msg_type = models.CharField(max_length=20, default='file')
    original_name = models.CharField(max_length=512, null=True, blank=True)
    stored_name = models.CharField(max_length=255)
    file_path = models.CharField(max_length=255)
    file_size = models.PositiveBigIntegerField(default=0)
    mime_type = models.CharField(max_length=128, null=True, blank=True)
    status = models.CharField(max_length=20, default='pending')
    remark = models.CharField(max_length=255, null=True, blank=True)
    # nullableMorphs：关联到任务或项目
    related_type = models.CharField(max_length=255, null=True, blank=True)
    related_id = models.BigIntegerField(null=True, blank=True)
    callback_data = models.JSONField(null=True, blank=True)
    # 软删除扩展字段
    deleted_by = models.BigIntegerField(null=True, blank=True, db_index=True,
                                       help_text='软删除操作人 ID；恢复时清空')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wecom_files'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['status'], name='wecom_file_status_idx'),
            models.Index(fields=['media_id'], name='wecom_file_media_idx'),
        ]


# ================= AI 智能助手（对齐参考 app/Models/Ai*.php） =================

class AiSetting(models.Model):
    """ai_settings 表：AI 全局配置（单行，id=1）。"""
    PROVIDERS = {
        'deepseek': {
            'name': 'DeepSeek（深度求索）',
            'base_url': 'https://api.deepseek.com/v1',
            'lab': 'deepseek',
            'price': '输入 ¥1/百万token · 输出 ¥2/百万token',
            'free_quota': '新用户注册送 ¥10 额度',
            'best_for': '性价比最高，推荐首选',
            'models': {
                'deepseek-chat': 'DeepSeek-V3（通用对话）',
                'deepseek-reasoner': 'DeepSeek-R1（推理模型）',
            },
            'docs': 'https://platform.deepseek.com/api_keys',
        },
        'qwen': {
            'name': '通义千问（阿里云）',
            'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
            'lab': 'openai-compatible',
            'price': 'Turbo ¥0.3/百万 · Plus ¥0.8/百万 · Max ¥2/百万',
            'free_quota': '新用户送 100万 token 免费额度',
            'best_for': '阿里云生态企业，稳定性好',
            'models': {
                'qwen-turbo': 'Qwen-Turbo（快速低成本）',
                'qwen-plus': 'Qwen-Plus（均衡）',
                'qwen-max': 'Qwen-Max（最强）',
            },
            'docs': 'https://dashscope.console.aliyun.com/apiKey',
        },
        'doubao': {
            'name': '豆包（字节跳动）',
            'base_url': 'https://ark.cn-beijing.volces.com/api/v3',
            'lab': 'openai-compatible',
            'price': 'Pro-4k ¥0.8/百万 · Pro-32k ¥2/百万',
            'free_quota': '新用户送 50万 token 体验额度',
            'best_for': '火山引擎生态，视觉能力强',
            'models': {
                'doubao-pro-4k': 'Doubao-Pro-4k',
                'doubao-pro-32k': 'Doubao-Pro-32k',
                'doubao-pro-128k': 'Doubao-Pro-128k',
            },
            'docs': 'https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey',
        },
        'hunyuan': {
            'name': '腾讯混元',
            'base_url': 'https://api.hunyuan.cloud.tencent.com/v1',
            'lab': 'openai-compatible',
            'price': 'Lite 免费 · Pro ¥2/百万 · Standard ¥0.5/百万',
            'free_quota': 'Hunyuan-Lite 永久免费调用',
            'best_for': '腾讯云生态，有免费模型可用',
            'models': {
                'hunyuan-lite': 'Hunyuan-Lite（免费）',
                'hunyuan-pro': 'Hunyuan-Pro',
                'hunyuan-standard': 'Hunyuan-Standard',
            },
            'docs': 'https://console.cloud.tencent.com/hunyuan/api-key',
        },
    }

    provider = models.CharField(max_length=40, default='deepseek')
    api_key = models.CharField(max_length=500, null=True, blank=True)
    base_url = models.CharField(max_length=500, null=True, blank=True)
    model = models.CharField(max_length=100, default='deepseek-chat')
    vision_enabled = models.BooleanField(default=False)
    vision_model = models.CharField(max_length=100, null=True, blank=True)
    temperature = models.DecimalField(max_digits=3, decimal_places=2, default=0.70)
    max_tokens = models.IntegerField(default=2000)
    enabled = models.BooleanField(default=False)
    rate_limit_minute = models.PositiveIntegerField(default=30)
    rate_limit_daily = models.PositiveIntegerField(default=200)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ai_settings'

    def __str__(self):
        return f'AiSetting({self.provider}/{self.model})'

    @classmethod
    def current(cls):
        """获取当前 AI 配置（单行，id=1，不存在则建默认行）。"""
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                'provider': 'deepseek',
                'base_url': 'https://api.deepseek.com/v1',
                'model': 'deepseek-chat',
                'temperature': 0.70,
                'max_tokens': 2000,
            },
        )
        return obj

    def has_real_api_key(self):
        return bool(self.api_key) and not str(self.api_key).startswith('sk-xxxx')

    def is_available(self):
        """是否已启用且可用。

        兼容多供应商：任意一家「已启用 + 已填真实 Key」的供应商也算可用
        （对齐 AiSetting::isAvailable 的 Schema::hasTable 回退分支）。
        """
        if self.has_real_api_key():
            return bool(self.enabled)
        try:
            from django.db import connection
            if 'ai_providers' in connection.introspection.table_names():
                if AiProvider.objects.filter(enabled=True).exclude(
                        api_key__isnull=True).exclude(api_key='').exists():
                    return True
        except Exception:
            pass
        return False

    def masked_api_key(self):
        return _mask_api_key(self.api_key)

    def provider_info(self):
        return self.PROVIDERS.get(self.provider, {})


class AiDocTemplate(models.Model):
    """ai_doc_templates 表：文档生成模板（模板/范例/支援文件/生成指引）。"""
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=80, default='通用')
    description = models.TextField(null=True, blank=True)
    template_path = models.CharField(max_length=300, null=True, blank=True)
    example_path = models.CharField(max_length=300, null=True, blank=True)
    support_files = models.JSONField(null=True, blank=True, default=list)
    prompt_guide = models.TextField(null=True, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ai_doc_templates'
        ordering = ['category', 'id']

    def __str__(self):
        return self.name


class AiSkill(models.Model):
    """ai_skills 表：PM 专业服务能力注册表。"""
    name = models.CharField(max_length=100)
    slug = models.CharField(max_length=100, unique=True)
    description = models.TextField(null=True, blank=True)
    type = models.CharField(max_length=30, default='tool')  # tool|agent|doc-generator
    config = models.JSONField(null=True, blank=True, default=dict)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ai_skills'

    def __str__(self):
        return self.slug


class AiRateCounter(models.Model):
    """AI 每用户配额计数 + 文档生成在途锁（DB 实现，跨 gunicorn worker 一致）。

    key 约定：
      - `u:{user_id}:minute` / `u:{user_id}:daily`：窗口计数
      - `docgen:{user_id}`：文档生成在途锁（count=1 表示占用）
    """
    key = models.CharField(max_length=120, primary_key=True)
    count = models.IntegerField(default=0)
    reset_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ai_rate_counters'


# ==================== AI 多供应商 / 多模型 / 多会话 ====================
# 对齐参考 app/Models/{AiProvider,AiModel,AiConversation,AiMessage,AiAttachment}.php。
# AiSetting 仍保留为「旧版单供应商」字段，两家表通过 sync_to_legacy_setting() 双向同步，
# 保证既有多供应商能力、又不破坏依赖 ai_settings 的旧代码。


def _mask_api_key(api_key):
    """脱敏 API Key：保留前 6 位与后 4 位（对齐 AiSetting::maskedApiKey）。"""
    if not api_key or str(api_key).startswith('sk-xxxx'):
        return None
    key = str(api_key)
    length = len(key)
    head = key[:6]
    tail = key[-4:] if length > 10 else ''
    return head + '*' * max(8, length - len(head) - len(tail)) + tail


class AiProvider(models.Model):
    """ai_providers 表：AI 供应商账号（可同时配置多家，各自独立 Key / Base URL / 启停）。"""
    key = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    base_url = models.CharField(max_length=500, null=True, blank=True)
    api_key = models.CharField(max_length=500, null=True, blank=True)
    enabled = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False)
    note = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ai_providers'
        ordering = ['id']

    def __str__(self):
        return f'{self.name}({self.key})'

    # ---- 反向关系（对齐 AiProvider::models / enabledModels） ----
    # related_name 定义在 AiModel 上：models / enabled_models

    def enabled_models_qs(self):
        return self.models.filter(enabled=True).order_by('sort_order', 'id')

    def has_real_api_key(self):
        return bool(self.api_key) and not str(self.api_key).startswith('sk-xxxx')

    def is_usable(self):
        return bool(self.enabled) and self.has_real_api_key()

    def masked_api_key(self):
        return _mask_api_key(self.api_key)

    def lab(self):
        return AiSetting.PROVIDERS.get(self.key, {}).get('lab', 'openai-compatible')

    def preset_models(self):
        return AiSetting.PROVIDERS.get(self.key, {}).get('models', {})

    @classmethod
    def find_by_key(cls, key):
        return cls.objects.filter(key=key).first()

    @classmethod
    def default(cls):
        return cls.objects.filter(is_default=True).first() or cls.objects.order_by('id').first()

    def mark_as_default(self):
        AiProvider.objects.exclude(pk=self.pk).update(is_default=False)
        self.is_default = True
        self.save(update_fields=['is_default', 'updated_at'])

    def sync_to_legacy_setting(self):
        """同步 AiSetting 老字段，保证其它依赖 ai_settings 的代码不被破坏。"""
        setting = AiSetting.current()
        model = (self.enabled_models_qs().filter(is_default=True).first()
                 or self.enabled_models_qs().first()
                 or self.models.first())
        setting.provider = self.key
        setting.base_url = self.base_url or setting.base_url
        setting.api_key = self.api_key if self.has_real_api_key() else setting.api_key
        if model and model.model_id:
            setting.model = model.model_id
        setting.save()

    @classmethod
    def ensure_presets(cls):
        """把 PROVIDERS 预设里尚未落库的供应商补齐（对齐 AiController::settings 的自动建行）。"""
        for key, info in AiSetting.PROVIDERS.items():
            cls.objects.get_or_create(
                key=key,
                defaults={'name': info['name'], 'base_url': info['base_url'], 'enabled': False},
            )


class AiModel(models.Model):
    """ai_models 表：AI 模型目录，隶属某供应商，用户可在聊天时切换。"""
    provider = models.ForeignKey(
        AiProvider, on_delete=models.CASCADE, related_name='models', db_column='provider_id')
    model_id = models.CharField(max_length=120)
    name = models.CharField(max_length=150, null=True, blank=True)
    is_vision = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ai_models'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f'{self.provider.name} · {self.name or self.model_id}'

    def full_label(self):
        return f'{self.provider.name or "未指定供应商"} · {self.name or self.model_id}'

    def badge(self):
        return self.name or self.model_id

    def is_usable(self):
        return bool(self.enabled) and self.provider_id and self.provider.is_usable()

    @classmethod
    def usable_qs(cls):
        """用户可切换的模型：模型启用 + 所属供应商启用且有 Key。"""
        return (cls.objects.select_related('provider')
                .filter(enabled=True, provider__enabled=True)
                .exclude(provider__api_key__isnull=True)
                .exclude(provider__api_key='')
                .order_by('sort_order', 'id'))

    @classmethod
    def usable(cls):
        return [m for m in cls.usable_qs() if m.provider.has_real_api_key()]

    @classmethod
    def usable_default(cls):
        candidates = cls.usable()
        for m in candidates:
            if m.is_default:
                return m
        return candidates[0] if candidates else None

    @classmethod
    def usable_by_id(cls, pk):
        return next((m for m in cls.usable() if m.pk == pk), None)

    def mark_as_default(self):
        AiModel.objects.exclude(pk=self.pk).update(is_default=False)
        self.is_default = True
        self.save(update_fields=['is_default', 'updated_at'])


class AiConversation(SoftDeleteModel):
    """ai_conversations 表：AI 会话（多会话历史）。"""
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='ai_conversations', db_column='user_id')
    title = models.CharField(max_length=200, default='新会话')
    skill_slug = models.CharField(max_length=100, null=True, blank=True)
    model = models.ForeignKey(
        AiModel, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='conversations', db_column='model_id')
    message_count = models.IntegerField(default=0)
    last_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ai_conversations'
        ordering = ['-last_message_at', '-id']

    def __str__(self):
        return self.title

    def excerpt(self, limit=40):
        """会话摘要：首条用户消息的前 limit 个字符（对齐 AiConversation::excerpt）。"""
        first = self.messages.filter(role='user').first()
        content = (first.content if first else '') or ''
        content = content.replace('\r', ' ').replace('\n', ' ').strip()
        if not content:
            return '（无内容）'
        return content[:limit] + ('…' if len(content) > limit else '')


class AiMessage(models.Model):
    """ai_messages 表：会话中的单条消息。"""
    conversation = models.ForeignKey(
        AiConversation, on_delete=models.CASCADE, related_name='messages', db_column='conversation_id')
    role = models.CharField(max_length=20)  # user|assistant|system
    content = models.TextField(null=True, blank=True)
    meta = models.JSONField(null=True, blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ai_messages'
        ordering = ['id']

    def __str__(self):
        return f'{self.role}: {(self.content or "")[:20]}'


class AiAttachment(models.Model):
    """ai_attachments 表：会话附件（图片 / 文件）。"""
    conversation = models.ForeignKey(
        AiConversation, on_delete=models.CASCADE, related_name='attachments',
        db_column='conversation_id')
    message = models.ForeignKey(
        AiMessage, null=True, blank=True, on_delete=models.CASCADE,
        related_name='attachments', db_column='message_id')
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='ai_attachments', db_column='user_id')
    type = models.CharField(max_length=20)  # image|file
    original_name = models.CharField(max_length=255)
    path = models.CharField(max_length=500)
    mime_type = models.CharField(max_length=120, null=True, blank=True)
    size = models.BigIntegerField(default=0)
    text_extracted = models.BooleanField(default=False)
    extracted_text = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ai_attachments'
        ordering = ['id']

    def __str__(self):
        return self.original_name

    def is_image(self):
        return self.type == 'image' or str(self.mime_type or '').startswith('image/')

    def readable_size(self):
        size = float(self.size or 0)
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size < 1024 or unit == 'GB':
                return f'{round(size, 2)} {unit}'
            size /= 1024

    def absolute_path(self):
        from django.conf import settings
        return settings.PM_MEDIA_ROOT / self.path

    def exists(self):
        return self.absolute_path().is_file()

    def icon(self):
        if self.is_image():
            return 'fa-file-image-o'
        name = (self.original_name or '').lower()
        if name.endswith('.pdf'):
            return 'fa-file-pdf-o'
        if name.endswith(('.xls', '.xlsx', '.csv')):
            return 'fa-file-excel-o'
        if name.endswith(('.doc', '.docx')):
            return 'fa-file-word-o'
        if name.endswith(('.ppt', '.pptx')):
            return 'fa-file-powerpoint-o'
        if name.endswith(('.zip', '.rar', '.7z')):
            return 'fa-file-archive-o'
        return 'fa-file-text-o'
