# pm 项目管理系统：Laravel → Django 迁移计划

> 目标：将 `E:\my-pm\pm-server-openEuler`（已运行的 Laravel 12 项目管理系统）迁移到 Django，部署于 openEuler（自带 python3）。**功能、布局、样式保持不变**。
> 本计划仅作规划，**尚未开始编写代码**，待确认后实施。

---

## 0. 结论

**可行。** 关键判断依据：

1. **前端布局/样式可完整保真**：现有前端是「Blade 模板 + Tailwind CSS 4 + Vite 构建产物 + FontAwesome」。Blade 与 Django 模板语法高度相似（`{{ }}` 插值、`@foreach/@if` 对应 `{% for %}/{% if %}`），现有 HTML 结构与 Tailwind 类可直接套用；`public/build` 下已是编译好的静态资源，可原样复用。布局样式可以做到肉眼无差别。
2. **数据可保留**：现库为 MySQL `pm_db`，表结构可用 Django `inspectdb` 反向生成模型，历史数据可直接沿用同一数据库（或迁移到新库）。
3. **业务层需按语义重写（工作量核心）**：认证、权限(RBAC)、状态机、文件/媒体、Excel、队列、OnlyOffice/企微/语音集成，都需要从 Laravel 生态翻译为 Django 生态。这部分是迁移的重头。

---

## 1. 现状盘点

### 1.1 原系统技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| 框架 | Laravel 12（PHP 8.3） | MVC，路由在 `routes/web.php` |
| 数据库 | MySQL `pm_db` | session/queue/cache 均落库（database 驱动） |
| 模板 | Blade | 94 个 `.blade.php` 视图，2 个布局（app/auth） |
| 前端样式 | Tailwind CSS 4 + Vite | 构建产物在 `public/build`；FontAwesome 字体 |
| 权限 | spatie/laravel-permission | 角色：admin、project_creator、project_manager、task_executor、task_auditor、support |
| 状态机 | spatie/laravel-model-states | Project 7 态、Task 7 态 |
| 文件 | spatie/laravel-medialibrary | 上传文件管理 |
| 表格导入 | maatwebsite/excel | 用户批量导入 |
| 图像 | intervention/image | 头像处理 |
| 队列/调度 | Laravel database queue + schedule | 3 个 Job（逾期检查、状态刷新、批量通知） |
| 实时 | 聊天为 10s 轮询；语音用外部 Swoole 信令 + webhook | 非 websocket |

### 1.2 规模

- 模型：30 个 Eloquent（User/Project/Task/Todo/File/Department/Role/Message/Notification/Knowledge/CollabDocument/Voice* /Wecom* /OperationLog/ProjectTemplate 等）
- 控制器：24 个；服务类：11 个；Jobs：3 个
- 数据库迁移：39 个；视图：94 个

### 1.3 功能模块清单（源自路由）

认证、首页、仪表盘、项目管理（甘特图/变更/暂停恢复/状态拆分列表）、任务管理（执行/审核/延期申请/文件上传）、待办、文件库（资源/部门/预览下载）、知识库、协作文档（OnlyOffice）、语音会议（电话会议）、聊天（群聊+私聊，轮询）、通知、个人中心（资料/密码/头像/企微绑定）、管理后台（用户/部门/角色/状态/任务类型/文件库/操作日志/通知/服务/备份/聊天记录/企微集成）、项目模板、项目履历、企业微信集成（OAuth 登录、消息通知、文件收发）。

---

## 2. 迁移策略（保布局/样式）

三条可选路径：

- **路径 A｜Django 模板化复用（推荐）**：把现有 94 个 Blade 视图「机械翻译」成 Django 模板，保留原 HTML 结构、Tailwind 类、内联 JS；`public/` 下构建产物、CSS、字体原样拷贝。布局样式 100% 一致，最贴合需求。
- **路径 B｜前后端分离**：Django 只做 JSON API，前端重构成 SPA。**不推荐**：会重写前端，布局样式难以保持，且工作量翻倍。
- **路径 C｜混合**：多数页面用 A，极个别动态页（甘特图、语音）保留原 JS 片段挂接 API。作为 A 的补充。

**选定路径 A（+ C 补强）**。前端资源 = 复用 `public/` 静态产物 + 翻译模板标签，样式零改动。

