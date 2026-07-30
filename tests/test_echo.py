"""回声联调模式测试。

回声模式跑的是**真实**投递管线（``handle_message`` → 后台任务 → ``send()`` →
``on_processing_complete`` 收口 → ``send_result``），只把调模型那一步换成桩。
这里用假中继验证：眼镜发一句 → 中继收到「你好啊 #N」，且序列号递增。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import requires_hermes

pytestmark = requires_hermes

pytest.importorskip("websockets")
pytest.importorskip("aiohttp")

from hermes_livis import echo as echo_mod  # noqa: E402


class ScriptedRelay:
    """按脚本连发 N 条 exec，并收集 send_result。"""

    def __init__(self, prompts: list[str]) -> None:
        self.prompts = prompts
        self.results: list[dict[str, Any]] = []
        self._server: Any = None
        self.port = 0
        self.done = asyncio.Event()

    async def start(self) -> None:
        import websockets

        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/v1/ws"

    async def _handler(self, ws: Any) -> None:
        await ws.recv()  # 握手
        await ws.send(json.dumps({"type": "connected", "metadata": {}, "payload": {}}))

        async def reader() -> None:
            async for raw in ws:
                frame = json.loads(raw)
                if frame.get("type") != "send_result":
                    continue
                self.results.append(frame)
                job_id = frame["metadata"]["job_id"]
                await ws.send(
                    json.dumps(
                        {
                            "type": "ack_send_result",
                            "metadata": {"job_id": job_id},
                            "payload": {"ref_msg_id": job_id},
                        }
                    )
                )
                if len(self.results) >= len(self.prompts):
                    self.done.set()

        task = asyncio.create_task(reader())
        try:
            for index, prompt in enumerate(self.prompts, start=1):
                await ws.send(
                    json.dumps(
                        {
                            "type": "send_message",
                            "metadata": {"msg_id": f"m{index}", "job_id": f"job-{index}"},
                            "payload": {
                                "from_node_id": "glasses-echo",
                                "data": {"type": "exec", "content": prompt},
                            },
                        }
                    )
                )
                await asyncio.sleep(0.15)
            await asyncio.wait_for(self.done.wait(), timeout=25)
            await asyncio.sleep(0.3)
        finally:
            task.cancel()


class Idaas:
    def __init__(self) -> None:
        self._runner: Any = None
        self.port = 0

    async def start(self) -> None:
        from aiohttp import web

        async def token(request):
            return web.json_response(
                {
                    "rZgT0SETDNueMVAhfRN10": {
                        "access_token": "at-echo",
                        "refresh_token": "rt-test",
                        "expires_in": 3600,
                    }
                }
            )

        app = web.Application()
        app.router.add_post("/token", token)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# ---------------------------------------------------------------------------
# EchoSession（纯逻辑，无需网络）
# ---------------------------------------------------------------------------

class _Event:
    def __init__(self, text: str, node: str = "g1") -> None:
        self.text = text
        self.source = type("S", (), {"chat_id": node})()


async def test_sequence_number_increments() -> None:
    session = echo_mod.EchoSession()
    assert await session.handle(_Event("一")) == "你好啊 #1"
    assert await session.handle(_Event("二")) == "你好啊 #2"
    assert await session.handle(_Event("三")) == "你好啊 #3"
    assert session.seq == 3
    assert [text for _, _, text in session.received] == ["一", "二", "三"]


async def test_custom_template_placeholders() -> None:
    session = echo_mod.EchoSession("收到「{text}」来自 {node}，第 {n} 条")
    assert await session.handle(_Event("天气", "glasses-9")) == (
        "收到「天气」来自 glasses-9，第 1 条"
    )


async def test_unknown_placeholder_does_not_crash() -> None:
    """模板写错不该让联调中断。"""
    session = echo_mod.EchoSession("你好 {不认识的占位符}")
    assert await session.handle(_Event("hi")) == "你好 {不认识的占位符}"


async def test_summary_lists_recent_traffic() -> None:
    session = echo_mod.EchoSession()
    await session.handle(_Event("测试内容"))
    rendered = "\n".join(session.summary())
    assert "共处理 1 条请求" in rendered
    assert "测试内容" in rendered


# ---------------------------------------------------------------------------
# 端到端：真实投递管线 + 假中继
# ---------------------------------------------------------------------------

async def test_echo_roundtrip_through_the_real_pipeline(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """眼镜连发 3 条 → 中继收到 3 条「你好啊 #N」，序列号递增且一一对应。"""
    relay = ScriptedRelay(["你好", "今天天气", "讲个笑话"])
    idaas = Idaas()
    await relay.start()
    await idaas.start()
    monkeypatch.setenv("LIVIS_WS_URL", relay.url)
    monkeypatch.setenv("LIVIS_IDAAS_ENDPOINT", idaas.url)

    try:
        code = await echo_mod.run_echo(duration=6.0)
    finally:
        await relay.stop()
        await idaas.stop()

    assert code == 0
    assert len(relay.results) == 3, f"应收到 3 条结果，实际 {len(relay.results)}"

    texts = [json.loads(r["payload"]["data"])["text"] for r in relay.results]
    assert texts == ["你好啊 #1", "你好啊 #2", "你好啊 #3"]

    job_ids = [r["metadata"]["job_id"] for r in relay.results]
    assert job_ids == ["job-1", "job-2", "job-3"], "回复必须对上各自的 job"


async def test_echo_reports_connect_failure(
    empty_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有凭据时干净失败，不是抛异常。"""
    assert await echo_mod.run_echo(duration=1.0) == 1


def test_bootstrap_finds_hermes() -> None:
    assert echo_mod.bootstrap_hermes()


def test_ensure_platform_registered_is_idempotent() -> None:
    echo_mod.ensure_platform_registered()
    echo_mod.ensure_platform_registered()
    from gateway.platform_registry import platform_registry

    assert platform_registry.is_registered("livis")
