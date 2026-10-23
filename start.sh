#!/usr/bin/env bash
# ============================================================================
#  PM 项目管理系统 (Django) - Linux / openEuler 启动脚本
#
#  说明：项目里的 start.bat 是 Windows 批处理（.bat），在 openEuler/Linux 上
#        用 bash 执行会报 “未找到命令 / 参数太多 / 未预期的符号” 等错误，
#        在服务器上请使用本脚本 start.sh。
#
#  用法：
#     ./start.sh              # 启动全部（Web + Celery worker + Celery beat）
#     ./start.sh web          # 仅启动 Web
#     ./start.sh celery       # 仅启动 Celery worker + beat
#     ./start.sh stop         # 停止全部
#     ./start.sh restart      # 重启全部
#     ./start.sh status       # 查看运行状态
#     ./start.sh setup        # 首次初始化（建 venv / 装依赖 / 建库 / 迁移 / 预置数据）
#
#  可选环境变量：
#     PORT=8000 ./start.sh        # 指定 Web 端口（默认 8000）
#     HOST=0.0.0.0 ./start.sh     # 指定监听地址（默认 0.0.0.0）
#     PROD=1 ./start.sh           # 用 gunicorn 代替 runserver（生产模式）
# ============================================================================

set -u

PM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PM_DIR"

LOG_DIR="$PM_DIR/logs"
RUN_DIR="$PM_DIR/run"
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

mkdir -p "$LOG_DIR" "$RUN_DIR"

c_blue='\033[1;34m'; c_yellow='\033[1;33m'; c_red='\033[1;31m'; c_off='\033[0m'
log()  { printf "${c_blue}[INFO]${c_off} %s\n" "$*"; }
warn() { printf "${c_yellow}[WARN]${c_off} %s\n" "$*"; }
err()  { printf "${c_red}[ERROR]${c_off} %s\n" "$*"; }

