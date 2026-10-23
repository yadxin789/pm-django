#!/usr/bin/env bash
# ============================================================================
#  PM 运维组件安装器 —— 在【服务器】上执行
#
#  用法： bash /root/pm-stage/apply-on-server.sh
#
#  幂等：可反复执行。只做 4 件事：
#    1. 把同目录下的 4 个文件 install 到系统路径（覆盖旧版）
#    2. 去掉可能的 CRLF、校验 bash 语法与文件类型
#    3. daemon-reload + 重启 pm-monitor.timer
#    4. 真跑一次「备份」与「巡检」，把结果打出来
# ============================================================================
set -u

STAGE="$(cd "$(dirname "$0")" && pwd)"

FAIL=0
say() { printf '\n%s\n' "$*"; }
ok()  { printf '  [OK]   %s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; FAIL=$((FAIL+1)); }

SRC_FILES="pm-mysql-backup.sh pm-monitor.sh pm-monitor.service pm-monitor.timer"

say "== 0) 源文件检查（$STAGE）"
MISSING=0
for f in $SRC_FILES; do
    if [ -f "$STAGE/$f" ]; then
        printf '  [OK]   %-24s %s bytes\n' "$f" "$(stat -c%s "$STAGE/$f")"
    else
        bad "缺少 $STAGE/$f"
        MISSING=1
    fi
done
if [ "$MISSING" -ne 0 ]; then
    say "源文件不齐，中止（系统未被改动）"
    exit 1
fi

say "== 1) 去除 CRLF（Windows 上传保险）"
for f in $SRC_FILES; do
    sed -i 's/\r$//' "$STAGE/$f"
done
ok "已统一为 LF"

say "== 2) 安装到系统路径"
install -m 0755 -o root -g root "$STAGE/pm-mysql-backup.sh"  /etc/cron.daily/pm-mysql-backup && ok "/etc/cron.daily/pm-mysql-backup   0755"
install -m 0755 -o root -g root "$STAGE/pm-monitor.sh"      /usr/local/bin/pm-monitor.sh      && ok "/usr/local/bin/pm-monitor.sh       0755"
install -m 0644 -o root -g root "$STAGE/pm-monitor.service" /etc/systemd/system/pm-monitor.service && ok "/etc/systemd/system/pm-monitor.service 0644"
install -m 0644 -o root -g root "$STAGE/pm-monitor.timer"   /etc/systemd/system/pm-monitor.timer   && ok "/etc/systemd/system/pm-monitor.timer   0644"

say "== 3) 落地校验（换行 / 语法 / 类型）"
for f in /etc/cron.daily/pm-mysql-backup /usr/local/bin/pm-monitor.sh; do
    if file "$f" | grep -q CRLF; then
        bad "$f 仍含 CRLF"; sed -i 's/\r$//' "$f"
    else
        ok "$f 换行为 LF"
    fi
    if bash -n "$f" 2>/dev/null; then ok "bash -n $(basename "$f") 语法通过"; else bad "bash -n $f 语法错误"; fi
done

say "== 4) systemd 重载并启用/重启巡检定时器"
if systemctl daemon-reload; then ok "daemon-reload"; else bad "daemon-reload"; fi
# 必须先 enable：timer 默认未启用，仅 restart 不会开机自启，is-enabled 检查也会失败
if systemctl enable pm-monitor.timer >/dev/null 2>&1; then ok "pm-monitor.timer 已设置开机自启"; else bad "pm-monitor.timer enable 失败"; fi
if systemctl restart pm-monitor.timer; then ok "pm-monitor.timer 已重启"; else bad "pm-monitor.timer 重启失败"; fi
systemctl is-enabled pm-monitor.timer >/dev/null 2>&1 && ok "pm-monitor.timer enabled" || bad "pm-monitor.timer 未 enabled"

say "== 5) 备份前置条件"
[ -r /root/.my.cnf ] && ok "/root/.my.cnf 可读（凭据来源）" || bad "/root/.my.cnf 不可读 → 备份必失败"
[ -r /var/www/pm-django/.env ] && ok "/var/www/pm-django/.env 可读" || bad "/var/www/pm-django/.env 不可读"
if grep -q '^DB_NAME=' /var/www/pm-django/.env 2>/dev/null; then
    ok ".env 含 DB_NAME=$(grep '^DB_NAME=' /var/www/pm-django/.env | head -1 | cut -d= -f2)"
else
    printf '  [WARN] .env 无 DB_NAME，脚本将回退默认 pm_db_django\n'
fi

say "== 6) 真跑一次备份"
if /etc/cron.daily/pm-mysql-backup; then ok "备份脚本退出码 0"; else bad "备份脚本退出码 $?"; fi
printf '  --- /var/log/pm-mysql-backup.log 末尾 ---\n'
tail -6 /var/log/pm-mysql-backup.log 2>/dev/null | sed 's/^/    /' || printf '    (无日志)\n'
printf '  --- /var/backups/pm-mysql/ ---\n'
ls -lh /var/backups/pm-mysql/ 2>/dev/null | tail -5 | sed 's/^/    /' || printf '    (无备份文件)\n'

say "== 7) 巡检脚本自检"
if /usr/local/bin/pm-monitor.sh; then ok "巡检器退出码 0（设计为永不 failed）"; else bad "巡检器退出码 $?"; fi
systemctl start pm-monitor.service 2>/dev/null
sleep 1
printf '  --- pm-monitor.service 执行结果 ---\n'
systemctl show -p Result -p ExecMainStatus -p ActiveState pm-monitor.service 2>/dev/null | sed 's/^/    /'
if systemctl show -p Result --value pm-monitor.service 2>/dev/null | grep -q '^success$'; then
    ok "systemd 判定巡检单元 success"
else
    bad "systemd 判定巡检单元非 success（见上面 Result=）"
fi

say "== 8) 汇总"
printf '  pm-monitor.timer : %s\n' "$(systemctl is-active pm-monitor.timer 2>/dev/null)"
printf '  pm-django        : %s\n' "$(systemctl is-active pm-django 2>/dev/null)"
printf '  pm-celery        : %s\n' "$(systemctl is-active pm-celery 2>/dev/null)"
printf '  pm-celery-beat   : %s\n' "$(systemctl is-active pm-celery-beat 2>/dev/null)"
systemctl list-timers pm-monitor.timer --no-pager 2>/dev/null | head -3 | sed 's/^/    /'

printf '\n'
if [ "$FAIL" -eq 0 ]; then
    printf '==== 全部检查通过（FAIL=0）====\n'
    exit 0
else
    printf '==== 有 %d 项 FAIL，请按上面 [FAIL] 行处理 ====\n' "$FAIL"
    exit 1
fi
