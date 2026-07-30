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


# ---------------------------------------------------------------------------
# 生产中继实录帧（2026-07-30 联调抓取）
#
# 这些是真实理想中继发来的原始帧，逐字固化。代码里"看起来对"的解析，只有对
# 上真实字节才算数 —— 下面每条都对应一个曾经差点写错的地方。
# ---------------------------------------------------------------------------

REAL_CONNECTED = {
    "type": "connected",
    "metadata": {
        "msg_id": "66a61199-c53f-42d1-8404-ca39d41125c9",
        "device_id": "pc_a2bb9fd7",
        "client": "openclaw",
        "timestamp": 1785421561005,
    },
    "payload": {
        "client": "openclaw",
        "device_id": "pc_a2bb9fd7",
        # 中继分配的会话 id —— 官方插件完全忽略了它。
        "session_id": "f6c91ecf-3c50-4f10-b936-cec7499d6355",
    },
}

REAL_SEND_MESSAGE = {
    "type": "send_message",
    "metadata": {
        "msg_id": "595f54b6-b4e9-461f-b186-2a47d0a65d65",
        "job_id": "20260730222728-73e7df77-9c50-4cbc-9908-6be524f5b91a",
        "agent_id": "openclaw-a79d8c0d",
        "device_id": "pc_a2bb9fd7",
        "client": "openclaw",
        "timestamp": 1785421648561,
    },
    "payload": {
        # data 是 JSON **字符串**，且 inner 里有官方插件不读的 reply_to
        "data": '{"content":"好啊","reply_to":"8D4BA57C","type":"exec"}',
        "from_node_id": "8D4BA57C",
        "to_node_id": "pc_a2bb9fd7",
        # 注意：中继自己用的是拼写正确的 personal-device，
        # 而官方插件出站发的是拼错的 personl-device（实测服务端接受）。
        "to_node_type": "personal-device",
    },
}

REAL_ACK_SEND_RESULT = {
    "type": "ack_send_result",
    "metadata": {
        "msg_id": "f9f4f8f9-8c3f-4c3c-b921-10edebb29afc",
        "job_id": "20260730222728-73e7df77-9c50-4cbc-9908-6be524f5b91a",
        "agent_id": "openclaw-a79d8c0d",
        "device_id": "pc_a2bb9fd7",
        "client": "openclaw",
        "timestamp": 1785421648706,
    },
    # 没有 ref_msg_id！而且带业务状态码。
    "payload": {"code": "0", "message": "ok"},
}


def test_real_send_message_parses() -> None:
    request = protocol.parse_exec_request(REAL_SEND_MESSAGE)
    assert request.job_id == "20260730222728-73e7df77-9c50-4cbc-9908-6be524f5b91a"
    assert request.content == "好啊"
    assert request.from_node_id == "8D4BA57C"
    assert request.msg_id == "595f54b6-b4e9-461f-b186-2a47d0a65d65"


def test_real_send_message_carries_no_media() -> None:
    """实测：说「拍个照片儿」也只发来转写文本，没有任何附件字段。

    这条渠道没有图片入站通路 —— 若哪天中继开始带媒体字段，这个断言会失败，
    正好提醒我们去接。
    """
    inner = protocol.parse_inner_data(REAL_SEND_MESSAGE["payload"]["data"])
    assert set(inner) == {"content", "reply_to", "type"}
    media_ish = {"files", "images", "media", "objectKey", "url", "attachments"}
    assert not (media_ish & set(inner))
    assert not (media_ish & set(REAL_SEND_MESSAGE["payload"]))


def test_real_ack_has_no_ref_msg_id_and_falls_back_to_job_id() -> None:
    """生产 ack 里没有 ref_msg_id —— 只实现第一级会完全收不到确认。"""
    assert "ref_msg_id" not in REAL_ACK_SEND_RESULT["payload"]
    assert protocol.ack_target(REAL_ACK_SEND_RESULT) == (
        "20260730222728-73e7df77-9c50-4cbc-9908-6be524f5b91a"
    )


def test_real_ack_code_zero_is_success() -> None:
    ok, detail = protocol.ack_is_success(REAL_ACK_SEND_RESULT)
    assert ok is True
    assert "code=0" in detail


def test_nonzero_ack_code_is_failure() -> None:
    """服务端明确报错时不能当成已送达，否则这一轮回复被静默丢弃。"""
    frame = {"type": "ack_send_result", "payload": {"code": "500", "message": "boom"}}
    ok, detail = protocol.ack_is_success(frame)
    assert ok is False
    assert "boom" in detail


def test_missing_ack_code_defaults_to_success() -> None:
    """没有 code 字段时按成功处理，不要把成功的投递误判成失败去重发。"""
    assert protocol.ack_is_success({"payload": {}})[0] is True
    assert protocol.ack_is_success({})[0] is True


def test_non_numeric_ack_code_defaults_to_success() -> None:
    ok, detail = protocol.ack_is_success({"payload": {"code": "OK"}})
    assert ok is True
    assert "OK" in detail


def test_real_connected_frame_parses() -> None:
    assert protocol.parse_frame(json.dumps(REAL_CONNECTED))["type"] == "connected"
