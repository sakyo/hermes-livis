"""私有文件的原子写入与日志脱敏。

凭据、待投递结果这类文件必须满足三件事：
1. **原子替换** —— 崩溃时要么是旧内容要么是新内容，不能是半截；
2. **落盘** —— ``fsync`` 之后再 ``os.replace``，否则断电会丢；
3. **权限 0600 / 目录 0700** —— 里面有 refresh_token。

日志侧则要保证：任何来自服务端的错误正文在写进日志之前先脱敏，否则 IDaaS
的报错 body 里如果回显了 token，就会原样落进日志文件。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

# 命中 access_token / refresh_token / id_token / token 等 JSON 字段的值。
_JSON_SECRET_RE = re.compile(
    r'("(?:[a-z_]*_)?(?:access|refresh|id)?_?token"\s*:\s*")[^"]{4,}(")',
    re.IGNORECASE,
)
# 命中 form / query 串里的 token=...
_FORM_SECRET_RE = re.compile(
    r"((?:access_token|refresh_token|id_token|device_code|token)=)[^&\s\"']{4,}",
    re.IGNORECASE,
)
# 命中 Authorization: Bearer xxx
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.IGNORECASE)


def redact_text(value: str) -> str:
    """把文本里的令牌类字段替换成 ``[redacted]``。"""
    if not value:
        return ""
    text = _JSON_SECRET_RE.sub(r"\1[redacted]\2", value)
    text = _FORM_SECRET_RE.sub(r"\1[redacted]", text)
    text = _BEARER_RE.sub(r"\1[redacted]", text)
    return text


def redact_body(value: Any, limit: int = 500) -> str:
    """把服务端响应正文转成可安全写日志的短字符串。"""
    if isinstance(value, (dict, list)):
        try:
            raw = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            raw = str(value)
    else:
        raw = str(value)
    return redact_text(raw)[:limit]


def redact_secret(value: str | None, keep: int = 6) -> str:
    """遮蔽单个令牌/ID，用于日志。永远不要原样打印 token。"""
    if not value:
        return "<none>"
    text = str(value)
    if len(text) <= keep:
        return "*" * len(text)
    return f"{text[:keep]}…{len(text)}chars"


def ensure_private_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 目录已存在时 mkdir 的 mode 不生效，补一次 chmod；只读文件系统上失败无妨。
    with contextlib.suppress(OSError):
        os.chmod(target, 0o700)
    return target


def write_private_bytes(path: str | Path, data: bytes) -> None:
    """原子 + fsync + 0600 地写一个私有文件。

    ``O_EXCL`` 的临时文件名带 PID 与随机后缀，避免同机多进程互相覆盖；
    ``os.replace`` 在同一文件系统内是原子的。
    """
    target = Path(path)
    ensure_private_dir(target.parent)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        with contextlib.suppress(OSError):
            os.chmod(target, 0o600)
    finally:
        # 正常路径下 tmp 已被 replace 掉；这里只兜写入失败的残留。
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def write_private_text(path: str | Path, text: str) -> None:
    write_private_bytes(path, text.encode("utf-8"))


def write_private_json(path: str | Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    write_private_bytes(path, payload.encode("utf-8"))


def read_json(path: str | Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
