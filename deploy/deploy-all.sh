#!/bin/bash
# =============================================================================
# deploy-all.sh —— 全系统「总装」部署入口
#
# 之前每个能力都各自有一个脚本（deploy.sh / deploy-onlyoffice.sh /
# deploy-voice.sh / setup-nginx-* / harden-gunicorn-bind.sh /
# pm-mysql-backup.sh / pm-monitor.sh / apply-onlyoffice-fix.sh ...），
# 现场部署要靠人记住执行顺序，容易漏掉某一步。
#
# 本脚本把顺序固化下来，按依赖顺序依次调用，并支持按需跳过。
#
#   用法：
#     ./deploy/deploy-all.sh              # 全量总装
#     ./deploy/deploy-all.sh --core-only  # 只装主体
#     ./deploy/deploy-all.sh --skip mysql-backup,monitor
#
# 每一步都可重复执行（幂等），失败会中断并提示可直接查看的服务日志。
# =============================================================================
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

# ---------- 可调参数 ----------
SKIP_LIST=""
CORE_ONLY=""
DRY_RUN=""

# --skip=a,b,c    跳过指定步骤
# --core-only     只部署主体，不装任何可选组件
# --dry-run       只打印将要执行的步骤，不真正调用子脚本
while [ "$#" -gt 0 ]; do
    case "$1" in
        --core-only) CORE_ONLY="1" ;;
        --dry-run)   DRY_RUN="1" ;;
        --skip)      shift; SKIP_LIST="${1:-}" ;;
        --skip=*)    SKIP_LIST="${1#--skip=}" ;;
        -h|--help)
            echo "用法："
            echo "  $0                        全量总装"
            echo "  $0 --core-only            只部署主体"
            echo "  $0 --skip=voice,nginx     跳过指定步骤"
            echo "  $0 --dry-run              只打印步骤"
            echo
            echo "可跳过的步骤名：nginx harden onlyoffice voice"
            echo "                mysql-backup monitor legacy-port"
            exit 0
            ;;
        *) warn "忽略未知参数：$1" ;;
    esac
    shift
done

skip() { echo ",${SKIP_LIST}," | tr ',' '\n' | grep -qx "$1"; }

PM_DIR="${PM_DIR:-/var/www/pm-django}"

log()  { echo -e "\033[1;34m[TOTAL]\033[0m $*"; }
step() { echo; echo -e "\033[1;36m=====> $*\033[0m"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
fail() { echo -e "\033[1;31m[FAIL]\033[0m $*"; echo; echo "排查建议："; \
         echo "  journalctl -u pm-django -n 100 --no-pager"; \
         echo "  journalctl -u pm-celery -n 100 --no-pager"; \
         echo "  journalctl -u pm-celery-beat -n 100 --no-pager"; exit 1; }

run_step() {
    local desc="$1"; shift
    local script="$1"; shift || true
    step "${desc}"
    if [ ! -f "${script}" ]; then
        warn "脚本不存在，跳过：${script}"
        return 0
    fi
    if [ ! -x "${script}" ]; then chmod +x "${script}" 2>/dev/null || true; fi
    if [ -n "${DRY_RUN}" ]; then
        log "DRY-RUN 跳过执行：${script} $*"
        return 0
    fi
    if "${script}" "$@"; then
        log "完成：${desc}"
    else
        fail "失败：${desc}（脚本 ${script}）"
    fi
}

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        fail "本脚本需要 root 权限执行（涉及 systemd / nginx / MySQL）。请 sudo $0"
    fi
}

need_root

step "[0/8] 环境自检"
echo "  项目目录 : ${ROOT_DIR}"
echo "  部署目录 : ${PM_DIR}"
echo "  系统     : $(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME}" || uname -sr)"
echo "  当前用户 : $(id -un)"

# ---------------------------------------------------------------------------
# 1. 主体：Python 依赖 / MySQL / 代码落地 / .env / migrate / seed / systemd
# ---------------------------------------------------------------------------
run_step "[1/8] 主体部署（deploy.sh）" ./deploy.sh

