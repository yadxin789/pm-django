# 得道未来研发项目管理系统（Django 迁移版）

将原 Laravel（PHP）版 `pm-server-openEuler` 项目管理系统整体迁移到 **Django 5.2 LTS**，
部署于 openEuler（自带 Python 3.11）。**功能、布局、样式与原系统完全一致，用户感知不到语言替换。**

---

## 一、技术栈

| 项 | 选型 |
|---|---|
| Web 框架 | Django 5.2.17（LTS） |
| 运行时 | Python 3.11.6（openEuler 自带，无需额外安装） |
| 数据库 | MySQL 8.4（utf8mb4，新库 `pm_db_django`） |
| 驱动 | PyMySQL（`pm/__init__.py` 已 `install_as_MySQLdb`） |
| 异步/定时 | Celery 5.6 + Celery Beat（Redis 做 broker；为将来扩展复杂任务预留） |
| 部署 | systemd + gunicorn（最贴合 openEuler、一键部署、git 推送更新最快） |
| 前端 | 原生 Bootstrap + jQuery + ECharts（静态资源从参考 `public/` 原样拷贝） |

> 数据库表结构以参考目录 `database/migrations/*.php` 为唯一真实来源手工建模（非本地库反推）。

### AI 智能助手（对齐参考目录 2026-10-19 新增模块）

- 管理员在「AI 智能助手 → 开通引导 / AI 设置」粘贴任一 OpenAI 兼容提供商（DeepSeek / 通义千问 / 豆包 / 腾讯混元）的 API Key 并启用，全员共享使用。
- 聊天页通过 Function Calling 调用本地工具，直接查项目、任务、用户、部门、知识库、文件、待办等 PM 真实数据，不编造。
- 「生成文档」页按 5 套模板（立项报告 / 研发周报 / 会议纪要 / 需求文档 / 验收报告）结合自然语言描述与知识库生成 Markdown 文档；前端禁用按钮 + 服务端在途锁防重复提交。
- 每用户限流（每分钟 / 每日，管理员在设置页可调，DB 计数跨 gunicorn worker 一致）。
- 数据 REST API：`/api/ai/*`（meta / overview / projects / tasks / users / departments / knowledge / files / todos / templates），请求头 `X-AI-Key` 或参数 `api_key`，密钥取自环境变量 `AI_API_KEY`。

---

## 二、目录结构

```
pm-openEuler-django/
├── manage.py
├── requirements.txt        # 依赖清单
├── deploy.sh               # openEuler 一键部署/更新脚本
├── deploy/                 # systemd 单元（gunicorn / celery / celery-beat）
├── pm/                     # 工程配置
│   ├── settings.py         # 数据库/时区/静态/Celery 配置
│   ├── celery.py           # Celery 应用实例
│   └── urls.py
├── core/                   # 模型、认证后端、服务、context processor、seed 命令
│   └── services/           # throttle / operation_log / dashboard / project / task / extension ...
├── web/                    # 视图与路由
│   ├── urls.py             # URL name 沿用 Laravel 带点命名
│   ├── views/              # 各业务模块视图（对齐 Laravel Controller）
│   └── templatetags/pm_tags.py
├── templates/              # Django 模板（对齐 Laravel blade，布局一致）
├── static/                 # 静态资源（参考 public/ 拷贝）
└── media/                  # 上传文件（聊天文件、头像）
```

---

## 三、本地开发环境（Windows）

### 0. 前置要求

- Python 3.11+（推荐 3.13，已加入 PATH）
- MySQL 8.x（本机 127.0.0.1:3306）
- Redis（可选，仅 Celery worker/beat 需要；不启动不影响 Web 访问）

### 1. 首次初始化（仅一次，手动按序执行）

