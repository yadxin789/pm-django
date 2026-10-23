# -*- coding: utf-8 -*-
"""模板过滤器/标签（辅助布局 URL 激活判断等）。"""
from django import template

register = template.Library()


@register.filter
def startswith(value, arg):
    """判断字符串是否以 arg 开头（用于 routeIs 通配符判断）。"""
    return bool(value and str(value).startswith(str(arg)))


@register.filter
def pane_key(value):
    """状态 key（in_progress / not_started / overdue_completed 等，下划线）→
    HTML/CSS 用的 DOM key（in-progress / not-started / overdue-completed 等，连字符）。

    对齐 my-projects.html 中 tab 按钮的 data-bs-target 与 tab-pane 的 id 命名。
    """
    return str(value or '').replace('_', '-')


@register.filter
def endswith(value, arg):
    return bool(value and str(value).endswith(str(arg)))


@register.filter
def kd_type_label(value):
    """知识库文档类型数字 → 中文标签。"""
    return {1: '文档', 2: '章节', 3: '链接'}.get(value, '未知')


_STATE_LABEL = {
    'Completed': '已完成', 'InProgress': '进行中', 'NotStarted': '未启动',
    'Overdue': '已逾期', 'OverdueCompleted': '逾期完成', 'Suspended': '已暂停',
    'Pending': '等待中', 'ForAudit': '待审核', 'Rejected': '已驳回',
}
_STATE_COLOR = {
    'Project': {'Completed': '#28a745', 'InProgress': '#ffc107', 'NotStarted': '#6c757d',
                'Overdue': '#dc3545', 'OverdueCompleted': '#fd7e14', 'Suspended': '#6f42c1'},
    'Task': {'Completed': '#28a745', 'InProgress': '#17a2b8', 'NotStarted': '#6c757d',
             'Overdue': '#dc3545', 'OverdueCompleted': '#fd7e14', 'Suspended': '#6f42c1',
             'Pending': '#6c757d', 'ForAudit': '#17a2b8', 'Rejected': '#dc3545'},
}


def _state_scope(value):
    return 'Task' if '\\Task\\' in (value or '') else 'Project'


@register.filter
def status_label(value, obj=None):
    """状态字段 → 中文标签。

    对 Project 的 OverdueCompleted 状态做动态天数计算（对齐 Laravel
    app/States/Project/OverdueCompleted.php::label）：
    ``end_date < now`` 时输出 ``逾期N天完成``，否则 ``逾期完成``。

    用法：
        {{ p.status|status_label }}                  # 静态 label
        {{ p.status|status_label:p }}                # 传 Project 实例用于计算天数
        {{ task.status|status_label:task }}          # Task 不会有动态天数
    """
    if not value:
        return ''
    cls = str(value).rsplit('\\', 1)[-1]
    if cls == 'OverdueCompleted' and obj is not None and _state_scope(value) == 'Project':
        end_date = getattr(obj, 'end_date', None)
        if end_date:
            from datetime import datetime
            now = datetime.now()
            # Laravel diffInDays($other, false) = (other - this) 的天数差（保留符号）
            # 即 now - end_date；end_date < now 时 > 0
            delta = (now.date() - end_date).days
            if delta > 0:
                return f'逾期{delta}天完成'
        return '逾期完成'
    return _STATE_LABEL.get(cls, cls)


@register.filter
def status_color(value):
    if not value:
        return '#6c757d'
    cls = str(value).rsplit('\\', 1)[-1]
    return _STATE_COLOR.get(_state_scope(value), {}).get(cls, '#6c757d')


@register.filter
def get_item(mapping, key):
    if mapping is None:
        return 0
    if isinstance(mapping, dict):
        return mapping.get(key, 0)
    return 0


@register.filter(name='get')
def get_(mapping, key):
    """通用 dict 取值（与 dict.get 等价），找不到返回 None。"""
    if mapping is None:
        return None
    if isinstance(mapping, dict):
        return mapping.get(key)
    return None


@register.simple_tag(takes_context=True)
def route_active(context, *patterns):
    """当前 url_name 是否匹配任一模式（支持 * 通配），匹配返回 'active' 否则 ''。"""
    request = context.get('request')
    if not request:
        return ''
    name = getattr(getattr(request, 'resolver_match', None), 'url_name', '')
    for p in patterns:
        if p.endswith('*'):
            if name.startswith(p[:-1]):
                return 'active'
        elif name == p:
            return 'active'
    return ''


@register.simple_tag
def can_upload_file(task, user_id):
    return bool(task.can_upload_file(user_id))


@register.simple_tag
def can_apply_extension(task, user_id):
    return bool(task.can_apply_extension(user_id))


@register.simple_tag
def can_update_file(task, user_id, is_admin):
    return bool(task.can_update_file(user_id, is_admin))


@register.simple_tag
def can_audit(task, user_id):
    return bool(task.can_audit(user_id))
