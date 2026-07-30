"""适配器行为测试：收口、取消、ack 重试、持久化、授权、附件白名单。

不开端口、不碰真实理想服务：直接调 ``_handle_frame`` / ``send`` /
``on_processing_complete``，把出站帧收进一个假 WebSocket 里断言。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import AGENT_ID, DEVICE_ID, requires_hermes

pytestmark = requires_hermes


def _imports():
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageEvent, ProcessingOutcome

    from hermes_livis.plugin import adapter as mod

    return PlatformConfig, MessageEvent, ProcessingOutcome, mod


class FakeWS:
    """只记录 ``send()`` 内容的假 WebSocket。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed_with: Any = None

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)

    def by_type(self, kind: str) -> list[dict[str, Any]]:
        return [frame for frame in self.sent if frame.get("type") == kind]

    def results(self) -> list[dict[str, Any]]:
        return [json.loads(f["payload"]["data"]) for f in self.by_type("send_result")]


def make_adapter(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FakeWS]:
    PlatformConfig, _MessageEvent, _Outcome, mod = _imports()
    adapter = mod.LivisAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._agent_id = AGENT_ID
    adapter._device_id = DEVICE_ID
    ws = FakeWS()
    adapter._ws = ws
    return adapter, ws


def capture_inbound(adapter: Any, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    events: list[Any] = []

    async def fake_handle(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    return events


def frame(
    job_id: str,
    content: str = "今天天气如何",
    *,
    as_string: bool = False,
    from_node_id: str = "glasses-1",
    inner_type: str = "exec",
) -> dict[str, Any]:
    inner: Any = {"type": inner_type, "content": content}
    if as_string:
        inner = json.dumps(inner, ensure_ascii=False)
    return {
        "type": "send_message",
        "metadata": {"msg_id": f"m-{job_id}", "job_id": job_id},
        "payload": {
            "from_node_id": from_node_id,
            "from_node_type": "glasses",
            "data": inner,
        },
    }


async def settle(times: int = 4) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 入站
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("as_string", [False, True])
async def test_send_message_acks_then_dispatches(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, as_string: bool
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    events = capture_inbound(adapter, monkeypatch)

    await adapter._handle_frame(frame("job-1", as_string=as_string))

    acks = ws.by_type("ack_send_message")
    assert len(acks) == 1
    assert acks[0]["metadata"]["job_id"] == "job-1"
    assert acks[0]["metadata"]["client"] == "openclaw"
    assert acks[0]["payload"]["nodeType"] == "personl-device"

    assert len(events) == 1
    event = events[0]
    assert event.text == "今天天气如何"
    # message_id == job_id：base 的 _reply_anchor_for_event 靠它回传 reply_to。
    assert event.message_id == "job-1"
    # metadata 里也存一份：on_processing_complete 靠这个键定位 job。
    assert event.metadata["livis_job_id"] == "job-1"
    assert event.source.chat_id == "glasses-1"


async def test_unsupported_inner_type_is_still_acked(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """解析失败也必须回 ack —— 否则中继会无限重投这条消息。"""
    adapter, ws = make_adapter(monkeypatch)
    events = capture_inbound(adapter, monkeypatch)

    await adapter._handle_frame(frame("job-x", inner_type="notify"))

    assert len(ws.by_type("ack_send_message")) == 1
    assert events == []


async def test_malformed_payload_is_still_acked(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    events = capture_inbound(adapter, monkeypatch)

    bad = frame("job-bad")
    bad["payload"]["data"] = "{not json"
    await adapter._handle_frame(bad)

    assert len(ws.by_type("ack_send_message")) == 1
    assert events == []


async def test_frame_without_job_id_is_acked_and_ignored(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    events = capture_inbound(adapter, monkeypatch)

    bad = frame("job-y")
    bad["metadata"]["job_id"] = ""
    await adapter._handle_frame(bad)

    assert len(ws.by_type("ack_send_message")) == 1
    assert events == []


async def test_replay_is_deduped_but_still_acked(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    events = capture_inbound(adapter, monkeypatch)

    for _ in range(3):
        await adapter._handle_frame(frame("job-dup"))

    assert len(ws.by_type("ack_send_message")) == 3
    assert len(events) == 1


async def test_replay_of_unacked_result_redelivers(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重放 + 结果未被 ack ⇒ 对端没收到，补发已有结果而不是重跑 agent。"""
    adapter, ws = make_adapter(monkeypatch)
    events = capture_inbound(adapter, monkeypatch)

    await adapter._handle_frame(frame("job-1"))
    await adapter.send("glasses-1", "答案", reply_to="job-1")
    await _finish(adapter, "job-1")
    assert adapter._store.is_pending("job-1")

    await adapter._handle_frame(frame("job-1"))

    assert len(events) == 1
    assert len(ws.by_type("send_result")) == 2
    assert ws.results()[0] == ws.results()[1] == {"text": "答案"}


# ---------------------------------------------------------------------------
# 收口：钩子 / 兜底 / 看门狗
# ---------------------------------------------------------------------------

async def _finish(adapter: Any, job_id: str, outcome: Any = None) -> None:
    """模拟 hermes 触发 on_processing_complete。"""
    _P, _M, ProcessingOutcome, _mod = _imports()
    job = adapter._jobs[job_id]
    event = type("E", (), {"metadata": {"livis_job_id": job_id}, "message_id": job_id})()
    await adapter.on_processing_complete(event, outcome or ProcessingOutcome.SUCCESS)
    await settle()
    return job


async def test_hook_finalizes_immediately(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主收口信号：钩子一到就回包，不等兜底计时器。"""
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    # 把兜底窗口调到远大于测试时长，证明回包确实来自钩子。
    adapter._fallback_seconds = 30.0

    await adapter._handle_frame(frame("job-1"))
    await adapter.send("glasses-1", "北京今天晴。", reply_to="job-1")
    assert ws.by_type("send_result") == []  # 钩子未到 ⇒ 还没回包

    await _finish(adapter, "job-1")

    results = ws.by_type("send_result")
    assert len(results) == 1
    assert results[0]["metadata"]["job_id"] == "job-1"
    assert isinstance(results[0]["payload"]["data"], str)
    assert ws.results()[0] == {"text": "北京今天晴。"}


async def test_fallback_timer_fires_when_hook_never_comes(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hermes 的几条提前返回路径不触发钩子 —— 兜底计时器必须收口。"""
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    adapter._fallback_seconds = 0.05

    await adapter._handle_frame(frame("job-1"))
    await adapter.send("glasses-1", "兜底回复", reply_to="job-1")
    await asyncio.sleep(0.2)

    assert len(ws.by_type("send_result")) == 1
    assert ws.results()[0] == {"text": "兜底回复"}


async def test_hook_after_fallback_does_not_double_send(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """协议只允许一条结果：兜底先发了，迟到的钩子不能再发一条。"""
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    adapter._fallback_seconds = 0.05

    await adapter._handle_frame(frame("job-1"))
    await adapter.send("glasses-1", "回复", reply_to="job-1")
    await asyncio.sleep(0.2)
    await _finish(adapter, "job-1")

    assert len(ws.by_type("send_result")) == 1


async def test_watchdog_answers_a_silently_dropped_job(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连一次 send() 都没有（被授权拒绝等）时，眼镜也不能永远等不到回应。"""
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    adapter._watchdog_seconds = 0.05

    await adapter._handle_frame(frame("job-1"))
    await asyncio.sleep(0.25)

    results = ws.results()
    assert len(results) == 1
    _P, _M, _O, mod = _imports()
    assert results[0]["text"] == mod.WATCHDOG_RESULT_TEXT


async def test_watchdog_is_cancelled_after_a_normal_reply(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    adapter._watchdog_seconds = 0.1

    await adapter._handle_frame(frame("job-1"))
    await adapter.send("glasses-1", "答案", reply_to="job-1")
    await _finish(adapter, "job-1")
    await asyncio.sleep(0.3)

    assert len(ws.by_type("send_result")) == 1


async def test_text_and_document_merge_into_one_result(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """hermes 先 send() 再 send_document()，两者必须并进同一条结果。"""
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    _P, _M, _O, mod = _imports()

    doc = tmp_path / "report.md"
    doc.write_text("# 报告", encoding="utf-8")
    descriptor = {
        "name": "report",
        "fileSuffix": "md",
        "objectKey": "bucket/2026/07/report.md",
        "fileSize": 6,
        "fileType": "text/markdown",
        "file_path": "/2026/07",
        "expiresAt": 0,
    }

    async def fake_upload(creds, path, *, job_id, client, display_name=None):
        assert job_id == "job-1"
        assert client == "openclaw"
        return descriptor

    monkeypatch.setattr(mod, "upload_document", fake_upload)
    monkeypatch.setattr(adapter, "_safe_media_path", lambda p: str(p))

    await adapter._handle_frame(frame("job-1"))
    await adapter.send("glasses-1", "报告已生成。", reply_to="job-1")
    await adapter.send_document("glasses-1", str(doc), reply_to="job-1")
    await _finish(adapter, "job-1")

    assert len(ws.by_type("send_result")) == 1
    body = ws.results()[0]
    assert body["text"] == "报告已生成。"
    assert body["files"] == [descriptor]


async def test_long_reply_is_not_truncated(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    long_text = "长" * 12000

    await adapter._handle_frame(frame("job-1"))
    await adapter.send("glasses-1", long_text, reply_to="job-1")
    await _finish(adapter, "job-1")

    assert ws.results()[0]["text"] == long_text


async def test_empty_output_falls_back_to_spoken_notice(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    _P, _M, _O, mod = _imports()

    await adapter._handle_frame(frame("job-1"))
    await adapter.send("glasses-1", "   ", reply_to="job-1")
    await _finish(adapter, "job-1")

    assert ws.results()[0]["text"] == mod.EMPTY_RESULT_TEXT


async def test_failure_outcome_uses_failure_notice(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    _P, _M, ProcessingOutcome, mod = _imports()

    await adapter._handle_frame(frame("job-1"))
    await _finish(adapter, "job-1", ProcessingOutcome.FAILURE)

    assert ws.results()[0]["text"] == mod.FAILED_RESULT_TEXT


# ---------------------------------------------------------------------------
# 主动推送 / 孤立回复
# ---------------------------------------------------------------------------

async def test_send_without_job_fails_loudly(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    result = await adapter.send("nobody", "主动推送")
    assert result.success is False
    assert "不支持主动推送" in (result.error or "")
    assert ws.by_type("send_result") == []


async def test_orphan_reply_is_adopted_by_job_id(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重启后 delivery ledger 补发时内存台账已空，但 reply_to 仍可用。"""
    adapter, ws = make_adapter(monkeypatch)
    adapter._fallback_seconds = 0.05

    result = await adapter.send("glasses-1", "补发的回复", reply_to="job-old")
    assert result.success
    await asyncio.sleep(0.2)

    results = ws.by_type("send_result")
    assert len(results) == 1
    assert results[0]["metadata"]["job_id"] == "job-old"


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------

async def test_cancel_acks_interrupts_and_suppresses_result(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    interrupted: list[tuple] = []

    async def fake_interrupt(session_key, chat_id, metadata=None):
        interrupted.append((session_key, chat_id))

    monkeypatch.setattr(adapter, "interrupt_session_activity", fake_interrupt)

    await adapter._handle_frame(frame("job-1"))
    await adapter._handle_frame(
        {"type": "cancel_chat", "metadata": {"job_id": "job-1"}, "payload": {}}
    )
    await adapter.send("glasses-1", "迟到的答案", reply_to="job-1")
    await settle()

    acks = ws.by_type("ack_cancel_chat")
    assert len(acks) == 1 and acks[0]["metadata"]["job_id"] == "job-1"
    assert len(interrupted) == 1
    assert ws.by_type("send_result") == []


async def test_early_cancel_blocks_the_later_message(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    events = capture_inbound(adapter, monkeypatch)

    await adapter._handle_frame(
        {"type": "cancel_chat", "metadata": {"job_id": "job-2"}, "payload": {}}
    )
    await adapter._handle_frame(frame("job-2"))

    assert ws.by_type("ack_cancel_chat")
    assert ws.by_type("ack_send_message")
    assert events == []


async def test_cancelled_outcome_suppresses_result(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    _P, _M, ProcessingOutcome, _mod = _imports()

    await adapter._handle_frame(frame("job-1"))
    await adapter.send("glasses-1", "半截答案", reply_to="job-1")
    await _finish(adapter, "job-1", ProcessingOutcome.CANCELLED)

    assert ws.by_type("send_result") == []


# ---------------------------------------------------------------------------
# ack / 持久化 / 重连补发
# ---------------------------------------------------------------------------

async def test_ack_clears_pending_store(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)

    await adapter._handle_frame(frame("job-1"))
    await adapter.send("glasses-1", "答案", reply_to="job-1")
    await _finish(adapter, "job-1")
    assert adapter._store.is_pending("job-1")

    await adapter._handle_frame(
        {
            "type": "ack_send_result",
            "metadata": {"job_id": "job-1"},
            "payload": {"ref_msg_id": "job-1"},
        }
    )
    assert adapter._store.is_pending("job-1") is False
    assert adapter._store.is_completed("job-1") is True


async def test_result_is_queued_when_connection_is_down(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """断线时结果先落盘，重连后补发（刻意与上游"断线即 abort"不同）。"""
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)

    await adapter._handle_frame(frame("job-1"))
    adapter._ws = None
    await adapter.send("glasses-1", "答案", reply_to="job-1")
    await _finish(adapter, "job-1")
    assert adapter._store.is_pending("job-1")
    assert ws.by_type("send_result") == []

    adapter._ws = ws
    await adapter._redeliver_pending()

    assert len(ws.by_type("send_result")) == 1


async def test_pending_result_survives_process_restart(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """核心新增能力：hermes 崩在 send() 与真正投递之间时不丢答案。

    hermes 的 delivery ledger 在 send() 返回 True 时就标记"已送达"，因此它
    **不会**补发；这条补的正是那一段。
    """
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)

    await adapter._handle_frame(frame("job-1"))
    adapter._ws = None  # 模拟"还没发出去"
    await adapter.send("glasses-1", "崩溃前算好的答案", reply_to="job-1")
    await _finish(adapter, "job-1")

    # 新进程：全新适配器实例，只共享磁盘上的 state 目录
    revived, new_ws = make_adapter(monkeypatch)
    assert revived._store.pending_ids() == ["job-1"]
    await revived._redeliver_pending()

    assert new_ws.results()[0] == {"text": "崩溃前算好的答案"}


async def test_ack_timeout_retries_then_gives_up(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    _P, _M, _O, mod = _imports()
    monkeypatch.setattr(mod, "ACK_TIMEOUT", 0.01)

    await adapter._handle_frame(frame("job-1"))
    await adapter.send("glasses-1", "答案", reply_to="job-1")
    await _finish(adapter, "job-1")
    await asyncio.sleep(0.3)

    # 首发 + 最多 MAX_ACK_RETRIES 次重发
    assert 2 <= len(ws.by_type("send_result")) <= mod.MAX_ACK_RETRIES + 1
    assert adapter._store.is_pending("job-1") is False


# ---------------------------------------------------------------------------
# token 刷新
# ---------------------------------------------------------------------------

async def test_token_expiring_pushes_refreshed_token(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)

    async def fake_get(force: bool = False) -> str:
        assert force is True, "token_expiring 必须强制刷新"
        return "at-new"

    monkeypatch.setattr(adapter.creds, "get_access_token", fake_get)
    await adapter._refresh_token_on_relay()

    frames = ws.by_type("token_refresh")
    assert len(frames) == 1
    assert frames[0]["payload"]["token"] == "at-new"
    assert frames[0]["payload"]["refresh_token"] == "rt-test"
    assert frames[0]["metadata"]["job_id"] == ""
    if adapter._token_refresh_ack_task:
        adapter._token_refresh_ack_task.cancel()


async def test_repeated_token_failures_close_the_socket(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, ws = make_adapter(monkeypatch)
    _P, _M, _O, mod = _imports()

    for _ in range(mod.TOKEN_REFRESH_MAX_FAILURES):
        await adapter._note_token_failure("test")

    assert ws.closed_with is not None and ws.closed_with[0] == 1008


# ---------------------------------------------------------------------------
# 附件白名单
# ---------------------------------------------------------------------------

async def test_document_outside_media_roots_is_rejected(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """路径白名单：挡住被诱导的 agent 上传任意本地文件。"""
    adapter, _ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    _P, _M, _O, mod = _imports()

    called = {"upload": False}

    async def fake_upload(*_a, **_k):
        called["upload"] = True
        return {}

    monkeypatch.setattr(mod, "upload_document", fake_upload)
    monkeypatch.setattr(adapter, "validate_media_delivery_path", lambda p: None)

    secret = tmp_path / "secrets.md"
    secret.write_text("token", encoding="utf-8")

    await adapter._handle_frame(frame("job-1"))
    result = await adapter.send_document("glasses-1", str(secret), reply_to="job-1")

    assert result.success is False
    assert "不在允许投递的媒体目录" in (result.error or "")
    assert called["upload"] is False, "越界路径不该发起上传"


async def test_unsupported_attachment_type_is_rejected(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter, _ws = make_adapter(monkeypatch)
    capture_inbound(adapter, monkeypatch)
    monkeypatch.setattr(adapter, "_safe_media_path", lambda p: str(p))

    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")

    await adapter._handle_frame(frame("job-1"))
    result = await adapter.send_document("glasses-1", str(png), reply_to="job-1")

    assert result.success is False
    assert "png" in (result.error or "")


async def test_images_audio_video_are_refused(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, _ws = make_adapter(monkeypatch)
    assert (await adapter.send_image("c", "http://x/a.png")).success is False
    assert (await adapter.send_voice("c", "/tmp/a.ogg")).success is False
    assert (await adapter.send_video("c", "/tmp/a.mp4")).success is False


# ---------------------------------------------------------------------------
# 授权 / 注册
# ---------------------------------------------------------------------------

async def test_authorization_delegates_upstream_by_default(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, _ws = make_adapter(monkeypatch)
    assert adapter.authorization_is_upstream is True


async def test_local_allowlist_takes_over_when_set(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIVIS_ALLOWED_NODE_IDS", "glasses-1")
    adapter, _ws = make_adapter(monkeypatch)
    assert adapter.authorization_is_upstream is False


def test_check_requirements_needs_credentials(
    empty_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _P, _M, _O, mod = _imports()
    assert mod.check_requirements() is False


def test_check_requirements_true_with_credentials(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _P, _M, _O, mod = _imports()
    assert mod.check_requirements() is True


def test_livis_enabled_false_disables_platform(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _P, _M, _O, mod = _imports()
    monkeypatch.setenv("LIVIS_ENABLED", "false")
    assert mod.check_requirements() is False


def test_env_enablement_seeds_agent_id(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _P, _M, _O, mod = _imports()
    seed = mod.env_enablement()
    assert seed is not None and seed["agent_id"] == AGENT_ID


def test_register_declares_no_cron_delivery(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """理想中继没有主动推送通路，注册 cron 投递只会让它静默失败。"""
    _P, _M, _O, mod = _imports()
    captured: dict[str, Any] = {}
    cli_commands: list[str] = []

    class Ctx:
        def register_platform(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def register_cli_command(self, **kwargs: Any) -> None:
            cli_commands.append(kwargs["name"])

    mod.register(Ctx())
    assert captured["name"] == "livis"
    assert "cron_deliver_env_var" not in captured
    assert "standalone_sender_fn" not in captured
    assert captured["allow_update_command"] is False
    assert cli_commands == ["livis"]


# ---------------------------------------------------------------------------
# 登录晚于网关启动：connect() 返回 True 并挂在等待态
# ---------------------------------------------------------------------------

async def test_connect_succeeds_without_credentials_and_waits(
    empty_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没凭据也要能启动 —— 否则「先起网关、后登录」必须重启网关。"""
    _P, _M, _O, mod = _imports()
    adapter = mod.LivisAdapter(_P(enabled=True, extra={}))
    adapter._credential_poll_seconds = 0.05

    assert await adapter.connect() is True, "缺凭据不该让 connect 失败"
    await asyncio.sleep(0.2)
    assert adapter._waiting_for_credentials is True
    assert adapter._ws is None
    await adapter.disconnect()


async def test_missing_dependency_still_fails_connect(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺 Python 依赖不会自己恢复，仍然要干脆失败。"""
    _P, _M, _O, mod = _imports()
    adapter = mod.LivisAdapter(_P(enabled=True, extra={}))

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def fake_import(name, *args, **kwargs):
        if name == "websockets":
            raise ImportError("no websockets")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert await adapter.connect() is False


async def test_credentials_appearing_later_are_picked_up(
    empty_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登录写入凭据后，等待循环自己发现并退出等待态。"""
    _P, _M, _O, mod = _imports()
    adapter = mod.LivisAdapter(_P(enabled=True, extra={}))
    adapter._credential_poll_seconds = 0.05
    assert adapter._load_identity() is False

    empty_state_dir.mkdir(parents=True, exist_ok=True)
    (empty_state_dir / "tokens.json").write_text(
        json.dumps({"relay_refresh_token": "rt-late"}), encoding="utf-8"
    )
    (empty_state_dir / "agent.id").write_text("openclaw-late", encoding="utf-8")

    assert adapter._load_identity() is True
    assert adapter._agent_id == "openclaw-late"

    adapter._running = True
    adapter._waiting_for_credentials = True
    assert await adapter._wait_for_credentials() is True
    assert adapter._waiting_for_credentials is False, "凭据到了要退出等待态"
    assert adapter._reconnect_attempts == 0, "新凭据不该继承之前的退避计数"


async def test_wait_loop_stops_when_adapter_shuts_down(
    empty_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _P, _M, _O, mod = _imports()
    adapter = mod.LivisAdapter(_P(enabled=True, extra={}))
    adapter._credential_poll_seconds = 0.05
    adapter._running = False
    assert await adapter._wait_for_credentials() is False


def test_enablement_allows_login_after_gateway_start(
    empty_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无凭据 + LIVIS_ENABLED=true ⇒ 启用（适配器等待登录）。"""
    _P, _M, _O, mod = _imports()
    monkeypatch.setenv("LIVIS_ENABLED", "true")
    assert mod.check_requirements() is True
    assert mod.is_connected(None) is True
    assert mod.env_enablement() is not None


def test_enablement_stays_off_for_unconfigured_users(
    empty_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无凭据且没显式开 ⇒ 不启用，别打扰从没配过这条渠道的人。"""
    _P, _M, _O, mod = _imports()
    monkeypatch.delenv("LIVIS_ENABLED", raising=False)
    assert mod.check_requirements() is False
    assert mod.env_enablement() is None


def test_explicit_disable_beats_credentials(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _P, _M, _O, mod = _imports()
    monkeypatch.setenv("LIVIS_ENABLED", "false")
    assert mod.check_requirements() is False
    assert mod.env_enablement() is None


def test_env_enablement_never_mints_an_agent_id(
    empty_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置加载会在只读场景下被调用，凭空造一个未绑定的 agent_id 只会让人困惑。"""
    _P, _M, _O, mod = _imports()
    monkeypatch.setenv("LIVIS_ENABLED", "true")
    seed = mod.env_enablement()
    assert seed is not None
    assert "agent_id" not in seed
    assert not (empty_state_dir / "agent.id").exists()
