# -*- coding: utf-8 -*-
"""任务状态机服务（对齐 Laravel app/Services/TaskStateService.php）。

本项目状态机以「类名字符串」形式存在（Project.status / Task.status 字段），
不依赖 spatie/laravel-model-states，本服务负责：
  - 状态判定 helper（is_inactive / can_xxx …）
  - 状态流转校验
"""
from __future__ import annotations


# Project 状态
P_NOT_STARTED = 'App\\States\\Project\\NotStarted'
P_INPROGRESS = 'App\\States\\Project\\InProgress'
P_SUSPENDED = 'App\\States\\Project\\Suspended'
P_COMPLETED = 'App\\States\\Project\\Completed'
P_OVERDUE = 'App\\States\\Project\\Overdue'
P_OVERDUE_COMPLETED = 'App\\States\\Project\\OverdueCompleted'

# Task 状态
T_PENDING = 'App\\States\\Task\\Pending'
T_INPROGRESS = 'App\\States\\Task\\InProgress'
T_FORAUDIT = 'App\\States\\Task\\ForAudit'
T_COMPLETED = 'App\\States\\Task\\Completed'
T_REJECTED = 'App\\States\\Task\\Rejected'
T_OVERDUE = 'App\\States\\Task\\Overdue'
T_OVERDUE_COMPLETED = 'App\\States\\Task\\OverdueCompleted'


PROJECT_FINAL_STATES = {P_COMPLETED, P_OVERDUE_COMPLETED}
TASK_FINAL_STATES = {T_COMPLETED, T_OVERDUE_COMPLETED}
TASK_FORBIDDEN_STATES = {T_COMPLETED, T_OVERDUE_COMPLETED, T_FORAUDIT}


def is_project_inactive(status: str | None) -> bool:
    return status == P_SUSPENDED


def is_project_final(status: str | None) -> bool:
    return status in PROJECT_FINAL_STATES


def is_task_forbidden(status: str | None) -> bool:
    return status in TASK_FORBIDDEN_STATES


def is_task_final(status: str | None) -> bool:
    return status in TASK_FINAL_STATES


def default_project_status() -> str:
    return P_INPROGRESS


def default_task_status() -> str:
    return T_PENDING