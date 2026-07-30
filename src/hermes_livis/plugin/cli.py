"""``hermes livis <子命令>`` —— 登录 / 状态 / 登出 / 导入 / 重置 Agent ID。

同一套实现被两个入口复用：
* 插件注册后的 ``hermes livis ...``（见 :func:`register_cli` / :func:`dispatch`）
* 发行包自带的 ``hermes-livis ...``（安装插件之前也能用）

服务器友好：默认**不**尝试打开浏览器，只把授权链接打印出来。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import Any

from .auth import (
    LivisAuthError,
    LivisCredentials,
    browser_logout_url,
    poll_for_token,
    request_device_code,
    revoke,
)
from .constants import (
    OPENCLAW_TOKENS_FILE,
    PLUGIN_VERSION,
    REFERENCE_PLUGIN_VERSION,
    idaas_endpoint,
    ws_url,
)
from .safeio import redact_secret
from .store import PendingResultStore

Printer = Callable[[str], None]


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

def cmd_status(*, as_json: bool = False, out: Printer = print) -> int:
    """只读：不生成 agent_id、不写任何文件。"""
    creds = LivisCredentials()
    store_path = creds.directory / "pending_results.json"
    pending = {"pending": 0, "completed": 0}
    if store_path.exists():
        pending = PendingResultStore(store_path).snapshot()

    info = {
        "plugin_version": PLUGIN_VERSION,
        "reference_plugin_version": REFERENCE_PLUGIN_VERSION,
        "state_dir": str(creds.directory),
        "authenticated": bool(creds.refresh_token),
        "agent_id": creds.peek_agent_id() or "",
        "device_id_present": (creds.directory / "device.id").exists(),
        "idaas": idaas_endpoint(),
        "relay": ws_url(),
        "pending_results": pending["pending"],
        "completed_results": pending["completed"],
        "ready": creds.is_configured(),
    }

    if as_json:
        import json

        out(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    out("")
    out("理想眼镜 (Livis) 渠道状态")
    out("─" * 46)
    out(f"  插件版本     : {info['plugin_version']} (对齐官方 {REFERENCE_PLUGIN_VERSION})")
    out(f"  凭据目录     : {info['state_dir']}")
    out(f"  refresh_token: {redact_secret(creds.refresh_token)}")
    out(f"  agent_id     : {info['agent_id'] or '<未生成>'}")
    out(f"  device.id    : {'已生成' if info['device_id_present'] else '<未生成>'}")
    out(f"  IDaaS        : {info['idaas']}")
    out(f"  中继          : {info['relay']}")
    out(f"  待确认结果   : {info['pending_results']}（已完成 {info['completed_results']}）")
    out("")
    if info["ready"]:
        out("  ✅ 已就绪。启动网关即可：hermes gateway start")
    else:
        out("  ❌ 未就绪。执行 `hermes-livis login` 完成登录。")
    out("")
    return 0


def cmd_login(
    *, force: bool = False, open_browser: bool = False, out: Printer = print
) -> int:
    creds = LivisCredentials()
    try:
        code = request_device_code(force=force)
    except LivisAuthError as exc:
        out(f"\n❌ 请求设备码失败：{exc}\n")
        return 1

    out("")
    out("请在浏览器（手机也可以）中打开下面的链接完成登录：")
    out("")
    out(f"    {code.verification_uri}")
    out("")
    if code.user_code:
        out(f"    如页面要求输入验证码：{code.user_code}")
        out("")
    out("登录方式：手机号 + 短信验证码")
    out(f"等待授权中（{max(1, code.expires_in // 60)} 分钟内有效）……")
    out("")

    if open_browser and code.verification_uri:
        try:
            import webbrowser

            webbrowser.open(code.verification_uri)
        except Exception:
            pass

    def _tick() -> None:
        sys.stdout.write(".")
        sys.stdout.flush()

    try:
        poll_for_token(creds, code, on_pending=_tick)
    except LivisAuthError as exc:
        out(f"\n\n❌ 授权失败：{exc}\n")
        return 1

    out("")
    out(f"✅ 登录成功，凭据已写入 {creds.tokens_file}")
    _print_binding(creds.ensure_agent_id(), out=out)
    return 0


def cmd_logout(
    *, local_only: bool = False, show_browser_url: bool = False, out: Printer = print
) -> int:
    creds = LivisCredentials()
    if not creds.refresh_token:
        out("ℹ️  当前未登录，无需登出。")
        return 0

    if local_only:
        creds.clear_tokens()
        out("✅ 本地凭据已清除（服务端 refresh_token 未撤销）。")
    else:
        revoked = revoke(creds)
        if revoked:
            out("✅ 已在理想 IDaaS 撤销 refresh_token，本地凭据已清除。")
        else:
            out(
                "⚠️  服务端撤销未成功（可能令牌已过期），"
                "本地凭据仍已清除。"
            )

    out("")
    out("  注意：agent_id 与 device.id 保留着 —— 重新登录同一账号即可继续使用，")
    out("  眼镜端不需要重新绑定。要彻底重置请执行 `hermes-livis reset-agent-id`。")
    if show_browser_url:
        out("")
        out("  浏览器侧登出（换账号时用）：")
        out(f"    {browser_logout_url()}")
    out("")
    return 0


def cmd_import_openclaw(*, out: Printer = print) -> int:
    creds = LivisCredentials()
    imported = creds.import_from_openclaw()
    if imported:
        out(f"✅ 已从 ~/.openclaw/ 导入：{', '.join(imported)}")
        agent = creds.peek_agent_id()
        if agent:
            out(f"   agent_id = {agent}（眼镜端不需要重新绑定）")
    elif not OPENCLAW_TOKENS_FILE.exists():
        out("ℹ️  没找到 ~/.openclaw/livis-pc-kit-tokens.json —— 这台机器没装过官方 kit。")
    else:
        out("ℹ️  没有可导入的内容（hermes 侧已有凭据）。")
    return 0


def cmd_probe(*, timeout: float = 20.0, out: Printer = print) -> int:
    """连一次真实中继、发握手、等 ``connected``，然后立刻断开。

    把「理想是否接受这个客户端」从凭据 / 绑定 / agent / 授权等变量里孤立出来。
    **运行前先停掉 hermes gateway** —— 同一 agent_id 只能有一条活跃连接。
    """
    import asyncio

    from .probe import format_result, run_probe

    result = asyncio.run(run_probe(timeout=timeout))
    for line in format_result(result):
        out(line)
    return 0 if result.ok else 1


def cmd_reset_agent_id(*, out: Printer = print) -> int:
    creds = LivisCredentials()
    fresh = creds.reset_agent_id()
    out(f"✅ 新 agent_id 已写入：{fresh}")
    _print_binding(fresh, out=out)
    return 0


def _print_binding(agent_id: str, *, out: Printer = print) -> None:
    out("")
    out("─" * 46)
    out("下一步：在理想 APP 里把这台机器绑定到眼镜")
    out("")
    out(f"    Agent ID:  {agent_id}")
    out("")
    out("  1. 打开理想 APP → 眼镜 → 设备/电脑绑定")
    out("  2. 输入上面这串 Agent ID 完成绑定")
    out("  3. 回到服务器启动网关：hermes gateway start")
    out("")
    out("  注意：同一个 Agent ID 只能有一条活跃连接。若这台机器上还跑着")
    out("  openclaw 的 livis-pc-kit 渠道，请先停掉它，否则两边会互相顶掉。")
    out("─" * 46)
    out("")


# ---------------------------------------------------------------------------
# argparse 接线（两个入口共用）
# ---------------------------------------------------------------------------

def build_subcommands(parser: argparse.ArgumentParser) -> None:
    """在一个新的 subparsers 上挂子命令（``hermes livis ...`` 用）。"""
    build_subcommands_into(parser.add_subparsers(dest="livis_command"))


def build_subcommands_into(subs: Any) -> None:
    """把子命令挂到已有的 subparsers 上（``hermes-livis ...`` 用）。"""
    login = subs.add_parser("login", help="登录理想账号（OAuth2 设备码）")
    login.add_argument(
        "--force", action="store_true", help="强制重新选择账号（prompt=login）"
    )
    login.add_argument(
        "--open-browser", action="store_true",
        help="尝试在本机打开浏览器（服务器上通常不需要）",
    )

    logout = subs.add_parser("logout", help="登出并在服务端撤销 refresh_token")
    logout.add_argument(
        "--local-only", action="store_true", help="只清本地，不调用服务端 /revoke"
    )
    logout.add_argument(
        "--show-browser-url", action="store_true", help="打印浏览器侧登出链接"
    )

    status = subs.add_parser("status", help="查看凭据与投递状态（只读）")
    status.add_argument("--json", action="store_true", dest="as_json")

    probe = subs.add_parser(
        "probe", help="连一次真实中继验证握手是否被接受（先停掉 gateway）"
    )
    probe.add_argument(
        "--timeout", type=float, default=20.0, help="等待 connected 的秒数（默认 20）"
    )

    subs.add_parser("import-openclaw", help="从 ~/.openclaw/ 导入已有凭据")
    subs.add_parser("reset-agent-id", help="重置 Agent ID（需在 APP 里重新绑定）")


def run_subcommand(args: argparse.Namespace, *, out: Printer = print) -> int:
    command = getattr(args, "livis_command", None) or "status"
    if command == "login":
        return cmd_login(
            force=bool(getattr(args, "force", False)),
            open_browser=bool(getattr(args, "open_browser", False)),
            out=out,
        )
    if command == "logout":
        return cmd_logout(
            local_only=bool(getattr(args, "local_only", False)),
            show_browser_url=bool(getattr(args, "show_browser_url", False)),
            out=out,
        )
    if command == "probe":
        return cmd_probe(timeout=float(getattr(args, "timeout", 20.0)), out=out)
    if command == "import-openclaw":
        return cmd_import_openclaw(out=out)
    if command == "reset-agent-id":
        return cmd_reset_agent_id(out=out)
    return cmd_status(as_json=bool(getattr(args, "as_json", False)), out=out)


# -- hermes 插件 CLI 钩子 ----------------------------------------------------

def register_cli(parser: argparse.ArgumentParser) -> None:
    """``ctx.register_cli_command(setup_fn=...)``"""
    build_subcommands(parser)


def dispatch(args: argparse.Namespace) -> int:
    """``ctx.register_cli_command(handler_fn=...)``"""
    return run_subcommand(args)


def interactive_setup() -> None:
    """``hermes gateway setup`` → 理想眼镜。"""
    creds = LivisCredentials()
    if creds.is_configured():
        cmd_status()
        print("已配置。要换账号请执行：hermes-livis logout && hermes-livis login")
        return
    if not creds.refresh_token and OPENCLAW_TOKENS_FILE.exists():
        print("检测到 openclaw 的 livis-pc-kit 凭据，正在导入……")
        cmd_import_openclaw()
        if LivisCredentials().is_configured():
            cmd_status()
            return
    cmd_login()


__all__ = [
    "build_subcommands",
    "build_subcommands_into",
    "cmd_import_openclaw",
    "cmd_login",
    "cmd_logout",
    "cmd_probe",
    "cmd_reset_agent_id",
    "cmd_status",
    "dispatch",
    "interactive_setup",
    "register_cli",
    "run_subcommand",
]