---

## 3. 分层迁移方案

### 3.1 环境与工程骨架
- 确认目标 openEuler 版本 → 确定自带 Python 版本 → 选对应 Django LTS（openEuler 22.03 / Py3.9 → Django 4.2；openEuler 24.03 / Py3.11 → Django 5.2）。**待确认**。
- 新工程根：`E:\my-pm\pm-openEuler-django`。
- 依赖清单（requirements.txt）：django、mysqlclient、Pillow、openpyxl（Excel）、celery + redis 或轻量 apscheduler（队列/调度）、django-apscheduler（可选）、requests（企微/OnlyOffice 回调）、PyJWT（OnlyOffice JWT 签名）。

### 3.2 数据库层（模型 + 迁移）
- 对现有 MySQL `pm_db` 用 `manage.py inspectdb` 反向生成 30 个 Model，核对字段/外键/唯一约束后人工校正。
- 保留软删除（原用 Eloquent SoftDeletes）：Django 用 `is_deleted`/`deleted_at` 字段 + QuerySet 过滤，或 django-softdelete。
- 历史数据沿用同一 `pm_db`（Django 只读既有表）——**数据零丢失**；新库方案作为备选。

### 3.3 认证与会话
- Laravel 自定义 `username+password` 登录、注册、登出、登录限流（5 次/15 分钟）→ Django `auth`（`USERNAME_FIELD='username'` 自定义 User 模型），重写 LoginView/RegisterView，保留限流逻辑。
- 企业微信 OAuth 登录/绑定：重写 `WecomAuthController` 回调逻辑，绑定 `wecom_userid` 字段。
- 会话落库：Django session 可配 `django.contrib.sessions`（库表），对齐原 database session。

### 3.4 权限与角色
- 原「自定义 roles 表 + Spatie 角色」双轨 → Django 统一 RBAC：`Role`（自定义表，保留 code）+ `User.role_id`，用自定义 `has_role` 混入方法还原 `isAdmin/isProjectManager/...` 等方法，并在视图装饰器/中间件校验。轻量自建即可，不必引入 django-guardian（原系统是整对象级角色，非逐对象 ACL）。

### 3.5 状态机
- Project 状态（NotStarted/InProgress/Suspended/Completed/Overdue/OverdueCompleted）、Task 状态（Pending/ForAudit/InProgress/Completed/Rejected/Overdue/OverdueCompleted）：
  - 方案：Django `CharField(choices=...)` + 状态流转校验方法（放 Model 上），或引入 `django-fsm` 还原 `transition` 语义。推荐自建轻量状态字段 + 流转守卫，映射 `States/*` 各类的 `can_transition` 与 `transitionTo`。

### 3.6 模板与前端资源
- 94 个 Blade → Django 模板翻译：`@extends('layouts.app')`→`{% extends 'layouts/app.html' %}`；`{{ $user->name }}`→`{{ user.name }}`；`@foreach/@if/@csrf/@auth` 等一一映射。`@csrf` 用 Django `{% csrf_token %}`。
- 布局 `app.blade.php`/`auth.blade.php` → Django 基模板，保留导航、侧栏、顶栏结构。
- 静态资源：整拷 `public/`（build、css、js、font-awesome、fonts、webfonts、avatars、storage、favicon）到 Django `static/`，用 `{% static %}` 引用；`vite manifest` 视需要保留或用 Django 静态收集。

### 3.7 路由与视图
- `routes/web.php` 全部路由按模块（admin/project/task/file/notification/wecom-file/todo/extension/support/project-template/knowledge/collab/voice/profile/history/messages）映射到 Django `urls.py` + `views.py`，URL 路径保持不变（保证书签/已部署环境一致）。
- 24 个控制器 → 对应 Django View（Function-Based 或 Class-Based），沿用原业务逻辑。

### 3.8 服务 / 业务逻辑
- 11 个 Services（Project/Task/File/Dashboard/Notification/Extension/Wecom/WecomNotification/CollabOffice/VoiceToken/TaskState）逐一翻译为 Django service 模块（`services/` 包），保持方法级行为一致。
- Helpers/Traits/Support/Policies 对应映射。

