"""URL configuration for pm project."""
from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from web.views import ai_api

urlpatterns = [
    path('django-admin/', admin.site.urls),
    # AI 数据 REST API（X-AI-Key 认证，对齐 Laravel routes/api.php）
    path('api/ai/meta', ai_api.meta, name='ai.api.meta'),
    path('api/ai/overview', ai_api.overview, name='ai.api.overview'),
    path('api/ai/projects/<int:pk>', ai_api.project_show, name='ai.api.projects.show'),
    path('api/ai/projects', ai_api.projects, name='ai.api.projects'),
    path('api/ai/tasks', ai_api.tasks, name='ai.api.tasks'),
    path('api/ai/users', ai_api.users, name='ai.api.users'),
    path('api/ai/departments', ai_api.departments, name='ai.api.departments'),
    path('api/ai/knowledge', ai_api.knowledge, name='ai.api.knowledge'),
    path('api/ai/files', ai_api.files, name='ai.api.files'),
    path('api/ai/todos', ai_api.todos, name='ai.api.todos'),
    path('api/ai/templates', ai_api.templates, name='ai.api.templates'),
    # 业务路由（对应 Laravel routes/web.php）
    path('', include('web.urls')),
]

if settings.DEBUG:
    from django.conf.urls.static import static
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
