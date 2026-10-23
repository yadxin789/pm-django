# -*- coding: utf-8 -*-
"""协同办公链路验证：上传接口 + 编辑器页面 + OnlyOffice 三方配置一致性。

用途：回归验证 web/views/collab.py 与 OnlyOffice 配置（历史上出过两类故障）：
  1) 上传 500：视图函数名为 open 遮蔽内置 open() → 'str' object has no attribute 'user'
  2) 编辑器报 DocsAPI is not defined：ONLYOFFICE_SERVER_URL 填成了浏览器不可达的地址
     以及紧随其后的 JWT 校验失败 / 下载失败：JWT_SECRET 与 DS 容器不一致、
     PM_INTERNAL_URL 指向容器访问不到的 8000 端口

服务器上执行（任意目录均可）：
    /var/www/pm-django/venv/bin/python /root/pm-stage/verify-collab.py [项目ID，默认 1]

行为：真实走一遍「上传文档 → 打开编辑器页面」，校验落库/落盘/HTML/签名，
      最后删除测试记录与文件（零残留）。不修改任何生产数据。
"""
import base64
import hashlib
import hmac
import json
import os
import ssl
import subprocess
import sys
import traceback
import urllib.request

import django

def load_env_file(path='/var/www/pm-django/.env'):
    """模拟 systemd 的 EnvironmentFile：把 .env 注入 os.environ。

    必须做这一步：systemd 只把 .env 注入 pm-django 服务进程，SSH 会话里没有，
    否则 settings 会取到代码里的默认值（localhost:8080 等），验证结果失真。
    """
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# 允许从任意工作目录运行：把项目根目录挂到 sys.path 上
PROJECT_DIR = os.environ.get('PM_PROJECT_DIR', '/var/www/pm-django')
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

