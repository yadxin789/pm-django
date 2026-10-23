# -*- coding: utf-8 -*-
"""HTTPS / 麦克风（getUserMedia）安全上下文验证。

要证明的因果链：
  浏览器只在「安全上下文」（HTTPS 或 localhost）才提供 getUserMedia；
  纯 HTTP 时 navigator.mediaDevices 就是 undefined，
  电话会议点「加入会议」必然报：无法访问麦克风：Cannot read properties of
  undefined (reading 'getUserMedia')。
  → 参考实现（Laravel deploy-voice.sh）第一步就是签发自签证书并把站点跑在 https。

同时验证「切到 HTTPS 不能把别的东西弄坏」：
  1) 80 端口仍然可用（OnlyOffice 容器是服务器对服务器回访 http://<IP>/collab/callback/…，
     一旦 301 到 443，文档保存会失效）；
  2) HTTPS 页面加载的 api.js 也必须是 https（http://<IP>:8080 会被当混合内容拦掉 → 编辑器白板）；
  3) Django 必须认得 nginx 的 TLS（SECURE_PROXY_SSL_HEADER），否则 request.is_secure() 恒为 False，
     CSRF 的 Origin 校验会拿 https://IP 去比 http://IP → 所有表单/登录 403。

服务器上执行（任意目录均可）：
    /var/www/pm-django/venv/bin/python /root/pm-stage/verify-https-voice.py [服务器IP]
"""
import http.cookiejar
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import django

PROJECT_DIR = os.environ.get('PM_PROJECT_DIR', '/var/www/pm-django')
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


def load_env_file(path='/var/www/pm-django/.env'):
    """模拟 systemd 的 EnvironmentFile：把 .env 注入 os.environ（否则 settings 取到代码默认值）。"""
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(os.path.join(PROJECT_DIR, '.env'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pm.settings')
django.setup()

from django.conf import settings            # noqa: E402
from django.test import RequestFactory      # noqa: E402

from core.models import OperationLog, User  # noqa: E402


def detect_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('8.8.8.8', 80))       # 不会真的发包，只为拿到出口网卡地址
        return sock.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        sock.close()


HOST = sys.argv[1] if len(sys.argv) > 1 else detect_ip()
PM_HTTPS = f'https://{HOST}/'
PM_HTTP = f'http://{HOST}/'
DS_HTTPS = f'https://{HOST}:8443/'
CRT = '/etc/pm/voice/ssl/server.crt'
PM_CONF = '/etc/nginx/conf.d/pm.conf'
DS_CONF = '/etc/nginx/conf.d/pm-ds-https.conf'
PROBE_USER = 'https-probe-仅用于验证的账号'
FAILED = 0


def check(cond, msg):
    global FAILED
    if cond:
        print(f'  [OK]   {msg}')
    else:
        print(f'  [FAIL] {msg}')
        FAILED += 1
    return bool(cond)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随跳转：要看清 302 还是 403。"""

    def redirect_request(self, *args, **kwargs):
        return None


def build_opener():
    """证书不校验（自签）：端口/SAN 的正确性由第 1、2 节单独验证。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                         urllib.request.HTTPSHandler(context=ctx),
                                         _NoRedirect())
    return opener


def fetch(opener, url, method='GET', data=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        resp = opener.open(req, timeout=timeout)
        return resp.status, resp.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'ignore')
    except Exception as exc:                      # 连接被拒 / TLS 失败 / 超时
        return 0, f'{type(exc).__name__}: {exc}'


def mw_origin_verified(request):
    """调用 CSRF 中间件真实的 Origin 校验逻辑（生产用的就是这一份判定）。"""
    from django.http import HttpResponse
    from django.middleware.csrf import CsrfViewMiddleware

    middleware = CsrfViewMiddleware(lambda req: HttpResponse('ok'))
    return middleware._origin_verified(request)


