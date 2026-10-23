#!/usr/bin/env bash
# ============================================================================
#  PM 服务健康巡检脚本
#  部署位置：/usr/local/bin/pm-monitor.sh
#  由 pm-monitor.timer 每 5 分钟触发一次 pm-monitor.service 调用
#
#  设计要点：
#   - 巡检器自身永远 exit 0。被巡检服务异常时只写日志/发信，
#     不能让 systemd 把「巡检器」标记成 failed（否则告警链会自相矛盾）。
#   - mail 未安装时自动降级写入本地日志。
# ============================================================================

set -u

LOG_TAG="pm-monitor"
LOG_FILE="/var/log/pm-monitor.log"
ALERT_TO="${ALERT_TO:-root@localhost}"

alert() {
    local msg="$1"
    logger -t "$LOG_TAG" "$msg" 2>/dev/null || true
    if command -v mail >/dev/null 2>&1; then
        printf '%s\n' "$msg" | mail -s "[PM] service down" "$ALERT_TO" 2>/dev/null || true
    else
        printf '%s\n' "$msg" >>"$LOG_FILE" 2>/dev/null || true
    fi
}

for s in pm-django pm-celery pm-celery-beat; do
    state="$(systemctl is-active "$s" 2>/dev/null || true)"
    if [ "$state" != "active" ]; then
        alert "[$(date '+%F %T')] PM 告警: $s 状态异常 (${state:-unknown}) on $(hostname)"
    fi
done

exit 0