```powershell
cd E:\my-pm\pm-openEuler-django

# (1) 创建虚拟环境
python -m venv .venv

# (2) 激活
.\.venv\Scripts\Activate.ps1
#   若提示执行策略限制，先运行：
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# (3) 安装依赖
pip install -r requirements.txt

# (4) 建库（默认 root/root，可在 settings.py 改）
mysql -uroot -proot -e "CREATE DATABASE IF NOT EXISTS pm_db_django DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# (5) 迁移建表
python manage.py migrate

# (6) 灌入预置数据（角色/部门/状态/任务类型/58 用户，密码均为 123456）
python manage.py seed_data
```

### 2. 日常启动（之后每次开发）

在项目目录运行一键脚本：

```powershell
.\start.bat
```

脚本仅负责启动服务，不再重复装依赖/迁移。它会弹出三个独立窗口：

| 窗口 | 命令 | 作用 |
|---|---|---|
| PM-Web | `python manage.py runserver 0.0.0.0:8000` | Web 服务 |
| PM-Celery-Worker | `celery -A pm worker -l info -P solo` | 异步队列（Windows 下用 solo 池） |
| PM-Celery-Beat | `celery -A pm beat -l info` | 定时调度（数据库调度） |

浏览器访问 `http://127.0.0.1:8000`，默认账号 `admin` / `123456`。

> 停止服务：关闭弹出的三个命令行窗口即可。
> 拉了新代码、改了模型或更新了 `requirements.txt`，再回到第 1 节手动跑 `pip install -r requirements.txt` 和 `python manage.py migrate`。

### 3. 添加依赖

```powershell
# 先激活 venv，再安装新包
.\.venv\Scripts\Activate.ps1
pip install 某个包

# 写入 requirements.txt（锁定版本）
pip freeze | Select-String "包名" >> requirements.txt
```

---

## 四、一键部署（openEuler）

服务器为 openEuler，使用 **systemd + gunicorn + celery**，部署目录 `/var/www/pm-django`。

### 1. 打包项目

将项目打成 ZIP 文件 `pm-openEuler-django.zip`，拷入 U 盘。

### 2. 服务器部署

U 盘插入服务器 USB 口，以 root 执行：

```bash
# 1. 主系统（gunicorn + celery worker + celery beat）
./deploy.sh

# 2. 协同编辑（OnlyOffice Document Server，可选）
./deploy/deploy-onlyoffice.sh

# 3. 电话会议（Janus + coturn，可选）
./deploy/deploy-voice.sh
```

> 若 U 盘未自动识别，可手动指定 ZIP 路径：`./deploy.sh /绝对路径/pm-openEuler-django.zip`
> 脚本幂等，重复执行不会破坏已有部署。

### 3. 部署完成后

- 访问地址：`http://<服务器IP>:8000`
- 项目目录：`/var/www/pm-django`
- 数据库：`pm_db_django`（root / root）
- OnlyOffice：`http://<服务器IP>:8080`（协同编辑）
- 电话会议：`http://<服务器IP>:8000/project/<ID>/voice`

### 4. systemd 服务管理

| 服务名 | 作用 |
|---|---|
| `pm-django` | gunicorn 运行 Web（workers=3，监听 0.0.0.0:8000） |
| `pm-celery` | Celery worker（异步任务） |
| `pm-celery-beat` | 定时调度（基于数据库） |

常用命令：

```bash
systemctl status pm-django pm-celery pm-celery-beat   # 查看状态
systemctl restart pm-django                            # 重启 Web
journalctl -u pm-django -f                            # 查看 Web 日志
tail -f /var/www/pm-django/logs/gunicorn-error.log
```

### 5. 更新代码

重新打包 ZIP 拷入 U 盘，覆盖服务器 `/var/www/pm-django/` 后执行：

```bash
cd /var/www/pm-django
./venv/bin/pip install -r requirements.txt
./venv/bin/python manage.py migrate --noinput
./venv/bin/python manage.py collectstatic --noinput
systemctl restart pm-django pm-celery pm-celery-beat
```

