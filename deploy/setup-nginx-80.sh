#!/usr/bin/env bash
# ============================================================================
#  方案 A：Nginx 反向代理 80 → 127.0.0.1:8000
#
#  用法： bash /root/pm-stage/setup-nginx-80.sh
#  幂等：可反复执行（重复执行 = 重写配置 + 重载）
#
#  本步只做「让 80 端口可用」，不动 gunicorn 的监听地址。
#  gunicorn 收紧到 127.0.0.1 由第二步脚本负责。
# ============================================================================
set -u

WEB_ROOT=/var/www/pm-django
CONF=/etc/nginx/conf.d/pm.conf
TMPLOG=/tmp/nginx-t.out

FAIL=0
say() { printf '\n%s\n' "$*"; }
ok()  { printf '  [OK]   %s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; FAIL=$((FAIL+1)); }

say "== 0) 前置检查"
if command -v nginx >/dev/null 2>&1; then ok "nginx 已安装：$(nginx -v 2>&1)"; else bad "nginx 未安装"; exit 1; fi
[ -d "$WEB_ROOT/static" ] && ok "$WEB_ROOT/static 存在" || bad "$WEB_ROOT/static 缺失"
[ -d "$WEB_ROOT/media" ]  && ok "$WEB_ROOT/media 存在"  || bad "$WEB_ROOT/media 缺失"

say "== 1) 写入 /etc/nginx/conf.d/pm.conf"
cat > "$CONF" <<'NGINX'
# PM 项目反向代理：80 → 127.0.0.1:8000
# 静态/上传文件由 Nginx 直出，动态请求转发给 gunicorn
# 注意：本文件声明 default_server，会覆盖发行版自带的 /usr/share/nginx/html 欢迎页
server {
    listen       80 default_server;
    listen       [::]:80 default_server;
    # 不用 "_"：发行版自带的欢迎页 server 块已占用 "_"，同名会触发
    # nginx: [warn] conflicting server name "_" on 0.0.0.0:80, ignored
    # 未匹配任何 server_name 的请求由 default_server（本块）兜底
    server_name  pm-django;

    charset utf-8;
    client_max_body_size 200m;

    access_log /var/log/nginx/pm-access.log main;
    error_log  /var/log/nginx/pm-error.log  warn;

    gzip on;
    gzip_min_length 1k;
    gzip_comp_level 5;
    gzip_types text/plain text/css text/xml application/json application/javascript application/xml+rss image/svg+xml;

    # Django 静态资源（settings.py: STATICFILES_DIRS = BASE_DIR / 'static'）
    location /static/ {
        alias /var/www/pm-django/static/;
        expires 7d;
        access_log off;
    }

    # 用户上传（settings.py: MEDIA_ROOT = BASE_DIR / 'media'）
    location /media/ {
        alias /var/www/pm-django/media/;
        expires 7d;
        access_log off;
    }

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        "";
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
NGINX

if [ -s "$CONF" ]; then ok "$CONF 已写入（$(stat -c%s "$CONF") bytes）"; else bad "$CONF 写入失败"; exit 1; fi

say "== 2) nginx -t 语法检查"
if nginx -t >"$TMPLOG" 2>&1; then
    sed 's/^/    /' "$TMPLOG"
    ok "配置语法通过"
else
    sed 's/^/    /' "$TMPLOG"
    bad "配置语法不通过 —— 已中止，未重启 nginx（旧配置仍在生效）"
    exit 1
fi

say "== 3) 防火墙放行 80/tcp"
if firewall-cmd --permanent --add-port=80/tcp >/dev/null 2>&1; then ok "已写入 permanent 规则"; else bad "firewall-cmd --permanent 失败"; fi
if firewall-cmd --reload >/dev/null 2>&1; then ok "firewall-cmd --reload 完成"; else bad "firewall-cmd --reload 失败"; fi
printf '  当前放行端口：'; firewall-cmd --list-ports

say "== 4) 启动并设为开机自启"
systemctl enable nginx >/dev/null 2>&1 && ok "nginx enabled（开机自启）" || bad "enable 失败"
if systemctl restart nginx; then ok "nginx restarted"; else bad "nginx 启动失败"; fi
printf '  nginx 状态：%s\n' "$(systemctl is-active nginx)"

say "== 5) 连通性验证"
printf '  --- curl -I http://127.0.0.1/ ---\n'
curl -sI http://127.0.0.1/ 2>&1 | head -1 | sed 's/^/    /'
printf '  --- /admin/ ---\n'
printf '    HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/admin/)"

SF=$(find "$WEB_ROOT/static" -type f \( -name '*.css' -o -name '*.js' \) 2>/dev/null | head -1)
if [ -n "$SF" ]; then
    REL=${SF#$WEB_ROOT}
    printf '  --- 静态直出 %s ---\n' "$REL"
    printf '    HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1$REL")"
else
    printf '  --- 未找到 css/js，跳过静态直出验证 ---\n'
fi

IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
IP=${IP:-127.0.0.1}
printf '  --- 走真实网卡 IP %s ---\n' "$IP"
printf '    HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' "http://$IP/")"

say "== 6) 80 端口监听确认"
if ss -lnt "sport = :80" 2>/dev/null | grep -q LISTEN; then
    ss -lntp "sport = :80" 2>/dev/null | sed 's/^/    /'
    ok "80 已在监听"
else
    bad "80 未在监听"
fi

say "== 7) 汇总"
printf '  nginx     : %s (enabled)\n' "$(systemctl is-active nginx)"
printf '  pm-django : %s\n' "$(systemctl is-active pm-django)"
printf '  gunicorn  : %s\n' "$(ss -lnt "sport = :8000" 2>/dev/null | grep -c LISTEN)"

printf '\n'
if [ "$FAIL" -eq 0 ]; then
    printf '==== 第 1 步完成（FAIL=0）： http://%s/ 已可直接访问，无需带 :8000 ====\n' "$IP"
    exit 0
else
    printf '==== 有 %d 项 FAIL，请按上面 [FAIL] 行处理 ====\n' "$FAIL"
    exit 1
fi
