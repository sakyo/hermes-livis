"""协议层测试 —— 不需要 Hermes，纯字节级契约。"""

from __future__ import annotations

import json

import pytest

from hermes_livis.plugin import protocol
from hermes_livis.plugin.constants import NODE_TYPE

AGENT = "openclaw-a"
DEVICE = "pc_d"


def test_node_type_keeps_upstream_spelling() -> None:
    """上游拼写是 personl-device（少一个 a），不要"顺手修正"。"""
    assert NODE_TYPE == "personl-device"


def test_connect_frame_is_bare() -> None:
    """握手是裸帧：不注入 metadata.client / metadata.device_id / payload.nodeType。

    这是本项目最该守住的保真点 —— 唯一的大风险是服务端校验客户端身份，在握手
    帧上偏离上游性价比最低。
    """
    frame = protocol.connect_frame(
        agent_id=AGENT,
        device_id=DEVICE,
        node_name="我的电脑",
        access_token="at",
        refresh_token="rt",
        client="openclaw",
    )
    assert frame["type"] == "connect"
    assert set(frame["metadata"]) == {"msg_id", "job_id", "agent_id", "timestamp"}
    assert frame["metadata"]["agent_id"] == AGENT
    assert "client" not in frame["metadata"]
    assert "device_id" not in frame["metadata"]

    payload = frame["payload"]
    assert "nodeType" not in payload
    assert payload["device_id"] == DEVICE
    assert payload["client"] == "openclaw"
    assert payload["token"] == "at"
    assert payload["refresh_token"] == "rt"
    assert payload["node_desc"] == "personl-device 我的电脑"


def test_envelope_injects_client_and_node_type() -> None:
    frame = protocol.envelope(
        "heartbeat", agent_id=AGENT, device_id=DEVICE, client="openclaw"
    )
    assert frame["metadata"]["client"] == "openclaw"
    assert frame["metadata"]["device_id"] == DEVICE
    assert frame["payload"]["nodeType"] == NODE_TYPE


def test_envelope_keeps_empty_job_id() -> None:
    """``token_refresh`` 的 job_id 是空串，不能被替换成随机 UUID。"""
    frame = protocol.envelope(
        "token_refresh", agent_id=AGENT, device_id=DEVICE, client="c", job_id=""
    )
    assert frame["metadata"]["job_id"] == ""


@pytest.mark.parametrize("as_string", [False, True])
def test_parse_exec_request_accepts_both_data_shapes(as_string: bool) -> None:
    inner = {"type": "exec", "content": "今天天气如何"}
    frame = {
        "type": "send_message",
        "metadata": {"msg_id": "m", "job_id": "j"},
        "payload": {
            "from_node_id": "g1",
            "from_node_type": "glasses",
            "data": json.dumps(inner) if as_string else inner,
        },
    }
    request = protocol.parse_exec_request(frame)
    assert request.job_id == "j"
    assert request.content == "今天天气如何"
    assert request.from_node_id == "g1"


def test_missing_from_node_id_degrades_rather_than_rejects() -> None:
    """from_node_id 缺失只降级为 unknown，不因此拒收整条消息。"""
    frame = {
        "type": "send_message",
        "metadata": {"job_id": "j"},
        "payload": {"data": {"type": "exec", "content": "hi"}},
    }
    assert protocol.parse_exec_request(frame).from_node_id == "unknown"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"type": "notify", "content": "x"}},
        {"data": {"type": "exec", "content": "   "}},
        {"data": "not json"},
        {"data": 42},
    ],
)
def test_unsupported_payloads_raise(payload: dict) -> None:
    frame = {"type": "send_message", "metadata": {"job_id": "j"}, "payload": payload}
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_exec_request(frame)


def test_missing_job_id_raises() -> None:
    frame = {
        "type": "send_message",
        "metadata": {},
        "payload": {"data": {"type": "exec", "content": "hi"}},
    }
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_exec_request(frame)


def test_result_data_is_a_json_string() -> None:
    raw = protocol.result_data("你好")
    assert isinstance(raw, str)
    assert json.loads(raw) == {"text": "你好"}
    assert "files" not in json.loads(raw)

    with_files = json.loads(protocol.result_data("x", [{"objectKey": "k"}]))
    assert with_files["files"] == [{"objectKey": "k"}]


def test_result_data_keeps_unicode_unescaped() -> None:
    assert "你好" in protocol.result_data("你好")


def test_ack_target_prefers_ref_msg_id() -> None:
    assert (
        protocol.ack_target(
            {"payload": {"ref_msg_id": "a"}, "metadata": {"job_id": "b", "msg_id": "c"}}
        )
        == "a"
    )
    assert protocol.ack_target({"metadata": {"job_id": "b", "msg_id": "c"}}) == "b"
    assert protocol.ack_target({"metadata": {"msg_id": "c"}}) == "c"
    assert protocol.ack_target({}) == ""


@pytest.mark.parametrize(
    "raw", ["not json", "[1,2]", '{"metadata": 3}', '{"type": ""}', '{"no": "type"}']
)
def test_parse_frame_rejects_malformed(raw: str) -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_frame(raw)


def test_frame_summary_leaks_no_payload() -> None:
    summary = protocol.frame_summary(
        {"type": "send_message", "metadata": {"job_id": "j"}, "payload": {"data": "秘密"}}
    )
    assert summary == {"type": "send_message", "job_id": "j", "msg_id": ""}
