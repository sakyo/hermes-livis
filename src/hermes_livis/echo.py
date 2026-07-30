"""联调回声模式 —— 绕过 Hermes 生命周期，直接跑适配器。

用途：把「理想眼镜 ↔ 中继 ↔ 适配器」这条链路从整个 Hermes agent 栈里剥离出来
单独验证。不启动 gateway、不需要 LLM、不产生任何模型开销。

**它跑的仍然是真实代码路径**：``adapter.handle_message(event)`` 会走 Hermes
完整的投递管线（会话键 → 后台任务 → ``send()`` → ``on_processing_complete``
收口 → ``send_result`` → 等 ack），只是把「调用模型」那一步换成了一个桩函数，
返回 ``你好啊 #<序列号>``。所以链路上除了模型本身，其余都被真正验证到。

    hermes-livis echo                     # 一直跑到 Ctrl-C
    hermes-livis echo --duration 120      # 跑 2 分钟自动退出
    hermes-livis echo --reply "收到 {n}"   # 自定义回复模板

运行前请先停掉 hermes gateway —— 同一个 agent_id 只能有一条活跃连接。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_REPLY = "你好啊 #{n}"


def bootstrap_hermes() -> str:
    """让 ``gateway.*`` 可导入。

    解析顺序：已能导入 → ``HERMES_REPO`` → 与本仓库同级的 ``hermes-agent``。
    """
    try:
        import gateway.platforms.base  # noqa: F401

        return "<已在 sys.path 中>"
    except Exception:
        pass

    candidates: list[Path] = []
    env_repo = os.getenv("HERMES_REPO", "").strip()
    if env_repo:
        candidates.append(Path(env_repo).expanduser())
    candidates.append(Path(__file__).resolve().parents[2].parent / "hermes-agent")

    for candidate in candidates:
        if (candidate / "gateway" / "platforms" / "base.py").is_file():
            sys.path.insert(0, str(candidate))
            try:
                import gateway.platforms.base  # noqa: F401

                return str(candidate)
            except Exception:
                sys.path.pop(0)

    raise RuntimeError(
        "找不到可导入的 hermes-agent。在装有 hermes 的虚拟环境里运行，"
        "或设 HERMES_REPO=/path/to/hermes-agent。"
    )


def ensure_platform_registered() -> None:
    """让 ``Platform("livis")`` 可解析。

    Hermes 的 ``Platform`` 是动态枚举：插件平台名要么在仓库内置的
    ``plugins/platforms/`` 里，要么已登记进 ``platform_registry``。回声模式绕过
    了插件加载，所以这里补登记一次 —— 复刻生产环境「先注册、再实例化」的顺序。
    """
    from gateway.platform_registry import PlatformEntry, platform_registry

    if platform_registry.is_registered("livis"):
        return
    platform_registry.register(
        PlatformEntry(
            name="livis",
            label="理想眼镜 (Livis)",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
            source="plugin",
        )
    )


def configure_logging(verbose: bool) -> None:
    """把 ``[livis]`` 的日志直接打到终端，联调时肉眼可读。"""
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-5s %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    for name in ("hermes_livis", "gateway.platforms", "livis_platform"):
        logging.getLogger(name).setLevel(level)


class EchoSession:
    """回声联调会话：统计 + 桩回复。"""

    def __init__(self, reply_template: str = DEFAULT_REPLY) -> None:
        self.reply_template = reply_template
        self.seq = 0
        self.received: list[tuple[float, str, str]] = []
        self.started_at = time.time()

    async def handle(self, event: Any) -> str:
        """桩 agent：不调模型，直接回「你好啊 #N」。"""
        self.seq += 1
        text = str(getattr(event, "text", "") or "")
        node = getattr(getattr(event, "source", None), "chat_id", "?")
        self.received.append((time.time(), str(node), text))

        print(
            f"\n  ◀ 收到 #{self.seq}  来自 {node}\n"
            f"    内容: {text}\n",
            flush=True,
        )
        try:
            reply = self.reply_template.format(
                n=self.seq, text=text, node=node, seq=self.seq
            )
        except (KeyError, IndexError):
            # 模板里有不认识的占位符时不要崩，原样回。
            reply = self.reply_template
        print(f"  ▶ 回复 #{self.seq}: {reply}\n", flush=True)
        return reply

    def summary(self) -> list[str]:
        elapsed = time.time() - self.started_at
        lines = [
            "",
            "─" * 46,
            f"  运行 {elapsed:.0f}s，共处理 {self.seq} 条请求",
        ]
        for stamp, node, text in self.received[-10:]:
            when = time.strftime("%H:%M:%S", time.localtime(stamp))
            preview = text[:40].replace("\n", " ")
            lines.append(f"    {when}  {node}  {preview}")
        lines.append("")
        return lines


async def run_echo(
    *,
    reply_template: str = DEFAULT_REPLY,
    duration: float = 0.0,
    verbose: bool = False,
) -> int:
    """连上中继，用桩 agent 回声，直到 ``duration`` 到期或被 Ctrl-C。"""
    repo = bootstrap_hermes()
    ensure_platform_registered()
    configure_logging(verbose)

    from gateway.config import PlatformConfig

    from .plugin.adapter import LivisAdapter
    from .plugin.auth import LivisCredentials

    creds = LivisCredentials()
    with contextlib.suppress(Exception):
        creds.import_from_openclaw()

    print("")
    print("理想眼镜 · 回声联调模式（绕过 Hermes 生命周期）")
    print("─" * 46)
    print(f"  hermes 源     : {repo}")
    print(f"  状态目录      : {creds.directory}")
    print(f"  agent_id      : {creds.peek_agent_id() or '<未生成>'}")
    print(f"  回复模板      : {reply_template}")
    if duration > 0:
        print(f"  运行时长      : {duration:.0f}s")
    else:
        print("  运行时长      : 直到 Ctrl-C")
    print("─" * 46)
    print("  提示：先确认 hermes gateway 已停 —— 同一 agent_id 只能一条连接。")
    print("")

    session = EchoSession(reply_template)
    adapter = LivisAdapter(PlatformConfig(enabled=True, extra={}))
    adapter.set_message_handler(session.handle)

    if not await adapter.connect():
        print("\n❌ 连接失败。用 `hermes-livis probe` 看具体卡在哪一步。\n")
        return 1

    print("  ✅ 适配器已启动，等待眼镜发话……（Ctrl-C 退出）\n", flush=True)

    try:
        if duration > 0:
            await asyncio.sleep(duration)
        else:
            # 一直挂着，直到 KeyboardInterrupt。
            while True:
                await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n  收到退出信号，正在断开……", flush=True)
    finally:
        await adapter.disconnect()

    for line in session.summary():
        print(line)
    return 0


def main(
    *,
    reply_template: str = DEFAULT_REPLY,
    duration: float = 0.0,
    verbose: bool = False,
) -> int:
    try:
        return asyncio.run(
            run_echo(
                reply_template=reply_template, duration=duration, verbose=verbose
            )
        )
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
