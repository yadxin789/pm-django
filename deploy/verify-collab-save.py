# -*- coding: utf-8 -*-
"""协同办公「编辑后保存」回存闭环验证（不依赖浏览器，但全程走真实 HTTP）。

历史上这条链路是坏的（三处叠加）：
  1) 视图函数曾叫 open，遮蔽内置 open() → 文件读写全炸（已修）
  2) 回调地址用的是浏览器不可达的 localhost:8080 / 容器不可达的 8000 端口（已修）
  3) **callback 没有 @csrf_exempt** → DS 服务器 POST 过来一律 403，文档永远存不回去（本次修）

本脚本验证：
  A. 外部无 CSRF token 的 POST 能进回调（用 enforce_csrf_checks=True 的测试客户端，等于真实外部请求）
  B. DS 容器网络层确实能打到回调地址（容器内 curl 真实 POST）
  C. 真实回存：让 DS 转换服务产出一个真实文件（落在 DS 自己的 cache 上）并作为回调 url 下发，
     PM 取回后应「旧版本留档 → 覆盖落盘 → size/version 更新」
  D. 重复回调幂等（同一次保存的 status=2/6 多次送达不会把版本刷上天）
  E. status=4（关闭且无修改）不应改动文件

结束后删除测试记录与文件（含历史版本），零残留。
用法：/var/www/pm-django/venv/bin/python deploy/verify-collab-save.py [项目ID，默认 1]
"""
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

import django

PROJECT_DIR = os.environ.get('PM_PROJECT_DIR', '/var/www/pm-django')
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


def load_env_file(path='/var/www/pm-django/.env'):
    """模拟 systemd EnvironmentFile：SSH 会话里没有这些变量，不加载会读到代码默认值。"""
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

from django.conf import settings                        # noqa: E402
from django.test import Client                          # noqa: E402

from core.models import CollabDocument, Project, User    # noqa: E402
from core.services import collab_office as office        # noqa: E402

DOC_NAME = '【自动化验证】保存闭环'
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


# 启用 HTTPS 后 DS 走自签证书的 https://<IP>:8443，urllib 默认会抛
# CERTIFICATE_VERIFY_FAILED（表现为 code=0 的假故障）。证书本身不是被验证对象，
# 这里跳过校验；证书的正确性由 deploy/verify-https-voice.py 单独验证。
_UNVERIFIED_CTX = ssl.create_default_context()
_UNVERIFIED_CTX.check_hostname = False
_UNVERIFIED_CTX.verify_mode = ssl.CERT_NONE


def post_json(url, payload, headers=None, timeout=60):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data,
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_UNVERIFIED_CTX) as resp:
            return resp.status, resp.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'ignore')
    except Exception as exc:
        return 0, f'{type(exc).__name__}: {exc}'


def docker(*args, timeout=120):
    out = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    return out.returncode, out.stdout.strip()


def make_pdf_via_ds(doc, source_url):
    """让 DS 转换服务把源文档转成 PDF —— 产出的文件真实落在 DS 的 cache 上，
    这正是 DS 保存回调里下发给业务系统的 url 的来源，用它才能测出真实取回过程。"""
    key = uuid.uuid4().hex
    body = {'async': False, 'url': source_url, 'outputtype': 'pdf', 'key': key}
    headers = {}
    if office.jwt_secret():
        headers['Authorization'] = 'Bearer ' + office.generate_jwt(body)
    status, text = post_json(f'{office.server_url()}/converter', body, headers)
    print(f'  DS /converter → HTTP {status}：{text[:300]}')
    try:
        data = json.loads(text)
    except ValueError:
        return ''
    return data.get('fileUrl') or data.get('url') or ''