### 3.9 队列与调度
- 3 个 Job：逾期检查、项目状态刷新、批量通知 → Django 用 Celery（beat 定时）或轻量 `django-apscheduler`/APScheduler。聊天为轮询无需异步；企微批量通知可用 Celery 任务。

### 3.10 文件与上传
- medialibrary → Django `FileField`/`MediaStorage`，保留 File、FileVersion、DepartmentFile、WecomFile 等模型与目录结构；预览/下载/权限还原。
- 头像：intervention/image → Pillow 处理裁剪缩放。

### 3.11 外部服务集成（保持接口不变）
- **OnlyOffice 协同编辑**：PHP 侧仅负责 JWT 签名、`download`/`callback` 接口、doc_key 校验 → Django 用 PyJWT 签发同格式 JWT，复刻 download/callback 路由（无登录态、doc_key 校验），前端 OnlyOffice 配置 URL 不变。
- **企业微信**：OAuth 回调、应用消息/文件收发、回调验签（AES 加解密需用 python 复刻企微加解密协议）→ Django 重写 WecomService。
- **语音会议**：外部 Swoole 信令服务保持，Django 复刻 PHP 端 `hookEnter/hookLeave` webhook、token 签发（VoiceTokenService）、成员/文档配置/上传/历史接口。前端信令逻辑不变。

---

## 4. 模块迁移对照表（概览）

| 原模块（Laravel） | 迁移目标（Django） | 保真要点 |
|---|---|---|
| AuthController + User | Django auth + 自定义 User | username 登录、限流、企微登录 |
| AdminController（后台 8 子模块） | admin app 的 urls+views | 用户/部门/角色/状态/任务类型/日志/通知/备份 |
| ProjectController + Project | project app | 甘特图、状态拆分、变更、暂停恢复 |
| TaskController + Task/状态机 | task app + 状态守卫 | 执行/审核/延期/上传 |
| TodoController | todo app | 完成/延期审批 |
| FileController + medialibrary | file app + FileField | 预览/下载/部门文件 |
| KnowledgeController | knowledge app | 分类/文档/阅读 |
| Message/PrivateMessage（轮询） | chat app（轮询保持） | 群聊/私聊/清空 |
| NotificationController | notification app | 已读/全部已读/删除 |
| CollabDocumentController + CollabOfficeService | collab app + PyJWT | OnlyOffice 集成 |
| VoiceController + VoiceTokenService | voice app | Swoole webhook、token |
| Wecom*（4 控制器） | wecom app | OAuth/消息/文件/设置 |
| ProfileController | profile app | 资料/密码/头像/企微绑定 |
| HistoryController / ProjectTemplateController / SupportController / ExtensionController / DashboardController | 各自 app | 履历/模板/支持/延期审批/仪表盘 |

---

## 5. 关键技术映射表（Laravel → Django）

| Laravel 组件 | Django 对应 |
|---|---|
| Eloquent Model | `models.Model`（inspectdb 生成） |
| Blade 模板 | Django Template（标签翻译，结构/类不变） |
| 路由 `Route::get('x', [...])` | `path('x', view, name=...)`，URL 保持不变 |
| 控制器 | Function/Class-Based View |
| 中间件 `auth` | `login_required` / 自定义中间件 |
| spatie/permission | 自建 Role + has_role 混入 |
| spatie/model-states | CharField choices + 状态流转守卫 / django-fsm |
| spatie/medialibrary | `FileField` + MediaStorage |
| maatwebsite/excel | openpyxl / pandas |
| intervention/image | Pillow |
| database queue + schedule | Celery beat / django-apscheduler |
| 会话/缓存/队列（database） | Django session 落库；缓存可留 MySQL 或换 Redis |
| OnlyOffice JWT | PyJWT（保持同一 secret 格式） |
| 企微 AES 回调加解密 | python 复刻企微加解密协议 |
| SoftDeletes | deleted_at 字段 + QuerySet 过滤 |

---

## 6. 数据迁移

- 推荐：**直连现有 MySQL `pm_db`**，Django 模型映射既有表，历史数据原样可用，无需搬运。
- 备选（新库）：`inspectdb` 生成模型 → 建表 → 写脚本按表复制数据。
- 迁移前后做：表数量对比（39+）、行数抽样核对、外键完整性校验。

---

## 7. 阶段划分与工作量估算（供排期参考）

