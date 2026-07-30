"""理想中继 v1 的线上协议：帧构造与解析。

这里刻意与传输层解耦，方便单测直接断言字节级契约。三条必须守住的规则：

1. **握手 ``connect`` 用裸帧**。官方插件的握手是 ``socket.send(...)`` 直发，
   不经 ``sendMessage()`` 的字段注入；因此 ``metadata`` 只有
   ``{msg_id, job_id, agent_id, timestamp}``，``payload`` 里也**没有**
   ``nodeType``。本项目最大的未知就是服务端会不会校验客户端身份，在握手帧
   上偏离上游是性价比最低的偏离。
2. **其余每一帧都注入** ``metadata.client`` 与 ``payload.nodeType``。
3. ``send_result`` 的 ``payload.data`` 是 **JSON 字符串**，不是嵌套对象。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .constants import NODE_TYPE


class ProtocolError(ValueError):
    """收到的帧不符合协议。"""


@dataclass(frozen=True)
class ExecRequest:
    """一次 ``send_message`` 里的 ``exec`` 请求。"""

    job_id: str
    msg_id: str
    from_node_id: str
    from_node_type: str
    content: str


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return str(uuid.uuid4())


def metadata(
    *,
    agent_id: str,
    device_id: str,
    job_id: str = "",
    msg_id: str | None = None,
) -> dict[str, Any]:
    return {
        "msg_id": msg_id or new_id(),
        "job_id": job_id,
        "agent_id": agent_id,
        "device_id": device_id,
        "timestamp": now_ms(),
    }


def envelope(
    message_type: str,
    *,
    agent_id: str,
    device_id: str,
    client: str,
    job_id: str = "",
    payload: dict[str, Any] | None = None,
    msg_id: str | None = None,
) -> dict[str, Any]:
    """构造一个普通出站帧（非握手）。"""
    meta = metadata(
        agent_id=agent_id, device_id=device_id, job_id=job_id, msg_id=msg_id
    )
    meta["client"] = client
    wire_payload = dict(payload or {})
    wire_payload["nodeType"] = NODE_TYPE
    return {"type": message_type, "metadata": meta, "payload": wire_payload}


def connect_frame(
    *,
    agent_id: str,
    device_id: str,
    node_name: str,
    access_token: str,
    refresh_token: str,
    client: str,
) -> dict[str, Any]:
    """握手帧——逐字对齐官方裸帧，**不要**走 :func:`envelope`。"""
    return {
        "type": "connect",
        "metadata": {
            "msg_id": new_id(),
            "job_id": new_id(),
            "agent_id": agent_id,
            "timestamp": now_ms(),
        },
        "payload": {
            "device_id": device_id,
            "node_name": node_name,
            "node_desc": f"{NODE_TYPE} {node_name}",
            "client": client,
            "token": access_token,
            "refresh_token": refresh_token,
        },
    }


def encode(frame: dict[str, Any]) -> str:
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


def parse_frame(raw: str | bytes) -> dict[str, Any]:
    """解析一条入站帧，做最小结构校验。"""
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("中继返回了非法 JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("中继消息必须是对象")
    if not isinstance(value.get("type"), str) or not value.get("type"):
        raise ProtocolError("中继消息缺少 type")
    for key in ("metadata", "payload"):
        field = value.get(key)
        if field is not None and not isinstance(field, dict):
            raise ProtocolError(f"中继消息的 {key} 必须是对象")
    return value


def parse_inner_data(data: Any) -> dict[str, Any]:
    """``payload.data`` 既可能是对象也可能是 JSON 字符串，两种都要吃下。"""
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except ValueError as exc:
            raise ProtocolError("payload.data 不是合法 JSON") from exc
        if not isinstance(parsed, dict):
            raise ProtocolError("payload.data 解析结果必须是对象")
        return parsed
    raise ProtocolError("payload.data 必须是对象或 JSON 字符串")


def parse_exec_request(frame: dict[str, Any]) -> ExecRequest:
    """从 ``send_message`` 里取出 exec 请求。

    调用方必须**先回 ack 再调用本函数**：ack 是让中继停止重投的信号，若因为
    解析失败而不回 ack，中继会无限重投同一条消息。
    """
    if frame.get("type") != "send_message":
        raise ProtocolError("不是 send_message 帧")
    meta = frame.get("metadata") or {}
    payload = frame.get("payload") or {}
    job_id = str(meta.get("job_id") or "").strip()
    if not job_id:
        raise ProtocolError("send_message 缺少 metadata.job_id")

    inner = parse_inner_data(payload.get("data"))
    inner_type = inner.get("type") or "message"
    if inner_type != "exec":
        raise ProtocolError(f"不支持的请求类型: {inner_type!r}")
    content = str(inner.get("content") or "").strip()
    if not content:
        raise ProtocolError("exec 请求内容为空")

    return ExecRequest(
        job_id=job_id,
        msg_id=str(meta.get("msg_id") or "").strip(),
        # from_node_id 缺失时退化为 job 维度的会话，不因此拒收整条消息。
        from_node_id=str(payload.get("from_node_id") or "").strip() or "unknown",
        from_node_type=str(payload.get("from_node_type") or "").strip(),
        content=content,
    )


def result_data(text: str, files: list[dict[str, Any]] | None = None) -> str:
    """``send_result.payload.data`` —— JSON **字符串**。"""
    body: dict[str, Any] = {"text": text}
    if files:
        body["files"] = files
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def ack_target(frame: dict[str, Any]) -> str:
    """``ack_send_result`` 对应的 job：三级兜底，与上游一致。"""
    payload = frame.get("payload") or {}
    meta = frame.get("metadata") or {}
    return str(
        payload.get("ref_msg_id")
        or meta.get("job_id")
        or meta.get("msg_id")
        or ""
    )


def frame_summary(frame: dict[str, Any]) -> dict[str, str]:
    """可安全写日志的帧摘要（不含 payload 内容）。"""
    meta = frame.get("metadata") or {}
    return {
        "type": str(frame.get("type") or ""),
        "job_id": str(meta.get("job_id") or ""),
        "msg_id": str(meta.get("msg_id") or ""),
    }
