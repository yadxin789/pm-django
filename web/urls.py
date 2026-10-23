# -*- coding: utf-8 -*-
"""web 路由（对齐 Laravel routes/web.php 全量路由；url name 沿用 Laravel 带点命名）。

注意：Django path() 只匹配 URL 无法区分 GET/POST，因此 index/show 等 GET 视图与
store/update 等 POST 视图必须使用不同路径，否则 POST 请求会被先声明的 GET 视图抢占，
导致保存/更新/上传等操作静默失败（返回 GET 页面而不写入数据）。
本文件已为所有 POST 视图分配独立路径，name 不变，模板/表单的 {% url %} 自动跟随。
"""
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path

from web.views import (
    admin, admin_trash, ai, ai_api, auth, collab, dashboard, extension, file, history,
    knowledge, message, my_trash, notification, private_message, profile, project,
    project_template, support, task, todo, voice, wecom, wecomfile, welcome,
)

urlpatterns = [
    # ---- 公开 ----
    path('', welcome.index, name='home'),
    path('login', auth.login_view, name='login'),
    path('register', auth.register, name='register'),
    path('logout', auth.logout_view, name='logout'),

    # ---- 企业微信 OAuth（公开） ----
    path('wecom/auth', wecom.redirect_to_wecom, name='wecom.auth'),
    path('wecom/callback', wecom.oauth_callback, name='wecom.callback'),
    path('wecom/callback/event', wecom.event_callback, name='wecom.event-callback'),

    # ---- 认证后 ----
    path('dashboard', dashboard.index, name='dashboard'),

    # ---- AI 智能助手（开通引导 / 聊天 / 设置 / 自然语言生成文档）----
    path('ai', ai.welcome, name='ai.welcome'),
    path('ai/notice', ai.notice, name='ai.notice'),
    path('ai/chat', ai.chat, name='ai.chat'),
    path('ai/ask', ai.ask, name='ai.ask'),
    path('ai/clear', ai.clear, name='ai.clear'),

    # AI 会话管理（多会话 / 历史记录）
    path('ai/conversations', ai.store_conversation, name='ai.conversations.store'),
    path('ai/conversations/<int:pk>/rename', ai.rename_conversation, name='ai.conversations.rename'),
    path('ai/conversations/<int:pk>/destroy', ai.destroy_conversation, name='ai.conversations.destroy'),
    path('ai/conversations/<int:pk>/clear', ai.clear_conversation, name='ai.conversations.clear'),
    path('ai/conversations/<int:pk>/export', ai.export_conversation, name='ai.conversations.export'),

    # AI 附件预览 / 下载
    path('ai/attachments/<int:pk>/preview', ai.preview_attachment, name='ai.attachments.preview'),
    path('ai/attachments/<int:pk>/download', ai.download_attachment, name='ai.attachments.download'),

    path('ai/settings', ai.settings, name='ai.settings'),
    path('ai/settings/update', ai.update_settings, name='ai.settings.update'),
    path('ai/settings/test', ai.test_connection, name='ai.settings.test'),

    # AI 供应商管理（可同时开通多家）
    path('ai/settings/providers/<str:provider>/save', ai.save_provider, name='ai.providers.save'),
    path('ai/settings/providers/<str:provider>/toggle', ai.toggle_provider, name='ai.providers.toggle'),
    path('ai/settings/providers/<str:provider>/default', ai.set_default_provider, name='ai.providers.default'),
    path('ai/settings/providers/<str:provider>/test', ai.test_provider, name='ai.providers.test'),
    path('ai/settings/providers/<str:provider>/sync-models', ai.sync_provider_models, name='ai.providers.sync'),
    path('ai/settings/providers/<str:provider>/remove', ai.remove_provider, name='ai.providers.remove'),

    # AI 模型管理（每个供应商可开放多个模型）
    path('ai/settings/models', ai.store_model, name='ai.models.store'),
    path('ai/settings/models/<int:pk>/update', ai.update_model, name='ai.models.update'),
    path('ai/settings/models/<int:pk>/default', ai.set_default_model, name='ai.models.default'),
    path('ai/settings/models/<int:pk>/destroy', ai.destroy_model, name='ai.models.destroy'),

    path('ai/docs', ai.docs, name='ai.docs'),
    path('ai/docs/generate', ai.generate_doc, name='ai.docs.generate'),

    # 管理后台
    path('admin/', admin.index, name='admin.index'),
    # ---- 管理员回收站（与个人回收站统一入口）----
    path('admin/collab-trash', admin_trash.index, name='admin.trash'),
    path('admin/collab-trash/<str:type>/restore-batch', admin_trash.restore_batch, name='admin.trash.restore-batch'),
    path('admin/collab-trash/<str:type>/force-delete-batch', admin_trash.force_delete_batch, name='admin.trash.force-delete-batch'),
    path('admin/collab-trash/<str:type>/<int:pk>/restore', admin_trash.restore, name='admin.trash.restore'),
    path('admin/collab-trash/<str:type>/<int:pk>/force-delete', admin_trash.force_delete, name='admin.trash.force-delete'),
    # ---- 个人回收站（每个登录用户都能访问，管理员会重定向到 admin.trash）----
    path('my-trash', my_trash.index, name='my-trash'),
    path('my-trash/<str:type>/<int:pk>/restore', my_trash.restore, name='my-trash.restore'),
    path('admin/users/deleted', admin.deleted_users, name='admin.users.deleted'),
    path('admin/users/restore/<int:pk>', admin.restore_user, name='admin.users.restore'),
    path('admin/users/force-delete/<int:pk>', admin.force_delete_user, name='admin.users.force-delete'),
    path('admin/users', admin.users, name='admin.users'),
    path('admin/users/create', admin.create_user, name='admin.users.create'),
    path('admin/users/store', admin.store_user, name='admin.users.store'),
    path('admin/users/import', admin.import_users_form, name='admin.users.import-form'),
    path('admin/users/import/store', admin.import_users, name='admin.users.import'),
    path('admin/users/<int:pk>/edit', admin.edit_user, name='admin.users.edit'),
    path('admin/users/<int:pk>/update', admin.update_user, name='admin.users.update'),
    path('admin/users/<int:pk>/delete', admin.delete_user, name='admin.users.destroy'),
    path('admin/departments', admin.departments, name='admin.departments'),
    path('admin/departments/create', admin.create_department, name='admin.departments.create'),
    path('admin/departments/store', admin.store_department, name='admin.departments.store'),
    path('admin/departments/<int:pk>/edit', admin.edit_department, name='admin.departments.edit'),
    path('admin/departments/<int:pk>/update', admin.update_department, name='admin.departments.update'),
    path('admin/departments/<int:pk>/delete', admin.delete_department, name='admin.departments.destroy'),
    path('admin/roles', admin.roles, name='admin.roles'),
    path('admin/roles/create', admin.create_role, name='admin.roles.create'),
    path('admin/roles/store', admin.store_role, name='admin.roles.store'),
    path('admin/roles/<int:pk>/edit', admin.edit_role, name='admin.roles.edit'),
    path('admin/roles/<int:pk>/update', admin.update_role, name='admin.roles.update'),
    path('admin/roles/<int:pk>/delete', admin.delete_role, name='admin.roles.destroy'),
    path('admin/statuses', admin.statuses, name='admin.statuses'),
    path('admin/statuses/<int:pk>/edit', admin.edit_status, name='admin.statuses.edit'),
    path('admin/statuses/<int:pk>/update', admin.update_status, name='admin.statuses.update'),
    path('admin/task-types', admin.task_types, name='admin.task-types'),
    path('admin/task-types/create', admin.create_task_type, name='admin.task-types.create'),
    path('admin/task-types/store', admin.store_task_type, name='admin.task-types.store'),
    path('admin/task-types/<int:pk>/edit', admin.edit_task_type, name='admin.task-types.edit'),
    path('admin/task-types/<int:pk>/update', admin.update_task_type, name='admin.task-types.update'),
    path('admin/task-types/<int:pk>/delete', admin.delete_task_type, name='admin.task-types.destroy'),
    path('admin/file-repository', admin.file_repository, name='admin.file-repository'),
    path('admin/operation-logs', admin.operation_logs, name='admin.operation-logs'),
    path('admin/send-notification', admin.send_notification, name='admin.send-notification'),
    path('admin/send-notification/post', admin.post_notification, name='admin.notification.post'),
    path('admin/services', admin.services, name='admin.services'),
    path('admin/backup', admin.backup_database, name='admin.backup'),
    path('admin/todos', admin.todos, name='admin.todos'),
    path('admin/chat-messages', admin.chat_messages, name='admin.chat-messages'),
    path('admin/chat-messages/delete', admin.delete_chat_messages, name='admin.chat-messages.delete'),
    path('admin/chat-messages/clear-project', admin.clear_project_chat, name='admin.chat-messages.clear-project'),
    path('admin/wecom/settings', wecom.admin_settings, name='admin.wecom.settings'),
    path('admin/wecom/settings/save', wecom.admin_save, name='admin.wecom.settings.save'),
    path('admin/wecom/test', wecom.admin_test, name='admin.wecom.test'),
    path('admin/wecom/message-logs', wecom.admin_message_logs, name='admin.wecom.message-logs'),

    # 项目
    path('project/', project.index, name='project.index'),
    path('project/my-projects', project.my_projects, name='project.my-projects'),
    path('project/my-projects/overdue', project.my_overdue_projects, name='project.my-projects.overdue'),
    path('project/my-projects/in-progress', project.my_in_progress_projects, name='project.my-projects.in-progress'),
    path('project/my-projects/not-started', project.my_not_started_projects, name='project.my-projects.not-started'),
    path('project/my-projects/completed', project.my_completed_projects, name='project.my-projects.completed'),
    path('project/my-projects/suspended', project.my_suspended_projects, name='project.my-projects.suspended'),
    path('project/create', project.create, name='project.create'),
    path('project/store', project.store, name='project.store'),
    path('project/<int:pk>', project.show, name='project.show'),
    path('project/<int:pk>/edit', project.edit, name='project.edit'),
    path('project/<int:pk>/update', project.update, name='project.update'),
    path('project/<int:pk>/delete', project.delete, name='project.destroy'),
    path('project/<int:pk>/logs', project.logs, name='project.logs'),
    path('project/<int:pk>/change', project.change, name='project.change'),
    path('project/<int:pk>/change/update', project.update_change, name='project.update_change'),
    path('project/<int:pk>/gantt', project.gantt, name='project.gantt'),
    path('project/<int:pk>/pause', project.pause, name='project.pause'),
    path('project/<int:pk>/resume', project.resume, name='project.resume'),
    path('project/get-designers/<int:department_id>', project.get_designers, name='project.get-designers'),
    path('project/get-managers/<int:department_id>', project.get_managers, name='project.get-managers'),
    path('project/get-template/<int:template_id>', project.get_template_config, name='project.get-template'),
    # 协作文档
    path('project/<int:pk>/collab', collab.index, name='project.collab.index'),
    path('project/<int:pk>/collab/create', collab.create, name='project.collab.create'),
    path('project/<int:pk>/collab/store', collab.store, name='project.collab.store'),
    path('project/<int:pk>/collab/<int:doc>/open', collab.open_doc, name='project.collab.open'),
    path('project/<int:pk>/collab/<int:doc>/delete', collab.destroy, name='project.collab.destroy'),
    # 语音
    path('project/<int:pk>/voice', voice.index, name='project.voice.index'),
    path('project/<int:pk>/voice/token', voice.issue_token, name='project.voice.token'),
    path('project/<int:pk>/voice/members', voice.members, name='project.voice.members'),
    path('project/<int:pk>/voice/doc-config/<int:doc>', voice.doc_config, name='project.voice.doc-config'),
    # 会议内文档：与协同办公共用同一个文档库、同一套权限与实现
    path('project/<int:pk>/voice/doc-store', voice.doc_store, name='project.voice.doc-store'),
    path('project/<int:pk>/voice/doc-delete/<int:doc>', voice.doc_delete, name='project.voice.doc-delete'),
    # 兼容浏览器里缓存的老 voice-room.js（它 POST 的还是 upload-demo 这个地址）
    path('project/<int:pk>/voice/upload-demo', voice.doc_store, name='project.voice.upload-demo'),
    path('project/<int:pk>/voice/history', voice.history, name='project.voice.history'),
    path('voice/hooks/enter', voice.hook_enter, name='voice.hooks.enter'),
    path('voice/hooks/leave', voice.hook_leave, name='voice.hooks.leave'),

    # 项目履历
    path('history/project/<int:pk>', history.project_history, name='history.project.history'),

    # 聊天（群聊 + 私聊）
    path('project/<int:project_id>/messages', message.index, name='messages.index'),
    path('project/<int:project_id>/messages/send', message.store, name='messages.store'),
    path('project/<int:project_id>/messages/clear', message.clear_mine, name='messages.clear-mine'),
    path('project/<int:project_id>/messages/<int:mid>/delete', message.destroy, name='messages.destroy'),
    path('messages/<int:mid>/file', message.file, name='messages.file'),
    path('project/<int:project_id>/chat/<int:target_user_id>', message.private_chat, name='messages.private'),
    path('project/<int:project_id>/chat/<int:target_user_id>/clear', message.private_clear_mine, name='messages.private.clear-mine'),
    path('api/project/<int:project_id>/chat/members', private_message.members, name='api.chat.members'),
    path('api/project/<int:project_id>/chat/history/<int:target_user_id>', private_message.history, name='api.chat.history'),
    path('api/project/<int:project_id>/chat/send', private_message.send, name='api.chat.send'),

    # 个人中心
    path('profile/avatar', profile.avatar, name='profile.avatar'),
    path('profile/avatar/upload', profile.upload_avatar, name='profile.avatar.upload'),
    path('profile/edit', profile.edit_personal, name='profile.edit'),
    path('profile/update', profile.update_personal, name='profile.update'),
    path('profile/password', profile.edit_password, name='profile.password.edit'),
    path('profile/password/update', profile.update_password, name='profile.password.update'),
    path('profile/wecom/', profile.wecom_index, name='profile.wecom.index'),
    path('profile/wecom/unbind', profile.wecom_unbind, name='profile.wecom.unbind'),
    path('profile/wecom/settings', profile.wecom_update_settings, name='profile.wecom.settings.update'),

    # 任务
    path('task/', task.index, name='task.index'),
    path('task/my-tasks', task.my_tasks, name='task.my-tasks'),
    path('task/my-tasks/pending-execution', task.my_pending_execution_tasks, name='task.my-tasks.pending-execution'),
    path('task/my-tasks/overdue', task.my_overdue_tasks, name='task.my-tasks.overdue'),
    path('task/my-tasks/in-progress', task.my_in_progress_tasks, name='task.my-tasks.in-progress'),
    path('task/my-tasks/completed', task.my_completed_tasks, name='task.my-tasks.completed'),
    path('task/my-tasks/suspended', task.my_suspended_tasks, name='task.my-tasks.suspended'),
    path('task/my-audit-tasks', task.my_audit_tasks, name='task.my-audit-tasks'),
    path('task/my-audit-tasks/pending-audit', task.my_pending_audit_tasks, name='task.my-audit-tasks.pending-audit'),
    path('task/my-audit-tasks/overdue', task.my_overdue_audit_tasks, name='task.my-audit-tasks.overdue'),
    path('task/my-audit-tasks/in-progress', task.my_in_progress_audit_tasks, name='task.my-audit-tasks.in-progress'),
    path('task/my-audit-tasks/completed', task.my_completed_audit_tasks, name='task.my-audit-tasks.completed'),
    path('task/my-audit-tasks/suspended', task.my_suspended_audit_tasks, name='task.my-audit-tasks.suspended'),
    path('task/<int:pk>', task.show, name='task.show'),
    path('task/<int:pk>/upload', task.upload_file, name='task.upload'),
    path('task/<int:pk>/upload-file', task.store_file, name='task.store-file'),
    path('task/<int:pk>/audit', task.audit, name='task.audit'),
    path('task/<int:pk>/approve', task.approve, name='task.approve'),
    path('task/<int:pk>/reject', task.reject, name='task.reject'),
    path('task/<int:pk>/apply-extension', task.apply_extension, name='task.apply-extension'),
    path('task/<int:pk>/apply-extension/store', task.store_extension, name='task.store-extension'),
    path('task/<int:pk>/update', task.update_file, name='task.update-file'),
    path('task/<int:pk>/update/store', task.store_update, name='task.store-update'),

    # 文件
    path('file/', file.index, name='file.index'),
    path('file/resource-files', file.resource_files, name='file.resource-files'),
    path('file/manage-resource-files', file.manage_resource_files, name='file.manage-resource-files'),
    path('file/<int:pk>/preview', file.preview, name='file.preview'),
    path('file/<int:pk>/preview-content', file.preview_file_content, name='file.preview-content'),
    path('file/<int:pk>/download', file.download, name='file.download'),
    path('file/upload-department', file.upload_department_file, name='file.upload-department'),
    path('file/upload-department/store', file.store_department_file, name='file.store-department'),
    path('file/department/<int:pk>/preview', file.preview_department_file, name='file.preview-department'),
    path('file/department/<int:pk>/download', file.download_department_file, name='file.download-department'),
    path('file/department/<int:pk>/update', file.update_department_file, name='file.update-department'),
    path('file/department/<int:pk>/delete', file.delete_department_file, name='file.delete-department'),

    # 通知
    path('notification/', notification.index, name='notification.index'),
    path('notification/<int:pk>/read', notification.mark_as_read, name='notification.mark-read'),
    path('notification/read-all', notification.mark_all_as_read, name='notification.mark-all-read'),
    path('notification/<int:pk>/delete', notification.destroy, name='notification.destroy'),

    # 企微文件
    path('wecom-file/', wecomfile.index, name='wecom.file.index'),
    path('wecom-file/<int:pk>/preview', wecomfile.preview, name='wecom.file.preview'),
    path('wecom-file/<int:pk>/download', wecomfile.download, name='wecom.file.download'),
    path('wecom-file/<int:pk>/archive', wecomfile.archive, name='wecom.file.archive'),
    path('wecom-file/<int:pk>/delete', wecomfile.destroy, name='wecom.file.destroy'),
    path('wecom-file/send-to-wecom', wecomfile.send_to_wecom, name='wecom.file.send'),
    path('wecom-file/api/bound-users', wecomfile.bound_users, name='wecom.file.bound-users'),

    # 待办
    path('todo/', todo.index, name='todo.index'),
    path('todo/<int:pk>/complete', todo.complete, name='todo.complete'),
    path('todo/extension/<int:application_id>/approve', todo.approve_extension, name='todo.extension.approve'),
    path('todo/extension/<int:application_id>/reject', todo.reject_extension, name='todo.extension.reject'),

    # 延期申请
    path('extension/', extension.index, name='extension.index'),
    path('extension/<int:application_id>/approve', extension.approve, name='extension.approve'),
    path('extension/<int:application_id>/reject', extension.reject, name='extension.reject'),

    # 支持
    path('support/', support.index, name='support.index'),
    path('support/department-files', support.department_files, name='support.department-files'),
    path('support/send-notification', support.send_notification, name='support.send-notification'),
    path('support/send-notification/post', support.post_notification, name='support.notification.post'),
    path('support/upload-resource-file', support.upload_resource_file, name='support.upload-resource-file'),
    path('support/upload-resource-file/store', support.store_resource_file, name='support.store-resource-file'),

    # 项目模板
    path('project-template/', project_template.index, name='project-template.index'),
    path('project-template/create', project_template.create, name='project-template.create'),
    path('project-template/store', project_template.store, name='project-template.store'),
    path('project-template/<int:pk>/edit', project_template.edit, name='project-template.edit'),
    path('project-template/<int:pk>/update', project_template.update, name='project-template.update'),
    path('project-template/<int:pk>/delete', project_template.destroy, name='project-template.destroy'),

    # 知识库（show 查看须在 update 之前，避免 POST 更新抢占 GET 查看）
    path('knowledge/', knowledge.index, name='knowledge.index'),
    path('knowledge/create', knowledge.create, name='knowledge.create'),
    path('knowledge/store', knowledge.store, name='knowledge.store'),
    path('knowledge/<int:pk>/edit', knowledge.edit, name='knowledge.edit'),
    path('knowledge/<int:pk>/update', knowledge.update, name='knowledge.update'),
    path('knowledge/<int:pk>', knowledge.show, name='knowledge.show'),
    path('knowledge/<int:pk>/delete', knowledge.destroy, name='knowledge.destroy'),
    path('knowledge/<int:pk>/docs', knowledge.doc_store, name='knowledge.doc.store'),
    path('knowledge/<int:pk>/docs/<int:doc>', knowledge.doc_update, name='knowledge.doc.update'),
    path('knowledge/<int:pk>/docs/<int:doc>/delete', knowledge.doc_destroy, name='knowledge.doc.destroy'),
    path('knowledge/<int:pk>/docs/<int:doc>/read', knowledge.doc_read, name='knowledge.doc.read'),

    # 企业微信集成（侧边栏入口 + 配置保存/测试/日志）
    path('wecom/settings', wecom.admin_settings, name='wecom.settings'),
    path('wecom/settings/save', wecom.admin_save, name='wecom.settings.save'),
    path('wecom/test', wecom.admin_test, name='wecom.test'),
    path('wecom/message-logs', wecom.admin_message_logs, name='wecom.message-logs'),

    # 协同办公（无登录态，doc_key 校验）
    path('collab/download/<int:doc>', collab.download, name='collab.download'),
    path('collab/callback/<int:doc>', collab.callback, name='collab.callback'),
]

# 开发环境服务上传的媒体文件（头像/文件），与 Laravel 的 storage:link 对应
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
