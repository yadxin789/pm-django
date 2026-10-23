#!/usr/bin/env bash
# ============================================================================
#  方案 A 第 2 步：收紧 gunicorn 监听地址 0.0.0.0:8000 → 127.0.0.1:8000
#
#  用法： bash /root/pm-stage/harden-gunicorn-bind.sh
#  原理： drop-in 覆盖 ExecStart（不修改 deploy.sh 生成的原始 unit 文件）
#  效果： 8000 不再对局域网暴露，所有外部流量统一走 Nginx 的 80
#
#  回滚（任一条即可）：
#     rm -rf /etc/systemd/system/pm-django.service.d/bind.conf
#     systemctl daemon-reload && systemctl restart pm-django
# ============================================================================
set -u

DROPIN_DIR=/etc/systemd/system/pm-django.service.d
CONF=$DROPIN_DIR/bind.conf
GUNICORN=/var/www/pm-django/venv/bin/gunicorn
LOG_DIR=/var/www/pm-django/logs

FAIL=0
say() { printf '\n%s\n' "$*"; }
ok()  { printf '  [OK]   %s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; FAIL=$((FAIL+1)); }

say "== 0) 前置检查"
if [ -x "$GUNICORN" ]; then ok "gunicorn 可执行：$GUNICORN"; else bad "$GUNICORN 不存在或不可执行"; exit 1; fi
[ -d "$LOG_DIR" ] && ok "日志目录存在：$LOG_DIR" || bad "$LOG_DIR 缺失"
if ss -lnt "sport = :80" 2>/dev/null | grep -q LISTEN; then ok "Nginx 已在监听 80（转发目标就绪）"; else bad "80 上没有监听者，请先跑 setup-nginx-80.sh"; fi

say "== 1) 写入 drop-in $CONF"
mkdir -p "$DROPIN_DIR"
cat > "$CONF" <<'EOF'
[Service]
# 仅监听本机回环，外部流量统一经 Nginx 反代进来
# 注意：systemd 的 ExecStart 不能用 drop-in 追加，只有先清空再重设；
#       因此这里必须完整复制原始 unit 的整行参数
ExecStart=
ExecStart=/var/www/pm-django/venv/bin/gunicorn pm.wsgi:application --bind 127.0.0.1:8000 --workers 3 --threads 2 --timeout 120 --access-logfile /var/www/pm-django/logs/gunicorn-access.log --error-logfile /var/www/pm-django/logs/gunicorn-error.log
EOF
[ -s "$CONF" ] && ok "$CONF 已写入（$(stat -c%s "$CONF") bytes）" || { bad "$CONF 写入失败"; exit 1; }

say "== 2) 重载并重启"
systemctl daemon-reload && ok "daemon-reload" || bad "daemon-reload 失败"
if systemctl restart pm-django; then ok "pm-django restarted"; else bad "pm-django 重启失败"; fi
sleep 3
printf '  pm-django 状态：%s\n' "$(systemctl is-active pm-django)"

say "== 3) 生效后的实际启动命令"
systemctl show pm-django -p ExecStart --value | tr ';' '\n' | sed 's/^/    /' | head -6

say "== 4) 监听地址确认（应为 127.0.0.1:8000）"
if ss -lnt "sport = :8000" 2>/dev/null | grep -q LISTEN; then
    ss -lntp "sport = :8000" 2>/dev/null | sed 's/^/    /'
    if ss -lnt "sport = :8000" 2>/dev/null | grep -q '127.0.0.1:8000'; then
        ok "gunicorn 只监听回环地址"
    else
        bad "gunicorn 仍监听 0.0.0.0:8000（drop-in 未生效）"
    fi
else
    bad "8000 上没有监听者，Web 可能已挂"
fi

IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
IP=${IP:-127.0.0.1}

say "== 5) 业务连通性"
printf '  本机直连 gunicorn  http://127.0.0.1:8000/  -> HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/)"
printf '  经 Nginx         http://127.0.0.1/        -> HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/)"
printf '  经 Nginx 走网卡   http://%s/        -> HTTP %s\n' "$IP" "$(curl -s -o /dev/null -w '%{http_code}' "http://$IP/")"
printf '  经 Nginx 静态     /static/...             -> HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' "http://$IP/static/font-awesome/css/fontawesome-all.css")"

CODE_EXTERNAL=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://$IP:8000/" || true)
printf '  直连外部 8000     http://%s:8000/   -> HTTP %s（期望 000/失败，已收口）\n' "$IP" "${CODE_EXTERNAL:-000}"

say "== 6) 汇总"
printf '  nginx     : %s (enabled)\n' "$(systemctl is-active nginx)"
printf '  pm-django : %s\n' "$(systemctl is-active pm-django)"
printf '  pm-celery : %s\n' "$(systemctl is-active pm-celery)"
printf '  pm-celery-beat : %s\n' "$(systemctl is-active pm-celery-beat)"

printf '\n'
if [ "$FAIL" -eq 0 ]; then
    printf '==== 第 2 步完成（FAIL=0）：外部统一走 http://%s/ ，8000 已对局域网关闭 ====\n' "$IP"
    printf '     回滚：rm -rf %s && systemctl daemon-reload && systemctl restart pm-django\n' "$CONF"
    exit 0
else
    printf '==== 有 %d 项 FAIL，请按上面 [FAIL] 行处理 ====\n' "$FAIL"
    exit 1
fi