def main():
    print('== 1) 准备：项目 / 账号 / 测试文档（模板方式新建，内容是合法 docx）')
    project = Project.objects.filter(pk=PK).first()
    if not check(project is not None, f'项目 #{PK} 存在'):
        return 1
    user = next((u for u in User.objects.all()
                 if u.is_admin() or u.is_project_manager()
                 or project.participants().filter(id=u.id).exists()), None)
    if not check(user is not None, '找到有权限的账号'):
        return 1
    print(f'  项目 = #{project.pk} {project.name}；账号 = {user.username}')

    # 两个客户端，各自模拟真实场景：
    #   web —— 浏览器（带登录态；Django 测试客户端默认不校验 CSRF）
    #   raw —— DS 服务器（未登录 + 强制校验 CSRF，正是回调的真实处境）
    #          没修 @csrf_exempt 时，raw 会拿到 403
    web = Client()
    web.force_login(user)
    raw = Client(enforce_csrf_checks=True)

    CollabDocument.objects.filter(project_id=PK, name=DOC_NAME).delete()
    resp = web.post(f'/project/{PK}/collab/store',
                    {'mode': 'template', 'name': DOC_NAME, 'extension': 'docx'})
    doc = CollabDocument.objects.filter(project_id=PK, name=DOC_NAME).first()
    if not check(doc is not None and resp.status_code == 302, f'新建文档成功（HTTP {resp.status_code}）'):
        return 1
    src = os.path.join(settings.MEDIA_ROOT, doc.storage_path)
    history_dir = os.path.join(settings.MEDIA_ROOT, 'collab', str(PK), 'history')
    history_v1 = os.path.join(history_dir, f'{doc.id}_v1.docx')
    print(f'  文档 id={doc.id} 版本=v{doc.version} 大小={doc.size} 路径={doc.storage_path}')
    v1_bytes = open(src, 'rb').read()

    print()
    print('== 2) 回调入口：外部无 CSRF token 的 POST 必须能进来（403 就是保存失败的原因）')
    cb_url = f'/collab/callback/{doc.id}?k={doc.doc_key}'
    resp = raw.post(cb_url, data=json.dumps({'key': '错误key', 'status': 1, 'url': 'http://x/y'}),
                    content_type='application/json')
    check(resp.status_code == 200, f'POST 回调返回 HTTP {resp.status_code}（403 = 被 CSRF 拦下）')
    check(resp.json().get('error') == 1, f'错误 key 被拒绝且返回 JSON error=1：{resp.json()}')

    print()
    print('== 3) DS 容器网络层真实 POST 回调地址')
    rc, out = docker('exec', DS_CONTAINER, 'curl', '-s', '-w', '\n%{http_code}', '-X', 'POST',
                     '-H', 'Content-Type: application/json',
                     '-d', json.dumps({'key': '错误key', 'status': 1}),
                     f'{office.pm_internal_url()}/collab/callback/{doc.id}')
    tail = out.rsplit('\n', 1)[-1].strip() if out else ''
    check(rc == 0 and tail == '200', f'容器内 POST {office.pm_internal_url()}/collab/callback/{doc.id} → HTTP {tail}')

    print()
    print('== 4) status=4（关闭且无修改）不应改动文件')
    before = os.path.getsize(src)
    resp = raw.post(cb_url, data=json.dumps({'key': doc.doc_key, 'status': 4}),
                    content_type='application/json')
    doc.refresh_from_db()
    check(resp.status_code == 200 and resp.json().get('error') == 0, '返回 error=0')
    check(doc.version == 1 and os.path.getsize(src) == before, '版本与文件大小都没变')

    print()
    print('== 5) 真实回存：用 DS 转换服务产出的文件作为「DS 下发的新版本」')
    source_url = office.download_url(doc)
    print(f'  DS 取源文件地址（= 编辑器 document.url）= {source_url}')
    pdf_url = make_pdf_via_ds(doc, source_url)
    if not check(bool(pdf_url), 'DS 转换成功并返回可下载地址'):
        pdf_url = ''
    if pdf_url:
        resp = raw.post(cb_url,
                           data=json.dumps({'key': doc.doc_key, 'status': 2, 'url': pdf_url}),
                           content_type='application/json')
        doc.refresh_from_db()
        saved = open(src, 'rb').read()
        print(f'  回调响应：HTTP {resp.status_code} {resp.text[:120]}')
        check(resp.status_code == 200 and resp.json().get('error') == 0, '回调返回 error=0（DS 侧视为保存成功）')
        check(saved.startswith(b'%PDF'), '落盘内容 = DS 下发的 PDF（前 4 字节 %PDF）')
        check(saved != v1_bytes, '文件内容确实被新版本覆盖')
        check(doc.version == 2, f'版本号 +1 → v{doc.version}')
        check(doc.size == len(saved), f'记录的 size 与文件一致（{doc.size} 字节）')
        check(os.path.exists(history_v1), f'旧版本已留档：collab/{PK}/history/{doc.id}_v1.docx')
        if os.path.exists(history_v1):
            check(open(history_v1, 'rb').read() == v1_bytes, '留档内容 = 保存前的原始文件')

        print()
        print('== 6) 重复回调幂等（DS 会把同一次保存多次送达，不能把版本刷上天）')
        resp = raw.post(cb_url,
                        data=json.dumps({'key': doc.doc_key, 'status': 2, 'url': pdf_url}),
                        content_type='application/json')
        doc.refresh_from_db()
        check(resp.json().get('error') == 0, '重复回调仍返回 error=0')
        check(doc.version == 2, f'版本号没有重复自增（仍为 v{doc.version}）')

    print()
    print('== 7) 清理（记录 + 正文 + 历史版本 + 可能的临时文件）')
    doc_id = doc.id
    # force_delete 会一并清掉正文与 history/ 快照，保证「记录与文件」同步消失；
    # 只有不受管的 .tmp 临时文件需要手动删。
    from core.services.trash_service import force_delete
    force_delete(doc)
    removed = []
    for path in (src + '.tmp',):
        if os.path.exists(path):
            os.remove(path)
            removed.append(os.path.basename(path))
    print(f'  [OK]   已删除记录 #{doc_id} 与文件：{", ".join(removed) if removed else "（无）"}')

    print()
    if FAILED == 0:
        print('==== 保存回存闭环验证通过（FAIL=0）====')
        return 0
    print(f'==== 存在 {FAILED} 项 FAIL ====')
    return 1


if __name__ == '__main__':
    sys.exit(main())