| 阶段 | 内容 | 相对工作量 |
|---|---|---|
| P0 环境与骨架 | openEuler/Django 版本确认、工程初始化、依赖、inspectdb 建模、静态资源落位 | 小 |
| P1 认证与权限 | 自定义 User、登录/注册/登出、RBAC、状态机守卫 | 中 |
| P2 核心业务 | 项目/任务/待办/文件/知识库 五大模块的 模型+视图+模板 | 大 |
| P3 协同与集成 | OnlyOffice、企业微信、语音、消息/私聊、通知 | 大 |
| P4 管理后台与杂项 | 后台 8 子模块、个人中心、履历、模板、支持、备份 | 中 |
| P5 队列/调度/运维 | Jobs 迁移、定时任务、部署脚本（对应原 deploy.sh）、验收测试 | 小 |

> 注释：94 个模板翻译 + 30 模型 + 24 控制器是全量迁移，工作量主要由 P2/P3 决定；保布局样式要求下，前端翻译是纯机械劳动，风险低但量大。

---

## 8. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 企微 AES 加解密协议在 Python 端细节多 | 企微回调/收发失败 | 先用官方加解密算法在 Python 单测复现，再接入 |
| OnlyOffice JWT 格式不一致 | 协同编辑打不开 | 先跑通同 secret 的签名互验（PHP 产 token ↔ Django 验） |
| 状态机流转漏改导致状态异常 | 项目/任务卡状态 | 用 django-fsm 或守卫集中校验，逐状态写迁移用例 |
| Blade→Django 标签翻译遗漏 | 个别页面报错/样式缺 | 94 个模板逐页验收，先跑通再批量 |
| openEuler Python 版本与 Django 版本匹配 | 无法启动 | 阶段 P0 先定版本，选 LTS 对齐 |
| 语音/Swoole 依赖旧 PHP 端点 | 语音失效 | 保持外部信令不变，仅重写 PHP 侧 webhook 为 Django |
| inspectdb 生成模型与 ORM 语义偏差 | 查询报错 | 人工校正外键/多对多/JSON 字段 |

---

## 9. 已确认决策（2026-09-30 确认，据此开工）

1. **Django / Python 版本**：目标 openEuler 24.03 SP4，自带 **Python 3.11.6** → 采用 **Django 5.2 LTS**。本地开发用 `py -3.13`（Python 3.13.7）建 venv 开发（兼容 Django 5.2），部署到服务器 3.11.6。
2. **数据库方案**：**新建库再导数据**。新库（`pm_db_django`）由 Django 迁移建表；表结构用 `inspectdb` 从现有 `pm_db` 反向生成（保证字段/外键全对齐）；预置数据参照参考目录 `database/seeders/` 构造（见第 10 节种子清单），以 Django management command 灌入。
3. **异步 / 定时**：**Celery + Celery Beat**（配 Redis），为将来扩展复杂任务留余地。聊天维持现有 **10s 轮询** 不变。
4. **部署方式**：**openEuler 上 systemd + gunicorn**（最贴合自带 python3、一键部署、git 推送更新最快）。提供等价于参考目录 `deploy.sh` 的一键部署脚本：`git pull → venv 依赖 → migrate → 收集静态 → 重启 gunicorn/celery`。
5. **总体原则**：功能、布局、样式与参考目录完全一致，维持用户使用习惯，让用户感知不到语言替换。

## 10. 预置种子数据清单（源自参考目录 seeder）

| 种子 | 内容 |
|---|---|
| RoleSeeder | 6 角色：admin 系统管理员 / project_creator 项目创建人 / project_manager 项目监督人 / task_executor 任务执行人 / task_auditor 任务审核人 / support 支援人员 |
| DepartmentSeeder | 13 部门：pm/product/appearance/structure/hardware/software/sample/quality/packaging（project 型）+ admin_dept/material/patent/certification（non-project 型） |
| TaskTypeSeeder | 37 个任务类型，按部门归属（产品/外观/结构/硬件/软件/样品/品质/包装各类型），`is_required=1` |
| UserSeeder | admin、pm_director + 各 project 部门 2 审核人 + 2 执行人 + 2 支援、各 non-project 部门 2 支援（默认密码 123456） |

---

*计划已定稿。按 P0→P5 顺序实施。*
