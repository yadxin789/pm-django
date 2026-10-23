# -*- coding: utf-8 -*-
"""web 视图包（对应 Laravel app/Http/Controllers）。

新增：
  - admin_trash   管理员回收站
  - my_trash      个人回收站
  - private_message 私聊
"""
from . import (  # noqa: F401
    admin,
    admin_trash,
    auth,
    collab,
    dashboard,
    extension,
    file,
    history,
    knowledge,
    message,
    my_trash,
    notification,
    private_message,
    profile,
    project,
    project_template,
    support,
    task,
    todo,
    voice,
    wecom,
    wecomfile,
    welcome,
)