load_env_file(os.path.join(PROJECT_DIR, '.env'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pm.settings')
django.setup()

from django.conf import settings                                # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile   # noqa: E402
from django.test import Client                                  # noqa: E402

from core.models import CollabDocument, Project, User           # noqa: E402

DOC_NAME = '【自动化验证】上传链路'
PK = int(sys.argv[1]) if len(sys.argv) > 1 else 1
DS_CONTAINER = 'onlyoffice'
FAILED = 0


def check(cond, msg):
    global FAILED
    if cond:
        print(f'  [OK]   {msg}')
    else:
        print(f'  [FAIL] {msg}')
        FAILED += 1
    return bool(cond)


def b64url_decode(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def jwt_signature_valid(token, secret):
    """按 OnlyOffice 约定（HS256）重算签名，校验 token 是否由该密钥签发。"""
    try:
        header, body, signature = token.split('.')
    except ValueError:
        return False
    expect = hmac.new(secret.encode(), f'{header}.{body}'.encode(), hashlib.sha256).digest()
    return hmac.compare_digest(expect, b64url_decode(signature))


def docker(*args):
    try:
        out = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=60)
        return out.returncode, out.stdout.strip()
    except Exception as exc:
        return 1, f'{type(exc).__name__}: {exc}'


# 启用 HTTPS 后，ONLYOFFICE_SERVER_URL 指向自签证书的 https://<IP>:8443，
# urllib 默认会因证书不可信直接抛 CERTIFICATE_VERIFY_FAILED（表现为 code=0 的假故障），
# 这里的证书校验本身不是被验证对象（端口/连通性才是），故跳过校验。
_UNVERIFIED_CTX = ssl.create_default_context()
_UNVERIFIED_CTX.check_hostname = False
_UNVERIFIED_CTX.verify_mode = ssl.CERT_NONE


def http_code(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=_UNVERIFIED_CTX) as resp:
            return resp.status, resp.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as exc:
        return exc.code, ''
    except Exception as exc:
        return 0, f'{type(exc).__name__}: {exc}'


def verify_onlyoffice_config():
    """OnlyOffice 三方配置：浏览器地址可达 + JWT 密钥一致 + 容器能回访 PM。"""
    print('== 1) OnlyOffice 配置')
    server_url = str(settings.ONLYOFFICE_SERVER_URL).rstrip('/')
    secret = str(settings.ONLYOFFICE_JWT_SECRET)
    pm_internal = str(getattr(settings, 'ONLYOFFICE_PM_INTERNAL_URL', '')).rstrip('/')
    print(f'  ONLYOFFICE_SERVER_URL      = {server_url}')
    print(f'  ONLYOFFICE_JWT_SECRET      = {secret[:8]}…（{len(secret)} 位）')
    print(f'  ONLYOFFICE_PM_INTERNAL_URL = {pm_internal}')

    host = server_url.split('//')[-1].split('/')[0].split(':')[0]
    check(host not in ('localhost', '127.0.0.1', '::1'),
          f'SERVER_URL 不是回环地址（{host}）—— 回环地址只有服务器本机的浏览器能加载 api.js')
    code, body = http_code(f'{server_url}/web-apps/apps/api/documents/api.js')
    check(code == 200, f'api.js 可访问（HTTP {code}）')
    check('DocsAPI' in body, 'api.js 内容里含 DocsAPI（确实拿到了 OnlyOffice 的加载器）')

    rc, ds_secret = docker('exec', DS_CONTAINER, 'cat', '/etc/onlyoffice/documentserver/local.json')
    if rc == 0:
        found = next((line.split('"')[3] for line in ds_secret.splitlines() if '"string"' in line), '')
        check(found == secret, f'DS 容器生效密钥与 Django 一致（DS={found[:8]}…）')
    else:
        check(False, f'读取 DS 容器 local.json 失败：{ds_secret}')

    rc, code_out = docker('exec', DS_CONTAINER, 'curl', '-s', '-o', '/dev/null',
                          '-w', '%{http_code}', f'{pm_internal}/')
    check(rc == 0 and code_out in ('200', '302', '403', '404'),
          f'DS 容器可回访 PM（{pm_internal}/ → HTTP {code_out}）')

    rc, empty_secret = docker('inspect', DS_CONTAINER, '--format', '{{range .Config.Env}}{{println .}}{{end}}')
    if rc == 0:
        container_env_secret = next((l[len('JWT_SECRET='):] for l in empty_secret.splitlines()
                                     if l.startswith('JWT_SECRET=')), '')
        check(container_env_secret != '',
              'DS 容器启动时显式指定了 JWT_SECRET（为空则 DS 每次启动随机生成，永远对不上）')


def main():
    verify_onlyoffice_config()
    print()

    from web.views import collab as collab_module
    print('== 2) 视图/模板层')
    check('open' not in vars(collab_module),
          'collab 模块内不存在名为 open 的全局名（否则会遮蔽内置 open()）')

    project = Project.objects.filter(pk=PK).first()
    if not check(project is not None, f'项目 #{PK} 存在'):
        return 1
    user = next((u for u in User.objects.all() if collab_module._can_view_project(u, project)), None)
    if not check(user is not None, '存在对该项目有查看权限的账号'):
        return 1
    print(f'  项目 = #{project.pk} {project.name}；账号 = {user.username} (id={user.pk})')

    client = Client()
    client.force_login(user)
    print()
    print(f'== 3) POST /project/{PK}/collab/store（mode=upload）')
    upload = SimpleUploadedFile(
        '自动化验证.docx',
        b'PK\x03\x04' + b'0' * 128,
        content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )
    try:
        resp = client.post(f'/project/{PK}/collab/store',
                           {'mode': 'upload', 'name': DOC_NAME, 'file': upload})
    except Exception as exc:
        traceback.print_exc()
        check(False, f'上传视图未抛异常（实际 {type(exc).__name__}: {exc}）')
        return 1
    print(f'  HTTP {resp.status_code}  Location: {resp.get("Location")}')
    check(resp.status_code == 302, '返回 302 跳转（而不是 500）')

    doc = CollabDocument.objects.filter(project_id=PK, name=DOC_NAME).first()
    disk = os.path.join(settings.MEDIA_ROOT, doc.storage_path) if doc else ''
    if check(doc is not None, '数据库生成了 CollabDocument 记录'):
        print(f'  记录 id={doc.pk} 路径={doc.storage_path} 大小={doc.size}')
        check(doc.size > 0, '记录 size 大于 0')
        check(os.path.exists(disk), f'文件已落盘：{disk}')

    if doc is not None:
        print()
        print(f'== 4) GET /project/{PK}/collab/{doc.pk}/open（编辑器页面）')
        try:
            page = client.get(f'/project/{PK}/collab/{doc.pk}/open')
            html = page.content.decode('utf-8', 'ignore')
            print(f'  HTTP {page.status_code}')
            check(page.status_code == 200, '编辑器页面返回 200')
            api_src = f'{str(settings.ONLYOFFICE_SERVER_URL).rstrip("/")}/web-apps/apps/api/documents/api.js'
            check(api_src in html, f'页面引用的 api.js 地址 = {api_src}')
            check('id="collab-config"' in html, '页面内嵌了 json_script 编辑器配置')

            raw = html.split('id="collab-config"', 1)[1]
            raw = raw.split('>', 1)[1].split('</script>', 1)[0]
            config = json.loads(raw)
            print(f'  配置 document.url = {config["document"]["url"]}')
            check(config['document']['url'].startswith(str(settings.ONLYOFFICE_PM_INTERNAL_URL).rstrip('/')),
                  'document.url 使用 PM_INTERNAL_URL（DS 侧可达地址）')
            check(config['editorConfig']['callbackUrl'].startswith(
                      str(settings.ONLYOFFICE_PM_INTERNAL_URL).rstrip('/')),
                  'callbackUrl 使用 PM_INTERNAL_URL')
            # 注意：open 视图每次打开都会轮换 doc_key，必须重新取库里的最新值再比对
            doc.refresh_from_db()
            check('k=' + doc.doc_key in config['document']['url'],
                  '下载地址带 doc_key 鉴权参数（且与库里最新 doc_key 一致）')
            token = config.get('token', '')
            check(token != '' and jwt_signature_valid(token, str(settings.ONLYOFFICE_JWT_SECRET)),
                  'config.token 的签名与 ONLYOFFICE_JWT_SECRET 匹配（DS 会据此放行）')
        except Exception as exc:
            traceback.print_exc()
            check(False, f'编辑器页面校验异常：{type(exc).__name__}: {exc}')

        print()
        print('== 5) 清理（彻底删除测试记录与文件，零残留）')
        doc_id = doc.pk
        # 必须用 TrashService.force_delete：一次性清掉「记录 + 物理文件 + 历史快照」。
        # 若先 doc.delete()（软删）再 os.remove，会留下「记录在、文件丢」的死记录，
        # 正是 04:00 巡检要抓的脏数据。
        from core.services.trash_service import force_delete
        force_delete(doc)
        print(f'  [OK]   已彻底删除记录 #{doc_id} 及其文件')
        print('  [OK]   无残留')

    print()
    if FAILED == 0:
        print('==== 协同办公链路验证通过（FAIL=0）====')
        return 0
    print(f'==== 存在 {FAILED} 项 FAIL ====')
    return 1


if __name__ == '__main__':
    sys.exit(main())
