#!/usr/bin/env bash
# ============================================================================
#  清理：撤掉 8000 端口的历史防火墙放行
#
#  背景：/etc/systemd/system/pm-django.service.d/bind.conf 已把 gunicorn 收紧到
#        127.0.0.1:8000，外部流量统一走 Nginx 的 80。原先给局域网放行的
#        8000 rich rule 已无意义，留着会让防火墙规则集与事实不符。
#
#  用法： bash /root/pm-stage/close-legacy-8000.sh
#  幂等：规则不存在时只报告，不报错
#
#  回滚（重新放行 8000）：
#    firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="192.168.2.0/24" port port="8000" protocol="tcp" accept'
#    firewall-cmd --reload
# ============================================================================
set -u

RULE_QUOTED='rule family="ipv4" source address="192.168.2.0/24" port port="8000" protocol="tcp" accept'
RULE_PLAIN='rule family=ipv4 source address=192.168.2.0/24 port port=8000 protocol=tcp accept'

FAIL=0
say() { printf '\n%s\n' "$*"; }
ok()  { printf '  [OK]   %s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; FAIL=$((FAIL+1)); }

say "== 0) 前置检查：gunicorn 是否已收紧到回环"
if ss -lnt "sport = :8000" 2>/dev/null | grep -q '127.0.0.1:8000'; then
    ok "gunicorn 只监听 127.0.0.1:8000"
else
    bad "gunicorn 未收紧（仍为 0.0.0.0:8000）"
    printf '  此时删除放行会导致 8000 彻底不可用，已中止。\n'
    printf '  请先执行：bash /root/pm-stage/harden-gunicorn-bind.sh\n'
    exit 1
fi

say "== 1) 当前 rich rules（删除前）"
firewall-cmd --list-rich-rules 2>/dev/null | sed 's/^/    /'
printf '    —— 以上为全部 rich rule ——\n'

say "== 2) 删除含 8000 的 rich rule"
if firewall-cmd --list-rich-rules 2>/dev/null | grep -q '8000'; then
    if firewall-cmd --permanent --remove-rich-rule="$RULE_QUOTED" >/dev/null 2>&1; then
        ok "已从 permanent 配置中删除（带引号形式）"
    elif firewall-cmd --permanent --remove-rich-rule="$RULE_PLAIN" >/dev/null 2>&1; then
        ok "已从 permanent 配置中删除（无引号形式）"
    else
        bad "删除失败，请手动核对 --list-rich-rules 的规则文本"
    fi
    if firewall-cmd --reload >/dev/null 2>&1; then ok "firewall-cmd --reload 完成"; else bad "reload 失败"; fi
else
    ok "不存在含 8000 的 rich rule，无需处理"
fi

say "== 3) 删除后状态"
printf '  rich rules : '; firewall-cmd --list-rich-rules 2>/dev/null | tr '\n' ' '; printf '(显示为空即干净)\n'
printf '  ports      : '; firewall-cmd --list-ports
printf '  80 监听数  : %s\n' "$(ss -lnt "sport = :80" 2>/dev/null | grep -c LISTEN)"
printf '  8000 监听  : '; ss -lnt "sport = :8000" 2>/dev/null | grep LISTEN | sed 's/^/               /'

IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
IP=${IP:-127.0.0.1}

say "== 4) 业务连通性（本机视角）"
printf '  http://%s/       -> HTTP %s\n' "$IP" "$(curl -s -o /dev/null -w '%{http_code}' "http://$IP/")"
printf '  http://%s/admin/ -> HTTP %s\n' "$IP" "$(curl -s -o /dev/null -w '%{http_code}' "http://$IP/admin/")"
printf '  http://%s:8000/  -> HTTP %s（期望 000，已收口）\n' "$IP" "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://$IP:8000/" || true)"

say "== 5) 服务状态"
printf '  nginx / pm-django / pm-celery / pm-celery-beat : %s\n' "$(systemctl is-active nginx pm-django pm-celery pm-celery-beat | tr '\n' ' ')"

printf '\n'
if [ "$FAIL" -eq 0 ]; then
    printf '==== 清理完成（FAIL=0）：防火墙只留 80/8080/9090，8000 已彻底无对外入口 ====\n'
    exit 0
else
    printf '==== 有 %d 项 FAIL，请按上面 [FAIL] 行处理 ====\n' "$FAIL"
    exit 1
fi