usage() {
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------------------
# 加载项目 .env（settings.py 只读环境变量，不会自动读取 .env 文件）
# ---------------------------------------------------------------------------
load_env() {
    [ -f "$PM_DIR/.env" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|\#*) continue ;; esac
        key="${line%%=*}"
        val="${line#*=}"
        [ -n "$key" ] || continue
        export "$key=$val"
    done < "$PM_DIR/.env"
}

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
find_python() {
    for p in "$PM_DIR/venv/bin/python" "$PM_DIR/.venv/bin/python"; do
        [ -x "$p" ] && { printf '%s' "$p"; return 0; }
    done
    command -v python3 2>/dev/null && return 0
    return 1
}

port_open() {
    (exec 3<>"/dev/tcp/$1/$2") >/dev/null 2>&1
}

pid_alive() {
    local pidfile="$1" pid
    [ -f "$pidfile" ] || return 1
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

ensure_env() {
    load_env
    DB_HOST="${DB_HOST:-127.0.0.1}"
    DB_PORT="${DB_PORT:-3306}"
    DB_NAME="${DB_NAME:-pm_db_django}"

    PY="$(find_python || true)"
    if [ -z "${PY:-}" ]; then
        err "未找到 Python 解释器。请先 dnf install -y python3 python3-pip python3-devel gcc"
        exit 1
    fi
    if ! "$PY" -c 'import django' >/dev/null 2>&1; then
        err "当前解释器未安装 Django 依赖：$PY"
        echo "        请先执行：./start.sh setup     （自动建 venv 并安装 requirements.txt）"
        exit 1
    fi
    [ -f "$PM_DIR/.env" ] || warn "未找到 .env，将使用默认数据库配置（root/root@127.0.0.1:3306/pm_db_django）"
}

# ---------------------------------------------------------------------------
# 基础设施（MySQL / Redis）探活，必要时尝试拉起
# ---------------------------------------------------------------------------
ensure_infra() {
    if ! port_open "$DB_HOST" "$DB_PORT"; then
        warn "MySQL ${DB_HOST}:${DB_PORT} 未监听，尝试启动数据库服务..."
        systemctl start mysqld >/dev/null 2>&1 || systemctl start mariadb >/dev/null 2>&1 || true
        sleep 3
        port_open "$DB_HOST" "$DB_PORT" || warn "MySQL 仍不可用，Web 可能报数据库连接错误"
    fi
    if ! port_open 127.0.0.1 6379; then
        warn "Redis 127.0.0.1:6379 未监听，尝试启动 redis ..."
        systemctl start redis >/dev/null 2>&1 || true
        sleep 1
    fi
}

# ---------------------------------------------------------------------------
# 进程启停
# ---------------------------------------------------------------------------
start_proc() {
    local name="$1"; shift
    local pidfile="$RUN_DIR/$name.pid"
    if pid_alive "$pidfile"; then
        warn "$name 已在运行 (PID $(cat "$pidfile"))，跳过"
        return 0
    fi
    if command -v setsid >/dev/null 2>&1; then
        nohup setsid "$@" >>"$LOG_DIR/$name.log" 2>&1 &
    else
        nohup "$@" >>"$LOG_DIR/$name.log" 2>&1 &
    fi
    echo $! >"$pidfile"
    sleep 2
    if pid_alive "$pidfile"; then
        log "$name 已启动 (PID $(cat "$pidfile"))  日志: logs/$name.log"
    else
        err "$name 启动失败，请查看 logs/$name.log"
        tail -n 15 "$LOG_DIR/$name.log" 2>/dev/null || true
        rm -f "$pidfile"
        return 1
    fi
}

stop_proc() {
    local name="$1"
    local pidfile="$RUN_DIR/$name.pid"
    if ! pid_alive "$pidfile"; then
        warn "$name 未在运行"
        rm -f "$pidfile"
        return 0
    fi
    local pid
    pid="$(cat "$pidfile")"
    kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
    log "$name 已停止"
}

status() {
    local name pidfile
    echo "服务状态（项目目录: $PM_DIR）:"
    for name in web celery-worker celery-beat; do
        pidfile="$RUN_DIR/$name.pid"
        if pid_alive "$pidfile"; then
            printf '  %-14s 运行中 (PID %s)\n' "$name" "$(cat "$pidfile")"
        else
            printf '  %-14s 已停止\n' "$name"
        fi
    done
    echo
    echo "端口监听:"
    (ss -lntp 2>/dev/null || netstat -lntp 2>/dev/null) | grep -E ":(8000|6379|3306)\b" || echo "  （8000/6379/3306 均未监听）"
}

# ---------------------------------------------------------------------------
# 启动 Web / Celery
# ---------------------------------------------------------------------------
start_web() {
    if port_open 127.0.0.1 "$PORT"; then
        warn "端口 $PORT 已被占用，Web 可能已在运行"
    fi
    if [ "${PROD:-0}" = "1" ]; then
        if [ ! -x "$PM_DIR/venv/bin/gunicorn" ]; then
            err "PROD=1 需要 gunicorn，请执行 ./venv/bin/pip install gunicorn"
            exit 1
        fi
        start_proc web "$PM_DIR/venv/bin/gunicorn" pm.wsgi:application \
            --bind "$HOST:$PORT" --workers 3 --threads 2 --timeout 120 \
            --access-logfile "$LOG_DIR/gunicorn-access.log" \
            --error-logfile "$LOG_DIR/gunicorn-error.log"
    else
        start_proc web "$PY" manage.py runserver "$HOST:$PORT" --noreload
    fi
}

start_celery() {
    if ! port_open 127.0.0.1 6379; then
        warn "Redis 不可用，跳过 Celery（worker / beat）。可执行：systemctl start redis"
        return 0
    fi
    local -a cbin
    if [ -x "$PM_DIR/venv/bin/celery" ]; then
        cbin=("$PM_DIR/venv/bin/celery")
    else
        cbin=("$PY" -m celery)
    fi
    start_proc celery-worker "${cbin[@]}" -A pm worker --loglevel=info --concurrency=2
    start_proc celery-beat   "${cbin[@]}" -A pm beat   --loglevel=info \
        --scheduler django_celery_beat.schedulers:DatabaseScheduler \
        -s "$RUN_DIR/celerybeat-schedule"
}

start_all() {
    echo "============================================"
    echo " PM 项目管理系统 (Django) - 启动中"
    echo "============================================"
    ensure_env
    ensure_infra
    start_web
    start_celery
    echo
    local ip
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "============================================"
    echo " 访问地址 : http://${ip:-127.0.0.1}:$PORT"
    echo " 后台管理 : http://${ip:-127.0.0.1}:$PORT/admin/   (admin / 123456)"
    echo " 日志目录 : $LOG_DIR"
    echo " 查看状态 : ./start.sh status"
    echo " 停止服务 : ./start.sh stop"
    echo "============================================"
}

stop_all() {
    stop_proc celery-beat
    stop_proc celery-worker
    stop_proc web
}

# ---------------------------------------------------------------------------
# 首次初始化（等价于 Windows 下 start.bat 提示的那 6 步）
# ---------------------------------------------------------------------------
do_setup() {
    echo "============================================"
    echo " PM 项目管理系统 - 首次初始化"
    echo "============================================"
    command -v python3 >/dev/null 2>&1 || {
        err "未安装 python3。请先：dnf install -y python3 python3-pip python3-devel gcc"
        exit 1
    }

    if [ ! -x "$PM_DIR/venv/bin/python" ]; then
        log "[1/5] 创建虚拟环境 venv ..."
        python3 -m venv "$PM_DIR/venv" || { err "创建 venv 失败"; exit 1; }
    else
        log "[1/5] venv 已存在，跳过创建"
    fi

    log "[2/5] 安装 Python 依赖 ..."
    "$PM_DIR/venv/bin/pip" install -U pip >/dev/null 2>&1 || true
    "$PM_DIR/venv/bin/pip" install -r "$PM_DIR/requirements.txt" \
        || { err "依赖安装失败，请检查网络 / pip 源"; exit 1; }
    "$PM_DIR/venv/bin/pip" install gunicorn >/dev/null 2>&1 || true

    load_env
    DB_NAME="${DB_NAME:-pm_db_django}"
    DB_USER="${DB_USER:-root}"
    DB_PASSWORD="${DB_PASSWORD:-root}"
    DB_HOST="${DB_HOST:-127.0.0.1}"
    DB_PORT="${DB_PORT:-3306}"

    log "[3/5] 生成 .env 并创建数据库 ${DB_NAME} ..."
    if [ ! -f "$PM_DIR/.env" ]; then
        cat >"$PM_DIR/.env" <<EOF
DB_NAME=$DB_NAME
DB_USER=$DB_USER
DB_PASSWORD=$DB_PASSWORD
DB_HOST=$DB_HOST
DB_PORT=$DB_PORT
DEBUG=False
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
EOF
        log "    已生成 .env"
    else
        log "    .env 已存在，保留原配置"
    fi

    if command -v mysql >/dev/null 2>&1; then
        if mysql -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" -p"$DB_PASSWORD" -e "SELECT 1" >/dev/null 2>&1; then
            mysql -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" -p"$DB_PASSWORD" \
                -e "CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            log "    数据库 ${DB_NAME} 就绪"
        elif mysql -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" -e "SELECT 1" >/dev/null 2>&1; then
            mysql -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" \
                -e "CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            log "    数据库 ${DB_NAME} 就绪（密码为空）"
        else
            warn "    无法连接 MySQL，跳过建库。请手动执行："
            echo "        mysql -uroot -p -e \"CREATE DATABASE IF NOT EXISTS $DB_NAME DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\""
        fi
    else
        warn "    未找到 mysql 客户端，跳过建库"
    fi

    log "[4/5] 数据库迁移 migrate ..."
    (cd "$PM_DIR" && "$PM_DIR/venv/bin/python" manage.py migrate --noinput) \
        || { err "migrate 失败，请检查数据库连接"; exit 1; }

    log "[5/5] 写入预置数据与静态文件 ..."
    (cd "$PM_DIR" && "$PM_DIR/venv/bin/python" manage.py seed_data) || warn "seed_data 失败（若已初始化过可忽略）"
    (cd "$PM_DIR" && "$PM_DIR/venv/bin/python" manage.py seed_beat) || true
    (cd "$PM_DIR" && "$PM_DIR/venv/bin/python" manage.py collectstatic --noinput) >/dev/null 2>&1 \
        || warn "collectstatic 失败（可忽略）"

    echo
    log "初始化完成，执行 ./start.sh 启动服务"
}

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
case "${1:-all}" in
    all)              start_all ;;
    web)              ensure_env; start_web ;;
    celery)           ensure_env; start_celery ;;
    stop)             stop_all ;;
    restart)          stop_all; sleep 1; start_all ;;
    status)           status ;;
    setup)            do_setup ;;
    -h|--help|help)   usage ;;
    *) err "未知参数: $1"; echo; usage; exit 1 ;;
esac
