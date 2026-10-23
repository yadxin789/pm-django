# -*- coding: utf-8 -*-
"""预置种子数据命令（对齐参考目录 database/seeders：Role/Department/TaskType/User + statuses）。

用法：python manage.py seed_data
幂等：已存在记录不重复创建。
"""
from django.core.management.base import BaseCommand

from core.models import Department, Role, Status, TaskType, User

ROLES = [
    {'name': '系统管理员', 'code': 'admin', 'description': '系统后台管理权限'},
    {'name': '项目创建人', 'code': 'project_creator', 'description': '创建和管理项目（原项目经理岗位）'},
    {'name': '项目监督人', 'code': 'project_manager', 'description': '管理所有项目，查看全部项目数据（原项目管理部总经理岗位）'},
    {'name': '任务执行人', 'code': 'task_executor', 'description': '执行任务并上传文件（原设计师岗位）'},
    {'name': '任务审核人', 'code': 'task_auditor', 'description': '审核任务文件（原主管岗位）'},
    {'name': '支援人员', 'code': 'support', 'description': '部门资料支持（原文员岗位）'},
]

DEPARTMENTS = [
    {'name': '项目管理部', 'code': 'pm', 'type': 'project', 'sort_order': 1},
    {'name': '产品规划部', 'code': 'product', 'type': 'project', 'sort_order': 2},
    {'name': '外观设计部', 'code': 'appearance', 'type': 'project', 'sort_order': 3},
    {'name': '结构设计部', 'code': 'structure', 'type': 'project', 'sort_order': 4},
    {'name': '硬件设计部', 'code': 'hardware', 'type': 'project', 'sort_order': 5},
    {'name': '软件设计部', 'code': 'software', 'type': 'project', 'sort_order': 6},
    {'name': '样品制作部', 'code': 'sample', 'type': 'project', 'sort_order': 7},
    {'name': '品质验证部', 'code': 'quality', 'type': 'project', 'sort_order': 8},
    {'name': '包装设计部', 'code': 'packaging', 'type': 'project', 'sort_order': 9},
    {'name': '系统后台管理部', 'code': 'admin_dept', 'type': 'non-project', 'sort_order': 10},
    {'name': '物料管理部', 'code': 'material', 'type': 'non-project', 'sort_order': 11},
    {'name': '专利管理部', 'code': 'patent', 'type': 'non-project', 'sort_order': 12},
    {'name': '认证管理部', 'code': 'certification', 'type': 'non-project', 'sort_order': 13},
]

# statuses 表预置：状态机完整类名 -> 自定义标签（对齐 refactor_statuses 迁移）
STATES = [
    'App\\States\\Project\\InProgress',
    'App\\States\\Project\\Completed',
    'App\\States\\Project\\Overdue',
    'App\\States\\Project\\OverdueCompleted',
    'App\\States\\Project\\Suspended',
    'App\\States\\Project\\NotStarted',
    'App\\States\\Task\\Pending',
    'App\\States\\Task\\InProgress',
    'App\\States\\Task\\ForAudit',
    'App\\States\\Task\\Completed',
    'App\\States\\Task\\Rejected',
    'App\\States\\Task\\Overdue',
    'App\\States\\Task\\OverdueCompleted',
]

# 任务类型（对齐 TaskTypeSeeder）：(名称, 描述, 部门code, 排序)
TASK_TYPES = [
    ('立项书', '项目立项相关文档', 'product', 1),
    ('项目功能技术要求说明书', '项目功能和技术要求说明', 'product', 2),
    ('产品成本评估报告', '产品成本评估分析报告', 'product', 3),
    ('项目结案总结报告', '项目结案总结文档', 'product', 4),
    ('产品外观设计图', '产品外观设计图纸', 'appearance', 5),
    ('外观设计说明书', '外观设计说明文档', 'appearance', 6),
    ('外观设计综合压缩包', '外观设计相关文件压缩包', 'appearance', 7),
    ('产品结构图', '产品结构设计图纸', 'structure', 8),
    ('产品装配图', '产品装配图纸', 'structure', 9),
    ('产品结构说明书', '产品结构说明文档', 'structure', 10),
    ('结构BOM表', '结构物料清单', 'structure', 11),
    ('产品结构设计综合压缩包', '结构设计相关文件压缩包', 'structure', 12),
    ('电路原理图', '电路原理图纸', 'hardware', 13),
    ('原理图说明书', '原理图说明文档', 'hardware', 14),
    ('原理设计综合压缩包', '原理设计相关文件压缩包', 'hardware', 15),
    ('PCB板图', 'PCB板设计图纸', 'hardware', 16),
    ('PCB板3D图', 'PCB板3D设计图', 'hardware', 17),
    ('电子BOM表', '电子物料清单', 'hardware', 18),
    ('PCB综合压缩包', 'PCB设计相关文件压缩包', 'hardware', 19),
    ('软件流程图', '软件流程设计图', 'software', 20),
    ('软件设计说明书', '软件设计说明文档', 'software', 21),
    ('软件综合压缩包', '软件设计相关文件压缩包', 'software', 22),
    ('样品制作要求书', '样品制作要求说明', 'sample', 23),
    ('样品技术要求书', '样品技术要求说明', 'sample', 24),
    ('样品测试项目要求书', '样品测试项目要求', 'sample', 25),
    ('样本测试报告', '样品测试报告文档', 'sample', 26),
    ('产品规格书', '产品规格说明文档', 'sample', 27),
    ('样品制作过程总结报告', '样品制作过程总结', 'sample', 28),
    ('样品制作综合压缩包', '样品制作相关文件压缩包', 'sample', 29),
    ('品质验证项目要求书', '品质验证项目要求', 'quality', 30),
    ('品质测试数据表', '品质测试数据记录', 'quality', 31),
    ('品质验证报告', '品质验证报告文档', 'quality', 32),
    ('品质验证综合压缩包', '品质验证相关文件压缩包', 'quality', 33),
    ('包装设计图', '包装设计图纸', 'packaging', 34),
    ('标示标签徽标设计图', '标签徽标设计图纸', 'packaging', 35),
    ('包装设计说明书', '包装设计说明文档', 'packaging', 36),
    ('包装设计综合压缩包', '包装设计相关文件压缩包', 'packaging', 37),
]