> 环境变量优先读 `/var/www/pm-django/.env`（脚本自动生成）。

### 4. Celery 定时任务（对齐 Laravel 调度）

本地/服务器初始化调度（幂等）：

```bash
./venv/bin/python manage.py seed_beat
```

| Beat 任务 | 调度 | 对应 Laravel 调度 |
|---|---|---|
| `pm.tasks.refresh_all_projects_status` | 每 60 秒 | `projects:refresh-status`（参考为每 3 秒，可调） |
| `pm.tasks.check_upcoming_deadlines` | 每天 08:00 | 未来 3 天到期提醒 |
| `pm.tasks.check_overdue_tasks` | 每 30 分钟 | 任务逾期检查 |
| `pm.tasks.check_audit_overdue` | 每天 10:00/15:00 | 审核超时提醒 |
| `pm.tasks.check_overdue_projects` | 每天 01:00 | 项目逾期检查 |

任务注册于 `core/tasks.py`，由 `pm/celery.py`（autodiscover_tasks）自动发现；调度走 `django_celery_beat` DatabaseScheduler，部署的 `pm-celery.service` 与 `pm-celery-beat.service` 分别承载 worker 与调度器（依赖 Redis）。

---

## 五、运维脚本（数据库备份 / 服务巡检）更新流程

服务器上的两个运维组件 —— **MySQL 每日备份**与 **pm-\* 服务健康巡检** —— 只在本地 `deploy/` 目录维护，
通过安装器 `deploy/apply-on-server.sh` 一键推送。**不要在服务器上直接改这两个脚本**，下次重推会被覆盖。

### 1. 文件对应关系

| 本地（唯一真实来源） | 服务器落地路径 | 权限 |
|---|---|---|
| `deploy/pm-mysql-backup.sh` | `/etc/cron.daily/pm-mysql-backup` | 0755 |
| `deploy/pm-monitor.sh` | `/usr/local/bin/pm-monitor.sh` | 0755 |
| `deploy/pm-monitor.service` | `/etc/systemd/system/pm-monitor.service` | 0644 |
| `deploy/pm-monitor.timer` | `/etc/systemd/system/pm-monitor.timer` | 0644 |
| `deploy/apply-on-server.sh` | `/root/pm-stage/apply-on-server.sh`（安装器，不落到系统路径） | — |

服务器暂存目录为 `/root/pm-stage/`。安装器执行时**要求这 4 个待安装文件齐全**，缺一个就中止（此时系统不会被改动）。

**其他运维脚本**（同样是「scp 到 `/root/pm-stage/` → `ssh host bash 脚本`」，本身不落到系统路径，故不参与上面的安装器校验）：

| 本地 | 用途 | 落地产物 |
|---|---|---|
| `deploy/setup-nginx-80.sh` | 反代第 1 步：Nginx 监听 80 → 转发 `127.0.0.1:8000` | `/etc/nginx/conf.d/pm.conf`、firewalld 放行 `80/tcp`、`nginx` enable |
| `deploy/harden-gunicorn-bind.sh` | 反代第 2 步：gunicorn 收紧为只监听回环 | `/etc/systemd/system/pm-django.service.d/bind.conf` |
| `deploy/close-legacy-8000.sh` | 清理：撤掉已失效的 8000 历史放行 | 删除 firewalld 中 8000 的 rich rule |

### 2. 一次性准备：配置免密登录

```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@192.168.2.102 "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && echo INSTALLED"
```

看到 `INSTALLED` 即成功（只需输一次 root 密码）。验证：

```powershell
ssh -o BatchMode=yes root@192.168.2.102 hostname
```

### 3. 标准流程：改本地 → 一条命令重推

