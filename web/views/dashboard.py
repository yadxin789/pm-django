# -*- coding: utf-8 -*-
"""仪表盘视图（对齐 DashboardController）。"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from core.services import dashboard as dash_svc


@login_required
def index(request):
    user = request.user
    if user.is_admin():
        data = dash_svc.get_admin_dashboard()
        return render(request, 'dashboard/admin.html', data)
    elif user.is_project_manager():
        return render(request, 'dashboard/index.html', dash_svc.project_manager_dashboard())
    else:
        data = dash_svc.get_user_dashboard(user)
        return render(request, 'dashboard/index.html', data)
