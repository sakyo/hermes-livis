"""假中继端到端：真开 WebSocket 服务器 + 假 IDaaS，跑完整往返。

比单测多覆盖的部分：真实的 ``websockets.connect`` 握手、``async for`` 读循环、
心跳任务、重连补发、以及"服务端干净关闭必须退避"这条。
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


class FakeRelay:
    """最小中继：接握手 → 回 connected → 按脚本推消息。"""

    def __init__(self) -> None:
        self.handshakes: list[dict[str, Any]] = []
        self.inbound: list[dict[str, Any]] = []
        self.connections = 0
        self._server: Any = None
        self.port = 0
        self.script: Any = None
        self.closed_cleanly = asyncio.Event()

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
        self.connections += 1
        conn = self.connections
        raw = await ws.recv()
        self.handshakes.append(json.loads(raw))
        await ws.send(json.dumps({"type": "connected", "metadata": {}, "payload": {}}))

        reader = asyncio.create_task(self._read(ws))
        try:
            if self.script is not None:
                await self.script(self, ws, conn)
            else:
                await asyncio.sleep(3)
        finally:
            reader.cancel()

    async def _read(self, ws: Any) -> None:
        async for raw in ws:
            self.inbound.append(json.loads(raw))

    def by_type(self, kind: str) -> list[dict[str, Any]]:
        return [f for f in self.inbound if f.get("type") == kind]

    async def wait_for(self, kind: str, timeout: float = 10.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            found = self.by_type(kind)
            if found:
                return found[-1]
            await asyncio.sleep(0.02)
        raise AssertionError(f"超时未收到 {kind}；已收到 {[f['type'] for f in self.inbound]}")


class FakeIdaas:
    """只实现 POST /token 的最小 IDaaS（返回理想的多受众嵌套结构）。"""

    def __init__(self) -> None:
        self.calls = 0
        self._runner: Any = None
        self.port = 0

    async def start(self) -> None:
        from aiohttp import web

        async def token(request):
            self.calls += 1
            return web.json_response(
                {
                    "rZgT0SETDNueMVAhfRN10": {
                        "access_token": f"at-{self.calls}",
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


@pytest.fixture()
async def stack(state_dir: Path, monkeypatch: pytest.MonkeyPatch):
    relay = FakeRelay()
    idaas = FakeIdaas()
    await relay.start()
    await idaas.start()
    monkeypatch.setenv("LIVIS_WS_URL", relay.url)
    monkeypatch.setenv("LIVIS_IDAAS_ENDPOINT", idaas.url)
    monkeypatch.setenv("LIVIS_RESULT_FALLBACK_MS", "80")
    try:
        yield relay, idaas
    finally:
        await relay.stop()
        await idaas.stop()


def _make_adapter():
    from gateway.config import PlatformConfig

    from hermes_livis.plugin.adapter import LivisAdapter

    return LivisAdapter(PlatformConfig(enabled=True, extra={}))


async def test_full_roundtrip(stack) -> None:
    relay, _idaas = stack

    async def script(relay: FakeRelay, ws: Any, conn: int) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "send_message",
                    "metadata": {"msg_id": "m1", "job_id": "job-1"},
                    "payload": {
                        "from_node_id": "glasses-1",
                        "data": json.dumps({"type": "exec", "content": "北京天气"}),
                    },
                }
            )
        )
        result = await relay.wait_for("send_result")
        await ws.send(
            json.dumps(
                {
                    "type": "ack_send_result",
                    "metadata": {"job_id": result["metadata"]["job_id"]},
                    "payload": {"ref_msg_id": result["metadata"]["job_id"]},
                }
            )
        )
        await asyncio.sleep(0.5)

    relay.script = script

    adapter = _make_adapter()

    async def handler(event):
        assert event.text == "北京天气"
        await asyncio.sleep(0.05)
        return "北京今天晴，最高 31 度。"

    adapter.set_message_handler(handler)
    assert await adapter.connect() is True
    try:
        await relay.wait_for("send_result", timeout=15)
        await asyncio.sleep(0.3)
    finally:
        await adapter.disconnect()

    # 握手逐字对齐上游：不带 nodeType / metadata.client / metadata.device_id
    handshake = relay.handshakes[0]
    assert handshake["type"] == "connect"
    assert set(handshake["metadata"]) == {"msg_id", "job_id", "agent_id", "timestamp"}
    assert "nodeType" not in handshake["payload"]
    assert handshake["payload"]["client"] == "openclaw"
    assert handshake["payload"]["token"] == "at-1"

    # 先 ack 再处理
    assert relay.by_type("ack_send_message")[0]["metadata"]["job_id"] == "job-1"

    results = relay.by_type("send_result")
    assert len(results) == 1
    body = json.loads(results[0]["payload"]["data"])
    assert body == {"text": "北京今天晴，最高 31 度。"}
    assert adapter._store.is_completed("job-1") is True


async def test_clean_server_close_still_backs_off(stack, monkeypatch) -> None:
    """服务端干净关闭（code 1000）时 ``async for`` 不抛异常。

    若只在异常分支退避，"服务端策略性拒绝"就会变成对中继的高频重连风暴。
    这条断言退避确实发生了。
    """
    relay, _idaas = stack

    async def script(relay: FakeRelay, ws: Any, conn: int) -> None:
        await ws.close(code=1000, reason="bye")

    relay.script = script

    async def handler(event):
        return None

    adapter = _make_adapter()
    adapter.set_message_handler(handler)
    assert await adapter.connect() is True
    try:
        await asyncio.sleep(1.2)
    finally:
        await adapter.disconnect()

    # 退避下限 1s：1.2 秒内最多再连一次，绝不该是紧密循环。
    assert relay.connections <= 2, f"疑似无退避紧密重连：{relay.connections} 次连接"
    assert adapter._reconnect_attempts >= 1, "干净关闭也必须计入退避"


async def test_pending_result_is_redelivered_after_reconnect(stack) -> None:
    """第一次连接不 ack 就断开，重连后必须补发同一条结果。"""
    relay, _idaas = stack

    async def script(relay: FakeRelay, ws: Any, conn: int) -> None:
        if conn == 1:
            await ws.send(
                json.dumps(
                    {
                        "type": "send_message",
                        "metadata": {"msg_id": "m", "job_id": "job-lost"},
                        "payload": {
                            "from_node_id": "g",
                            "data": {"type": "exec", "content": "会丢的那条"},
                        },
                    }
                )
            )
            await relay.wait_for("send_result")
            await ws.close(code=1011, reason="oops")  # 不 ack 直接断
        else:
            await asyncio.sleep(3)

    relay.script = script

    async def handler(event):
        return "答案"

    adapter = _make_adapter()
    adapter.set_message_handler(handler)
    assert await adapter.connect() is True
    try:
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            if len(relay.by_type("send_result")) >= 2:
                break
            await asyncio.sleep(0.05)
    finally:
        await adapter.disconnect()

    results = relay.by_type("send_result")
    assert len(results) >= 2, "重连后应补发未确认的结果"
    bodies = {json.loads(r["payload"]["data"])["text"] for r in results}
    assert bodies == {"答案"}
    assert relay.connections >= 2


async def test_connect_fails_cleanly_without_credentials(
    empty_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _make_adapter()
    assert await adapter.connect() is False