def read(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return fh.read()
    except OSError:
        return ''


def verify_cert():
    print('== 1) 自签证书（浏览器只认 SAN，不认 CN）')
    if not check(os.path.exists(CRT), f'证书存在：{CRT}'):
        return
    out = subprocess.run(['openssl', 'x509', '-in', CRT, '-noout', '-checkip', HOST],
                         capture_output=True, text=True)
    check(out.returncode == 0, f'证书 SAN 覆盖本机 IP（{HOST}）—— 缺 SAN 会被浏览器判为无效证书')
    info = subprocess.run(['openssl', 'x509', '-in', CRT, '-noout', '-subject', '-dates'],
                          capture_output=True, text=True).stdout.strip().replace('\n', '；')
    print(f'  {info}')


def verify_nginx():
    print()
    print('== 2) nginx 配置')
    pm = read(PM_CONF)
    check('listen       443 ssl' in pm or 'listen 443 ssl' in pm, 'pm.conf 有 443 ssl 监听')
    check(CRT in pm, 'pm.conf 指向同一张证书')
    ds = read(DS_CONF)
    if check(bool(ds), f'存在 DS 的 HTTPS 反代配置：{DS_CONF}'):
        check('8443 ssl' in ds, 'DS 反代监听 8443 ssl')
        check('$http_host' in ds,
              'DS 反代用 $http_host 传 Host（保住 :8443；只传 $host 会让浏览器去连 http://<IP>:8080 被拦）')
        check('$http_upgrade' in ds and '$http_connection' in ds, 'DS 反代透传 websocket（协同编辑实时同步）')
        check('proxy_pass         http://127.0.0.1:8080' in ds, 'DS 反代回源 127.0.0.1:8080')


def verify_transport():
    print()
    print('== 3) 传输层实测（自签证书不校验）')
    opener = build_opener()

    code, _ = fetch(opener, PM_HTTPS)
    check(code in (200, 302), f'PM HTTPS 可访问：{PM_HTTPS} → HTTP {code}')

    code, _ = fetch(opener, PM_HTTP)
    check(code in (200, 302),
          f'80 端口仍可用：{PM_HTTP} → HTTP {code}（DS 容器回访 PM 走这里，跳到 443 会让保存失效）')

    code, body = fetch(opener, DS_HTTPS + 'healthcheck')
    check(code == 200 and 'true' in body.lower(), f'DS HTTPS healthcheck → HTTP {code} {body[:30]}')

    code, body = fetch(opener, DS_HTTPS + 'web-apps/apps/api/documents/api.js')
    check(code == 200 and 'DocsAPI' in body, f'DS api.js（HTTPS）→ HTTP {code}，含 DocsAPI')

    server_url = str(settings.ONLYOFFICE_SERVER_URL)
    check(server_url.startswith('https://'),
          f'ONLYOFFICE_SERVER_URL 已切 https（{server_url}）—— http 的 api.js 在 https 页面会被拦成混合内容')
    check(str(settings.ONLYOFFICE_PM_INTERNAL_URL).startswith('http://'),
          f'PM_INTERNAL_URL 保持 http（{settings.ONLYOFFICE_PM_INTERNAL_URL}）—— DS 容器服务端回访，不经浏览器')
    return opener


def verify_https_form(opener):
    print()
    print('== 4) HTTPS 表单 POST 真跑一遍（CSRF Origin 校验是 HTTPS 下最容易踩的坑）')
    check(not User.objects.filter(username=PROBE_USER).exists(), '探针用户名不与任何真实账号冲突')

    code, body = fetch(opener, PM_HTTPS + 'login')
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
    check(code == 200 and match, f'GET {PM_HTTPS}login → HTTP {code}，页面带 csrfmiddlewaretoken')

    if match:
        data = urllib.parse.urlencode({'username': PROBE_USER, 'password': 'probe-wrong-password',
                                       'csrfmiddlewaretoken': match.group(1)}).encode()
        code, _ = fetch(opener, PM_HTTPS + 'login', method='POST', data=data, headers={
            'Origin': PM_HTTPS.rstrip('/'), 'Referer': PM_HTTPS + 'login',
            'Content-Type': 'application/x-www-form-urlencoded'})
        # 期望「到达视图」（302 重定向回登录页 / 200 渲染错误提示）；
        # 403 表示被 CSRF 拦下 —— 那 HTTPS 下所有表单都提交不了
        check(code in (200, 302), f'POST {PM_HTTPS}login → HTTP {code}（403 即 CSRF 被拦）')


def verify_django_sees_tls():
    print()
    print('== 5) Django 是否认得 nginx 的 TLS（机制对照）')
    check(settings.SECURE_PROXY_SSL_HEADER == ('HTTP_X_FORWARDED_PROTO', 'https'),
          f'SECURE_PROXY_SSL_HEADER = {settings.SECURE_PROXY_SSL_HEADER}')

    # 用 RequestFactory + CSRF 中间件本身来做对照：比看状态码更精确（状态码会被
    # 「有没有带 token」干扰）。HTTP_HOST 要显式给，才是浏览器真实请求的样子 ——
    # Django 5 的 _get_raw_host() 在没有 HTTP_HOST 时会拿 SERVER_NAME + SERVER_PORT 拼，
    # 而 is_secure() 为真时它认为默认端口是 443，SERVER_PORT=80 会被拼成
    # 「192.168.2.102:80」，与 Origin 里的「192.168.2.102」不等，凭空造出一个假 403。
    # 真实浏览器访问 https://<IP>/ 时 Host 头不带端口（443 是默认端口）。
    origin = PM_HTTPS.rstrip('/')
    factory = RequestFactory(SERVER_NAME=HOST, HTTP_HOST=HOST)
    with_header = factory.post('/login', {}, HTTP_X_FORWARDED_PROTO='https', HTTP_ORIGIN=origin)
    without_header = factory.post('/login', {}, HTTP_ORIGIN=origin)

    check(with_header.is_secure() and with_header.scheme == 'https',
          '带 X-Forwarded-Proto: https → request.is_secure() 为真、scheme=https')
    check(mw_origin_verified(with_header),
          f'→ CSRF 的 Origin 校验通过（{origin} 与 get_host()={with_header.get_host()} 一致）')
    check(not without_header.is_secure(), '漏配该头（模拟故障）→ is_secure() 为假')
    check(not mw_origin_verified(without_header),
          '→ Origin 校验失败：HTTPS 下所有表单/登录都会 403（这就是必须配它的原因）')


def cleanup():
    print()
    print('== 6) 清理探针痕迹')
    deleted, _ = OperationLog.objects.filter(module='认证', action='登录失败',
                                             content__contains=PROBE_USER).delete()
    print(f'  [OK]   已清理探针操作日志 {deleted} 条；未创建任何用户或文档')


def main():
    print(f'目标：PM={PM_HTTPS}  DS={DS_HTTPS}')
    verify_cert()
    verify_nginx()
    opener = verify_transport()
    verify_https_form(opener)
    verify_django_sees_tls()
    cleanup()

    print()
    print('== 7) 结论 ==')
    print(f'  浏览器请改用 HTTPS：{PM_HTTPS}project/<项目ID>/voice')
    print('  getUserMedia 只在安全上下文（HTTPS 或 localhost）可用；')
    print('  纯 HTTP 时 navigator.mediaDevices 是 undefined，报错即为')
    print("  「无法访问麦克风：Cannot read properties of undefined (reading 'getUserMedia')」。")
    print('  首次访问需在证书告警页点「高级 → 继续前往」（443 与 8443 各一次）；')
    print(f'  或把 {CRT} 装成「受信任的根证书颁发机构」，永久免告警。')
    print()
    if FAILED == 0:
        print('==== HTTPS / 安全上下文验证通过（FAIL=0）====')
        return 0
    print(f'==== 存在 {FAILED} 项 FAIL ====')
    return 1


if __name__ == '__main__':
    sys.exit(main())