```powershell
# 改完本地 E:\my-pm\pm-openEuler-django\deploy\ 下的文件后，上传 + 安装（一条命令）
$stage = "E:\my-pm\pm-openEuler-django\deploy"
scp -o BatchMode=yes "$stage\pm-mysql-backup.sh" "$stage\pm-monitor.sh" "$stage\pm-monitor.service" "$stage\pm-monitor.timer" "$stage\apply-on-server.sh" root@192.168.2.102:/root/pm-stage/
ssh -o BatchMode=yes root@192.168.2.102 bash /root/pm-stage/apply-on-server.sh
```

只改了一个脚本时，也可只上传那一个（其余文件已在暂存目录中）：

```powershell
scp -o BatchMode=yes "E:\my-pm\pm-openEuler-django\deploy\pm-monitor.sh" root@192.168.2.102:/root/pm-stage/
ssh -o BatchMode=yes root@192.168.2.102 bash /root/pm-stage/apply-on-server.sh
```

> 若服务器 IP 变更，把上面命令里的 `192.168.2.102` 换成新地址即可（部署机地址与第「四」节一致）。

### 4. 安装器做了什么（幂等，可反复执行）

| 步骤 | 动作 |
|---|---|
| 0 | 检查 `$STAGE` 下 4 个文件是否齐全，缺则中止 |
| 1 | `sed -i 's/\r$//'` 统一去掉 Windows CRLF |
| 2 | `install -m 0755/0644` 安装到上表的目标路径 |
| 3 | `file` 校验换行、`bash -n` 校验 shell 语法 |
| 4 | `systemctl daemon-reload` + 重启 `pm-monitor.timer`，并确认其 enabled |
| 5 | 检查 `/root/.my.cnf`、`/var/www/pm-django/.env` 及 `.env` 中的 `DB_NAME` |
| 6 | **真跑一次备份**，打印备份日志与 `/var/backups/pm-mysql/` |
| 7 | 跑一次巡检脚本，并 `systemctl start pm-monitor.service` 确认 `Result=success` |
| 8 | 汇总 5 个服务的 `is-active` 与巡检 timer 的下次触发时间 |

结尾输出 `==== 全部检查通过（FAIL=0）====` 即为成功；出现任何 `[FAIL]` 行时按行处理，脚本退出码为 1。

> 第 6 步会真实生成一个备份文件（约 21 KB），属预期行为。

### 5. 生产注意事项

- **换行**：本地在 Windows 编辑后可能是 CRLF，安装器会自动归一；若绕过安装器手动 `scp`，务必先 `dos2unix`。
- **不要用复杂的一行 ssh 命令**：PowerShell 会对含空格、`$(...)`、`&&`、引号的参数二次引用，远端 bash 报
  `寻找匹配的 ' 时遇到了未预期的 EOF`。一律走「`scp` 上传文件 + `ssh host bash 脚本`」两段式。
- **`/etc/cron.daily/` 的触发机制**：openEuler 由 anacron 驱动（`/etc/anacrontab` 的 `1 5 cron.daily nice run-parts /etc/cron.daily`
  → 每天约 **03:05 ~ 03:50** 执行，`RANDOM_DELAY=45`、`START_HOURS_RANGE=3-22`；`/etc/cron.d/dailyjobs` 那行因
  `/etc/cron.hourly/0anacron` 存在而被跳过，属正常现象）。`run-parts` 会跳过文件名含 `.` 的文件，
  所以脚本名是 `pm-mysql-backup`（**无扩展名，不要改成 `.sh`**）。
- **日志**：`/var/log/pm-mysql-backup.log`（备份）、`/var/log/pm-monitor.log`（未装 `mail` 时告警降级落盘）、
  `journalctl -t pm-monitor`（告警记录）。
- **凭据**：备份走 `/root/.my.cnf`（mode 600），命令行不出现密码，`ps -ef` 不会泄露。
- **保留策略**：备份保留 7 天，由脚本内 `find ... -mtime +7 -delete` 自动清理。

### 6. 服务器侧常用校验

