"""连通性探针 —— 把"理想是否接受这个客户端"从其他变量里孤立出来。

本项目唯一无法离线验证的未知是：**理想服务端会不会拒绝非官方客户端**。
如果靠"启动整个网关 + 对眼镜说话"去试，一次失败会牵扯凭据、绑定、agent、
授权、协议等一堆变量。这个探针只做四件事然后立刻断开：

1. 读凭据
2. 用 refresh_token 换 access_token（验证账号与 IDaaS）
3. 建 WebSocket 连接并发送握手帧
4. 等 ``connected``（验证服务端是否接受本客户端）

**运行前请先停掉 hermes gateway** —— 同一个 agent_id 只能有一条活跃连接，
探针会和正在跑的渠道互相顶掉。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from . import protocol
from .auth import LivisAuthError, LivisCredentials
from .constants import PROTOCOL_VERSION, client_name, node_name, ws_url
from .safeio import redact_secret


@dataclass
class ProbeResult:
    ok: bool = False
    steps: list[tuple[str, bool, str]] = field(default_factory=list)
    close_code: int | None = None
    close_reason: str = ""
    verdict: str = ""

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append((name, ok, detail))


async def run_probe(*, timeout: float = 20.0) -> ProbeResult:
    result = ProbeResult()

    # -- 1. 凭据 ---------------------------------------------------------
    creds = LivisCredentials()
    with contextlib.suppress(Exception):
        creds.import_from_openclaw()

    refresh = creds.refresh_token
    agent_id = creds.peek_agent_id()
    if not refresh:
        result.step("凭据", False, "没有 refresh_token")
        result.verdict = "未登录。执行 `hermes-livis login`（或 import-openclaw）。"
        return result
    if not agent_id:
        result.step("凭据", False, "没有 agent_id")
        result.verdict = "缺 agent_id。执行 `hermes-livis login` 生成并在 APP 里绑定。"
        return result
    device_id = creds.device_id
    result.step(
        "凭据", True, f"agent_id={agent_id} device_id={redact_secret(device_id, keep=10)}"
    )

    # -- 2. 换 access_token ----------------------------------------------
    try:
        token = await creds.get_access_token(force=True)
    except LivisAuthError as exc:
        result.step("IDaaS 令牌", False, str(exc))
        result.verdict = (
            "换 access_token 失败 —— 是账号/令牌问题，还没到服务端校验客户端那一步。"
            "重新 `hermes-livis login` 再试。"
        )
        return result
    result.step("IDaaS 令牌", True, redact_secret(token))

    # -- 3/4. 连接 + 握手 -------------------------------------------------
    try:
        import websockets
    except ImportError:
        result.step("WebSocket", False, "缺少 websockets 包")
        result.verdict = "pip install websockets"
        return result

    url = f"{ws_url()}?protocol_version={PROTOCOL_VERSION}"
    try:
        async with websockets.connect(
            url, open_timeout=timeout, close_timeout=5, ping_interval=None
        ) as ws:
            result.step("建立连接", True, url)
            await ws.send(
                protocol.encode(
                    protocol.connect_frame(
                        agent_id=agent_id,
                        device_id=device_id,
                        node_name=node_name(),
                        access_token=token,
                        refresh_token=refresh,
                        client=client_name(),
                    )
                )
            )
            result.step("发送握手", True, f"client={client_name()}")

            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                remaining = deadline - asyncio.get_running_loop().time()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                try:
                    frame = protocol.parse_frame(raw)
                except protocol.ProtocolError as exc:
                    result.step("收到帧", False, f"非法帧: {exc}")
                    continue
                kind = frame.get("type")
                if kind == "connected":
                    result.step("服务端确认", True, "收到 connected")
                    result.ok = True
                    result.verdict = (
                        "✅ 理想中继接受了这个客户端。协议链路通，可以启动网关了。"
                    )
                    return result
                # 握手期间还可能先收到别的帧（如 token_expiring），继续等。
                result.step("收到帧", True, f"type={kind}（继续等 connected）")

            result.step("服务端确认", False, f"{timeout:.0f}s 内没有收到 connected")
            result.verdict = (
                "连接建立了但服务端没确认。可能是握手字段不被接受，"
                "或 agent_id 还没在理想 APP 里绑定到眼镜。先确认绑定，再看是否被静默拒绝。"
            )
            return result

    except Exception as exc:  # noqa: BLE001 —— 这里要把任何失败都翻译成人话
        code = getattr(exc, "code", None) or getattr(exc, "rcvd", None)
        if hasattr(code, "code"):
            result.close_code = int(code.code)
            result.close_reason = str(getattr(code, "reason", "") or "")
        elif isinstance(code, int):
            result.close_code = code
        result.step("建立连接/握手", False, str(exc)[:300])
        result.verdict = _explain_failure(result.close_code, str(exc))
        return result


def _explain_failure(close_code: int | None, message: str) -> str:
    lowered = message.lower()
    if close_code == 1008 or "policy" in lowered:
        return (
            "❌ 服务端以 1008（policy violation）关闭 —— 这是「客户端被拒」的典型信号。"
            "若排除了令牌问题仍然如此，本方案在服务端校验客户端身份的情况下走不通，"
            "只能退回「openclaw 当边车、hermes 当大脑」。"
        )
    if "401" in lowered or "unauthorized" in lowered or "403" in lowered:
        return (
            "❌ 握手被拒（401/403）。先 `hermes-livis logout && hermes-livis login` "
            "排除令牌问题；仍然被拒就是服务端在校验客户端身份。"
        )
    if close_code in {1000, 1001}:
        return (
            f"⚠️ 服务端「干净关闭」（code {close_code}）。这也可能是策略性拒绝 —— "
            "干净关闭不带错误信息，需要结合是否已在 APP 绑定来判断。"
        )
    if "timed out" in lowered or "timeout" in lowered:
        return (
            "❌ 连接超时。检查服务器出网（wss 443）、DNS，以及是否需要走代理。"
        )
    if "name or service not known" in lowered or "nodename nor servname" in lowered:
        return "❌ DNS 解析失败。检查服务器 DNS 与 /etc/resolv.conf。"
    return (
        "❌ 连接失败。若反复出现，检查是否有另一条连接占着同一个 agent_id"
        "（hermes gateway 正在跑、或 openclaw 的 livis-pc-kit 渠道没停）。"
    )


def format_result(result: ProbeResult) -> list[str]:
    lines = ["", "理想中继连通性探针", "─" * 46]
    for name, ok, detail in result.steps:
        mark = "✅" if ok else "❌"
        lines.append(f"  {mark} {name:<12} {detail}")
    if result.close_code is not None:
        lines.append(f"     关闭码       {result.close_code} {result.close_reason}")
    lines.extend(["", f"  {result.verdict}", ""])
    if not result.ok:
        lines.extend(
            [
                "  排查清单：",
                "    1. hermes gateway 是否已停？同一 agent_id 只能有一条连接。",
                "    2. openclaw 的 livis-pc-kit 渠道是否已停？",
                "    3. Agent ID 是否已在理想 APP 里绑定到眼镜？",
                "       （`hermes-livis status` 看当前值）",
                "",
            ]
        )
    return lines
