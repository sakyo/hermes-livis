"""文档上传 —— ``send_result.files`` 的来源。

中继只接受 pdf / html / htm / md / markdown / doc / docx，单文件 ≤100 MB。
返回的描述对象字段名必须与官方一致（``objectKey`` / ``fileSuffix`` /
``file_path`` …），理想 APP 端按这些键解析。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from .constants import (
    ALLOWED_DOCUMENT_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    MIME_TYPES,
    http_url,
)
from .safeio import redact_body

logger = logging.getLogger(__name__)


def upload_rejection_reason(file_path: str) -> str | None:
    """返回不可上传的原因；``None`` 表示可以上传。先查再传，省一趟网络。"""
    path = Path(file_path)
    ext = path.suffix.lstrip(".").lower()
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))
        return f"理想眼镜渠道不支持 .{ext or '?'} 附件（仅支持 {allowed}）"
    try:
        size = path.stat().st_size
    except OSError:
        return f"文件不存在: {path.name}"
    if size > MAX_UPLOAD_BYTES:
        return f"文件过大 {size / 1024 / 1024:.1f} MB（上限 100 MB）"
    return None


async def upload_document(
    credentials: Any,
    file_path: str,
    *,
    job_id: str,
    client: str,
    display_name: str | None = None,
) -> dict[str, Any]:
    """上传一个文档，返回可直接放进 ``send_result.files`` 的描述对象。"""
    import aiohttp

    reason = upload_rejection_reason(file_path)
    if reason:
        raise ValueError(reason)

    path = Path(file_path)
    ext = path.suffix.lstrip(".").lower()
    mime = MIME_TYPES.get(ext, "application/octet-stream")
    size = path.stat().st_size
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"读取文件失败: {exc}") from exc

    token = await credentials.get_access_token()
    params = {
        "client": client,
        "job_id": job_id or str(uuid.uuid4()),
        "msg_id": str(uuid.uuid4()),
        "file_size": str(size),
        "file_name": display_name or path.stem,
        "file_suffix": ext,
        "file_type": mime,
    }

    form = aiohttp.FormData()
    form.add_field("file", blob, filename=path.name, content_type=mime)

    timeout = aiohttp.ClientTimeout(total=180)
    try:
        # trust_env=False：不读 HTTP_PROXY 等环境变量，附件与 Bearer 令牌不会
        # 被环境里的代理截获。
        async with (
            aiohttp.ClientSession(timeout=timeout, trust_env=False) as session,
            session.post(
                f"{http_url()}/api/v1/files/upload",
                params=params,
                data=form,
                headers={"Authorization": f"Bearer {token}"},
                allow_redirects=False,
            ) as resp,
        ):
            status = resp.status
            body = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"上传网络错误: {exc}") from exc

    if status >= 400 or not isinstance(body, dict):
        raise RuntimeError(f"上传失败 HTTP {status}: {redact_body(body)}")

    info = body.get("fileInfo") or {}
    object_key = str(body.get("objectKey") or "")
    if not object_key:
        raise RuntimeError(f"上传响应缺少 objectKey: {redact_body(body)}")

    # 上游用 objectKey 的中间路径段拼出展示用 file_path，逐字复刻。
    display_path = (
        "/" + "/".join(object_key.split("/")[1:-1]) if "/" in object_key else "/"
    )

    def _int(value: Any, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    return {
        "name": str(info.get("fileName") or display_name or path.stem),
        "fileSuffix": str(info.get("fileSuffix") or ext),
        "objectKey": object_key,
        "fileSize": _int(info.get("fileSize") or body.get("fileSize"), size),
        "fileType": str(info.get("fileType") or mime),
        "file_path": display_path,
        "expiresAt": _int(body.get("expiresAt"), 0),
    }