class Command(BaseCommand):
    help = '灌入预置种子数据（角色/部门/状态/任务类型/用户），幂等。'

    def handle(self, *args, **options):
        # 1. 角色
        for r in ROLES:
            Role.objects.get_or_create(code=r['code'], defaults=r)
        self.stdout.write(self.style.SUCCESS(f'角色: {len(ROLES)}'))

        # 2. 部门
        for d in DEPARTMENTS:
            Department.objects.get_or_create(code=d['code'], defaults=d)
        self.stdout.write(self.style.SUCCESS(f'部门: {len(DEPARTMENTS)}'))

        # 3. 状态
        for sc in STATES:
            Status.objects.get_or_create(state_class=sc, defaults={'custom_label': None})
        self.stdout.write(self.style.SUCCESS(f'状态: {len(STATES)}'))

        # 4. 任务类型
        dept_by_code = {d.code: d.id for d in Department.objects.all()}
        created_tt = 0
        for name, desc, dept_code, sort in TASK_TYPES:
            tt, created = TaskType.objects.get_or_create(
                name=name,
                defaults={'description': desc, 'department_id': dept_by_code.get(dept_code), 'is_required': 1, 'sort_order': sort})
            if created:
                created_tt += 1
        self.stdout.write(self.style.SUCCESS(f'任务类型: {len(TASK_TYPES)} (新增 {created_tt})'))

        # 5. 用户（对齐 UserSeeder，密码 123456）
        self._seed_users(dept_by_code)
        self.stdout.write(self.style.SUCCESS('种子数据灌入完成。'))

    def _seed_users(self, dept_by_code):
        role_by_code = {r.code: r.id for r in Role.objects.all()}
        admin_role = role_by_code.get('admin')
        creator_role = role_by_code.get('project_creator')
        pm_role = role_by_code.get('project_manager')
        executor_role = role_by_code.get('task_executor')
        auditor_role = role_by_code.get('task_auditor')
        support_role = role_by_code.get('support')
        admin_dept = dept_by_code.get('admin_dept')
        pm_dept = dept_by_code.get('pm')

        seed_users = []

        # 管理员
        seed_users.append(dict(username='admin', name='系统管理员', department_id=admin_dept, role_id=admin_role))
        # 项目监督人
        if pm_dept and pm_role:
            seed_users.append(dict(username='pm_director', name='项目监督人', department_id=pm_dept, role_id=pm_role))

        # 项目参与部门
        for d in DEPARTMENTS:
            if d['type'] != 'project':
                continue
            dept_id = dept_by_code[d['code']]
            if d['code'] == 'pm':
                for i in (1, 2):
                    seed_users.append(dict(username=f'pm_manager_{i}', name=f'项目管理部项目创建人{i}',
                                           department_id=dept_id, role_id=creator_role))
            else:
                for i in (1, 2):
                    seed_users.append(dict(username=f'{d["code"]}_manager_{i}', name=f'{d["name"]}任务审核人{i}',
                                           department_id=dept_id, role_id=auditor_role))
                    seed_users.append(dict(username=f'{d["code"]}_designer_{i}', name=f'{d["name"]}任务执行人{i}',
                                           department_id=dept_id, role_id=executor_role))
                    seed_users.append(dict(username=f'{d["code"]}_clerk_{i}', name=f'{d["name"]}支援人员{i}',
                                           department_id=dept_id, role_id=support_role))

        # 非项目参与部门（支援人员）
        for d in DEPARTMENTS:
            if d['type'] != 'non-project' or d['code'] == 'admin_dept':
                continue
            dept_id = dept_by_code[d['code']]
            for i in (1, 2):
                seed_users.append(dict(username=f'{d["code"]}_clerk_{i}', name=f'{d["name"]}支援人员{i}',
                                       department_id=dept_id, role_id=support_role))

        created = 0
        for u in seed_users:
            if User.objects.filter(username=u['username']).exists():
                continue
            User.objects.create_user(
                username=u['username'], password='123456', name=u['name'],
                department_id=u['department_id'], role_id=u['role_id'], is_active=True)
            created += 1
        self.stdout.write(self.style.SUCCESS(f'用户: {len(seed_users)} (新增 {created})'))
