# -*- coding: utf-8 -*-
"""文件服务（对齐 Laravel app/Services/FileService.php）。

集中维护：
  - 可预览的 mime 类型白名单
  - 文件大小格式化
  - 允许的扩展名列表
  - 软删除入口（统一走 TrashService）
"""
from __future__ import annotations

PREVIEWABLE_MIME = {
    'application/pdf',
    'image/jpeg',
    'image/png',
    'image/gif',
    'image/webp',
    'text/plain',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}

ALLOWED_EXTENSIONS = [
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
    'jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp',
    'txt', 'csv', 'zip', 'rar', '7z',
]


def can_preview(mime_type: str | None) -> bool:
    return bool(mime_type) and mime_type in PREVIEWABLE_MIME


def format_file_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.2f} {unit}'
        size /= 1024
    return f'{size:.2f} TB'


def get_allowed_extensions() -> list[str]:
    return list(ALLOWED_EXTENSIONS)


def safe_delete(file_model):
    """符合规范的软删除：只标记 deleted_at，保留物理文件，可恢复。

    所有删除入口最终都应通过本服务 / TrashService 走软删路径。
    """
    from core.services.trash_service import soft_delete
    return soft_delete(file_model)