# -*- coding: utf-8 -*-
"""历史履历视图：对齐 Laravel HistoryController.php。"""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render

from core.models import Project, ProjectLog


@login_required
def project_history(request, pk):
    project = get_object_or_404(Project, pk=pk)
    logs = (ProjectLog.objects.filter(project_id=project.id)
            .select_related('user').order_by('-created_at'))
    return render(request, 'history/project-history.html', {
        'project': project,
        'logs': Paginator(logs, 20).get_page(request.GET.get('page', 1)),
    })
