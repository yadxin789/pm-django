# -*- coding: utf-8 -*-
"""OnlyOffice DocumentServer 集成服务：JWT、编辑器配置、模板生成（对齐 CollabOfficeService.php）。"""
import base64
import hashlib
import hmac
import io
import json
import os
import zipfile
from urllib.parse import quote

from django.conf import settings

DOCX = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:t>协同办公文档</w:t></w:r></w:p><w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>'''
DOCX_TYPES = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
DOCX_RELS = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'

XLSX_TYPES = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
XLSX_WB = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
XLSX_WB_RELS = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
XLSX_SHEET = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>协同办公表格</t></is></c></row></sheetData></worksheet>'


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def server_url():
    return str(settings.ONLYOFFICE_SERVER_URL).rstrip('/')


def jwt_secret():
    return str(settings.ONLYOFFICE_JWT_SECRET)


def pm_internal_url():
    return str(getattr(settings, 'ONLYOFFICE_PM_INTERNAL_URL', 'http://host.docker.internal:8000')).rstrip('/')


def generate_jwt(payload):
    header = _b64url(json.dumps({'alg': 'HS256', 'typ': 'JWT'}).encode())
    body = _b64url(json.dumps(payload).encode())
    sig = hmac.new(jwt_secret().encode(), (header + '.' + body).encode(), hashlib.sha256).digest()
    return header + '.' + body + '.' + _b64url(sig)


def document_type(extension):
    return {'docx': 'word', 'xlsx': 'cell', 'pptx': 'slide'}.get(str(extension).lower(), 'word')


def download_url(doc):
    return pm_internal_url() + '/collab/download/' + str(doc.id) + '?k=' + quote(str(doc.doc_key), safe='')


def callback_url(doc):
    return pm_internal_url() + '/collab/callback/' + str(doc.id) + '?k=' + quote(str(doc.doc_key), safe='')


def build_editor_config(doc, user, can_edit=True):
    config = {
        'type': 'desktop',
        'documentType': document_type(doc.extension),
        'document': {
            'fileType': str(doc.extension).lower(),
            'key': doc.doc_key,
            'title': f'{doc.name}.{doc.extension}',
            'url': download_url(doc),
            'permissions': {'edit': can_edit, 'download': True, 'print': True, 'review': True},
        },
        'editorConfig': {
            'mode': 'edit' if can_edit else 'view',
            'callbackUrl': callback_url(doc),
            'lang': 'zh-CN',
            'user': {'id': str(user.id), 'name': user.name},
            'customization': {'autosave': True, 'forcesave': True, 'compactHeader': False, 'hideRightMenu': False},
        },
    }
    if jwt_secret():
        config['token'] = generate_jwt({
            'document': config['document'],
            'editorConfig': config['editorConfig'],
            'documentType': config['documentType'],
        })
    return config


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def _resources_template_path(ext):
    """仓库内随代码部署的真实模板文件路径（resources/templates/template.<ext>）。"""
    return os.path.join(str(settings.BASE_DIR), 'resources', 'templates', 'template.' + ext)


def build_template(extension):
    ext = str(extension).lower()
    if ext == 'docx':
        return _zip({'[Content_Types].xml': DOCX_TYPES, '_rels/.rels': DOCX_RELS, 'word/document.xml': DOCX})
    if ext == 'xlsx':
        return _zip({'[Content_Types].xml': XLSX_TYPES, '_rels/.rels': XLSX_WB_RELS,
                     'xl/workbook.xml': XLSX_WB, 'xl/_rels/workbook.xml.rels': XLSX_WB_RELS,
                     'xl/worksheets/sheet1.xml': XLSX_SHEET})
    if ext == 'pptx':
        # pptx 必须使用由 python-pptx 生成的标准文件：手搓 OOXML 会触发
        # OnlyOffice C++ 渲染器的 createDuplicate 崩溃（对齐 Laravel buildPptxFile）。
        path = _resources_template_path('pptx')
        if not os.path.isfile(path):
            raise FileNotFoundError(
                'PPT 模板文件不存在：%s。请确认 resources/templates/template.pptx 已随代码部署。' % path)
        with open(path, 'rb') as f:
            return f.read()
    raise ValueError('不支持的模板类型: ' + ext)


def ensure_templates(template_dir):
    for ext in ('docx', 'xlsx', 'pptx'):
        path = os.path.join(template_dir, 'template.' + ext)
        if not os.path.exists(path):
            os.makedirs(template_dir, exist_ok=True)
            with open(path, 'wb') as f:
                f.write(build_template(ext))
