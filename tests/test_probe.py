"""连通性探针测试：接受 / 被拒(1008) / 未登录 / 超时。

探针是排查「理想是否接受这个客户端」的唯一工具，它的**判词**必须准确 ——
把 1008 说成网络问题会让人查错方向好几个小时。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("websockets")
pytest.importorskip("aiohttp")

from hermes_livis.plugin import probe as probe_mod  # noqa: E402


class Relay:
    """按脚本回应握手的最小中继。"""

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self.handshakes: list[dict[str, Any]] = []
        self._server: Any = None
        self.port = 0

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
        raw = await ws.recv()
        self.handshakes.append(json.loads(raw))
        if self.behaviour == "accept":
            await ws.send(json.dumps({"type": "connected", "metadata": {}, "payload": {}}))
            await asyncio.sleep(1)
        elif self.behaviour == "reject_1008":
            await ws.close(code=1008, reason="client not allowed")
        elif self.behaviour == "clean_close":
            await ws.close(code=1000, reason="bye")
        elif self.behaviour == "silent":
            await asyncio.sleep(2)


class Idaas:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self._runner: Any = None
        self.port = 0

    async def start(self) -> None:
        from aiohttp import web

        async def token(request):
            if self.fail:
                return web.json_response({"error": "invalid_grant"}, status=401)
            return web.json_response(
                {
                    "rZgT0SETDNueMVAhfRN10": {
                        "access_token": "at-probe",
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


async def _run(
    behaviour: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    idaas_fails: bool = False,
    timeout: float = 3.0,
) -> probe_mod.ProbeResult:
    relay = Relay(behaviour)
    idaas = Idaas(fail=idaas_fails)
    await relay.start()
    await idaas.start()
    monkeypatch.setenv("LIVIS_WS_URL", relay.url)
    monkeypatch.setenv("LIVIS_IDAAS_ENDPOINT", idaas.url)
    try:
        result = await probe_mod.run_probe(timeout=timeout)
        result.handshakes = relay.handshakes  # type: ignore[attr-defined]
        return result
    finally:
        await relay.stop()
        await idaas.stop()


def _step(result: probe_mod.ProbeResult, name: str) -> tuple[str, bool, str] | None:
    return next((s for s in result.steps if s[0] == name), None)


async def test_accept_reports_success(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _run("accept", monkeypatch)
    assert result.ok is True
    assert "接受了这个客户端" in result.verdict
    assert _step(result, "服务端确认")[1] is True

    # 探针发出的握手必须和适配器一样是裸帧
    handshake = result.handshakes[0]  # type: ignore[attr-defined]
    assert handshake["type"] == "connect"
    assert set(handshake["metadata"]) == {"msg_id", "job_id", "agent_id", "timestamp"}
    assert "nodeType" not in handshake["payload"]
    assert handshake["payload"]["token"] == "at-probe"


async def test_1008_is_diagnosed_as_client_rejection(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """最关键的一条：1008 必须被说成「客户端被拒」，不能含糊成网络问题。"""
    result = await _run("reject_1008", monkeypatch)
    assert result.ok is False
    assert result.close_code == 1008
    assert "客户端被拒" in result.verdict
    assert "openclaw 当边车" in result.verdict


async def test_clean_close_is_flagged_as_possible_rejection(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _run("clean_close", monkeypatch)
    assert result.ok is False
    assert result.close_code in {1000, None}
    assert "干净关闭" in result.verdict or "没有收到 connected" in result.verdict


async def test_silent_server_times_out_with_binding_hint(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _run("silent", monkeypatch, timeout=1.0)
    assert result.ok is False
    assert "没有收到 connected" in _step(result, "服务端确认")[2]
    assert "绑定" in result.verdict


async def test_bad_token_stops_before_the_relay(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """令牌就失败时要说清楚"还没到服务端校验客户端那一步"。"""
    result = await _run("accept", monkeypatch, idaas_fails=True)
    assert result.ok is False
    assert _step(result, "IDaaS 令牌")[1] is False
    assert "还没到服务端校验客户端" in result.verdict
    assert _step(result, "建立连接") is None, "令牌失败就不该再去连中继"


async def test_missing_credentials_short_circuits(
    empty_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await probe_mod.run_probe(timeout=1.0)
    assert result.ok is False
    assert "未登录" in result.verdict


async def test_probe_never_leaks_the_token(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _run("accept", monkeypatch)
    rendered = "\n".join(probe_mod.format_result(result))
    assert "at-probe" not in rendered
    assert "rt-test" not in rendered