if [ -n "${CORE_ONLY}" ]; then
    log "--core-only 已指定，跳过可选组件。"
    exit 0
fi

# ---------------------------------------------------------------------------
# 2. 反向代理与只监听本地端口（安全收口）
# ---------------------------------------------------------------------------
skip "nginx" || {
    run_step "[2/8] Nginx 反向代理 80 端口" ./deploy/setup-nginx-80.sh
}
skip "harden" || {
    run_step "[2.5/8] Gunicorn 只监听 127.0.0.1（安全收口）" ./deploy/harden-gunicorn-bind.sh
}

# ---------------------------------------------------------------------------
# 3. 可选能力：OnlyOffice 协同编辑
# ---------------------------------------------------------------------------
skip "onlyoffice" || {
    run_step "[3/8] OnlyOffice 文档服务" ./deploy/deploy-onlyoffice.sh
    run_step "[3.5/8] OnlyOffice 回调修复" ./deploy/apply-onlyoffice-fix.sh
}

# ---------------------------------------------------------------------------
# 4. 可选能力：语音
# ---------------------------------------------------------------------------
skip "voice" || {
    run_step "[4/8] 语音模块" ./deploy/deploy-voice.sh
}

# ---------------------------------------------------------------------------
# 5. 数据保障：MySQL 定时备份（含 crontab 校验）
# ---------------------------------------------------------------------------
skip "mysql-backup" || {
    run_step "[5/8] MySQL 自动备份与恢复脚本" ./deploy/pm-mysql-backup.sh
}

# ---------------------------------------------------------------------------
# 6. 可观测性：健康检查与告警
# ---------------------------------------------------------------------------
skip "monitor" || {
    run_step "[6/8] 服务健康监控" ./deploy/pm-monitor.sh
}

# ---------------------------------------------------------------------------
# 7. 清理历史遗留（占用 8000 端口的旧进程）
# ---------------------------------------------------------------------------
skip "legacy-port" || {
    run_step "[7/8] 关闭占用 8000 的遗留进程" ./deploy/close-legacy-8000.sh
}

# ---------------------------------------------------------------------------
# 8. 总装收尾：重载配置并做一次整体健康检查
# ---------------------------------------------------------------------------
step "[8/8] 重载服务并健康检查"
systemctl daemon-reload
for svc in pm-django pm-celery pm-celery-beat; do
    systemctl reset-failed "${svc}" >/dev/null 2>&1 || true
    systemctl restart "${svc}" >/dev/null 2>&1 \
        && log "  ${svc} 已重启" \
        || warn "  ${svc} 重启失败，请 systemctl status ${svc} 排查"
done

sleep 3
FAILED=0
for svc in pm-django pm-celery pm-celery-beat; do
    if systemctl is-active --quiet "${svc}"; then
        echo "  [OK]    ${svc}"
    else
        echo "  [DOWN]  ${svc}"
        FAILED=1
    fi
done

if command -v curl >/dev/null 2>&1; then
    if curl -fsS -o /dev/null --max-time 5 http://127.0.0.1:8000/ >/dev/null 2>&1; then
        echo "  [OK]    HTTP 127.0.0.1:8000/"
    else
        echo "  [DOWN]  HTTP 127.0.0.1:8000/"
        FAILED=1
    fi
fi

echo
if [ "${FAILED}" -eq 0 ]; then
    log "========== 总装部署完成，全部服务正常 =========="
else
    warn "========== 总装部署完成，但有服务未就绪（见上方 [DOWN]）=========="
fi
echo
echo "  常用命令："
echo "      systemctl status pm-django pm-celery pm-celery-beat"
echo "      journalctl -u pm-django -f"
echo "      ${PM_DIR}/deploy/pm-mysql-backup.sh status   # 备份状态"
echo
warn "提示：若服务器对外只走 HTTPS，请另行执行 ./deploy/setup-nginx-https.sh"
warn "提示：离线环境请先执行 ./deploy/prepare-offline-assets.sh"