```bash
systemctl list-timers pm-monitor.timer --no-pager        # 巡检下次触发时间
systemctl show -p Result -p ExecMainStatus pm-monitor.service
tail -20 /var/log/pm-mysql-backup.log                    # 备份结果
ls -lh /var/backups/pm-mysql/                            # 备份产物
bash -n /etc/cron.daily/pm-mysql-backup                  # 语法自检
```

### 7. Nginx 反向代理（去掉 URL 里的 `:8000`）

gunicorn 原本直接监听 `0.0.0.0:8000`，而浏览器不带端口时访问的是 80，所以过去必须写 `IP:8000`。现由 Nginx 接管 80：

```
浏览器 → Nginx:80 ─┬─ /static/ · /media/ → 直接读磁盘（/var/www/pm-django/…）
                   └─ 其他请求           → 127.0.0.1:8000（gunicorn）
```

按顺序执行（三步都幂等，可重复跑）：

```powershell
$stage = "E:\my-pm\pm-openEuler-django\deploy"
scp -o BatchMode=yes "$stage\setup-nginx-80.sh" "$stage\harden-gunicorn-bind.sh" "$stage\close-legacy-8000.sh" root@192.168.2.102:/root/pm-stage/
ssh -o BatchMode=yes root@192.168.2.102 bash /root/pm-stage/setup-nginx-80.sh
ssh -o BatchMode=yes root@192.168.2.102 bash /root/pm-stage/harden-gunicorn-bind.sh
ssh -o BatchMode=yes root@192.168.2.102 bash /root/pm-stage/close-legacy-8000.sh
```

三个要点：

- `pm.conf` 用 `listen 80 default_server;` 抢占 80 的默认虚拟主机，**未修改发行版主配置** `/etc/nginx/nginx.conf`；`server_name` 取 `pm-django` 而非 `_`，避免与自带的欢迎页 server 块同名告警（`conflicting server name "_" on 0.0.0.0:80, ignored`）。
- `bind.conf` 用 drop-in 覆盖 `ExecStart`（systemd 的 `ExecStart` 只能整体重设、不能追加），因此即便 `deploy.sh` 重新生成 `pm-django.service`，收紧也不会丢。
- gunicorn 的 access log 从此记录的是 `127.0.0.1`，**真实客户端 IP 在 `/var/log/nginx/pm-access.log`**。

回滚：

```bash
# 恢复 8000 直连
rm -rf /etc/systemd/system/pm-django.service.d/bind.conf
systemctl daemon-reload && systemctl restart pm-django
# 停掉反代
systemctl disable --now nginx
# 还原防火墙
firewall-cmd --permanent --remove-port=80/tcp && firewall-cmd --reload
```

> 后续若要上 HTTPS：`pm.conf` 增加 `listen 443 ssl` 的 server 块、80 块改为 `return 301 https://$host$request_uri;`；同时把 `settings.py` 的 `DEBUG` 改为 `False`（并收紧 `ALLOWED_HOSTS`、将 `SECRET_KEY` 移入 `.env`），执行 `collectstatic` 后把 `/static/` 的 `alias` 从 `static/` 改成 `staticfiles/`（即 `STATIC_ROOT`）。

---

## 六、默认预置数据

- 角色：管理员、项目监督人、项目经理、任务执行人、设计、职员等（6 种）
- 部门：13 个；状态：13 种；任务类型：37 种
- 用户：58 名，密码统一 `123456`；管理员账号 `admin` / `123456`

---

## 七、与 Laravel 原版的一致性说明

- URL 命名沿用 Laravel 带点命名（`project.show`、`task.show` 等），保证模板路由一致。
- 侧边栏/全局布局、登录注册渐变样式、各模块页面布局与原版逐页对齐。
- 状态字符串与颜色：项目/任务各状态 label 与 badge 颜色与 Laravel `statuses` 完全一致。
- 聊天维持 10 秒轮询、欢迎页 ECharts 可视化，均沿用原版交互。
