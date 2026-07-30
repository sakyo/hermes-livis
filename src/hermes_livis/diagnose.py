"""「为什么显示离线」的自动诊断。

把排查这件事从「grep 日志 + 肉眼对号入座」变成一条命令。判据全部来自网关日志里
``[livis]`` 前缀的行 —— 它们的**出现与否**本身就是最强的信号：

* 一行都没有   ⇒ 插件没加载，或平台没被启用（连 ``connect()`` 都没被调用）
* 「等待登录中」⇒ 网关看到的状态目录里没有凭据（多半和 CLI 不是同一个目录）
* 反复重连     ⇒ 连不上或被顶掉（同一 agent_id 有第二条连接是最常见原因）
* 「中继已确认连接」⇒ 连上了，「离线」是别的问题
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .installer import resolve_paths
from .installer import status as install_status

# 网关日志。hermes 把 stdout/stderr 分别重定向到这两个文件
# （hermes_cli/gateway.py: log_dir / "gateway.log" / "gateway.error.log"）。
LOG_NAMES = ("gateway.log", "gateway.error.log")

_LIVIS_LINE = re.compile(r"\[livis\]|livis_livis|hermes_livis|LivisAdapter")


@dataclass
class Diagnosis:
    log_path: Path | None = None
    log_exists: bool = False
    livis_lines: list[str] = field(default_factory=list)
    verdict: str = ""
    hints: list[str] = field(default_factory=list)
    ok: bool = False


def log_paths(home: Path | None = None) -> list[Path]:
    base = resolve_paths(home).hermes_home
    return [base / "logs" / name for name in LOG_NAMES]


def read_livis_lines(
    home: Path | None = None, *, limit: int = 60, only_livis: bool = True
) -> tuple[Path | None, list[str]]:
    """从网关日志里取最近的 livis 相关行。

    日志可能很大，只读尾部：按行倒着收集够数就停，避免把几百 MB 读进内存。
    """
    found_path: Path | None = None
    collected: list[str] = []
    for path in log_paths(home):
        if not path.is_file():
            continue
        found_path = found_path or path
        try:
            # 尾部 2 MB 足够覆盖最近几次启动，也不会读爆内存。
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > 2 * 1024 * 1024:
                    handle.seek(size - 2 * 1024 * 1024)
                    handle.readline()  # 丢掉可能被截断的半行
                raw = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in raw.splitlines():
            if not only_livis or _LIVIS_LINE.search(line):
                collected.append(line.rstrip())
    return found_path, collected[-limit:]


def _humanize(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days} 天 {hours} 小时"
    minutes = rest // 60
    return f"{hours} 小时 {minutes} 分" if hours else f"{minutes} 分钟"


_UPTIME_RE = re.compile(r"uptime=(\d+)s")


def _gateway_started_before_install(
    log_path: Path | None, plugin_dir: Path
) -> str | None:
    """网关是不是在插件安装**之前**就启动了？是则返回已运行时长的人话描述。

    Hermes 的 memory_monitor 会周期性打 ``uptime=<秒>s``，拿它反推进程启动时刻，
    再和插件目录的 mtime 比 —— 这是「装完没重启」的直接证据，而不是猜测。
    """
    if log_path is None or not log_path.is_file() or not plugin_dir.exists():
        return None
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as handle:
            if size > 512 * 1024:
                handle.seek(size - 512 * 1024)
                handle.readline()
            tail = handle.read().decode("utf-8", "replace")
        log_mtime = log_path.stat().st_mtime
        installed_at = plugin_dir.stat().st_mtime
    except OSError:
        return None

    matches = _UPTIME_RE.findall(tail)
    if not matches:
        return None
    uptime = int(matches[-1])
    # 最后一条 uptime 对应的时刻 ≈ 日志 mtime，反推出进程启动时刻。
    started_at = log_mtime - uptime
    if started_at < installed_at:
        return _humanize(uptime)
    return None


def diagnose(home: Path | None = None) -> Diagnosis:
    result = Diagnosis()
    paths = resolve_paths(home)
    info = install_status(home=home)

    existing = [p for p in log_paths(home) if p.is_file()]
    result.log_path = existing[0] if existing else (paths.hermes_home / "logs" / LOG_NAMES[0])
    result.log_exists = bool(existing)
    _, lines = read_livis_lines(home)
    result.livis_lines = lines

    # -- 先看安装/启用，这两步不过日志也不会有 ------------------------------
    if not info["installed"]:
        profile = info.get("profile", "default")
        result.verdict = "❌ 插件没装到这个 Hermes 主目录 —— 网关根本看不到它。"
        result.hints = [f"目标主目录：{info['hermes_home']}", "hermes-livis install"]
        if profile != "default":
            # profile 模式下 home 是 <root>/profiles/<name>/，插件、凭据、日志
            # 全都在那底下。装错 profile 是「装了却一行日志都没有」的常见根因。
            result.hints.insert(
                0,
                f"⚠️ 当前激活 profile 是 {profile}（非 default）—— "
                f"插件必须装进 {info['profile_home']}，"
                f"凭据也要放在 {info['profile_home']}/livis/",
            )
        return result

    if not info["enabled_in_config"]:
        result.verdict = (
            "❌ 插件已安装但没写进 config.yaml 的 plugins.enabled —— "
            "用户插件是 opt-in 的，不在名单里就不会被加载。"
        )
        result.hints = ["hermes-livis install   # 会补写 plugins.enabled"]
        return result

    if not result.log_exists:
        result.verdict = (
            "⚠️ 找不到网关日志文件 —— 网关可能是前台运行的（日志直接打在终端），"
            "或者还没启动过。"
        )
        result.hints = [
            f"预期路径：{result.log_path}",
            "前台运行时直接看终端输出，或：hermes gateway start 后再看这里",
        ]
        return result

    # -- 有日志了，按 [livis] 行分类 ----------------------------------------
    if not lines:
        # 头号原因：网关进程比插件还老。Hermes 的插件发现与配置读取都发生在
        # **进程启动时**，往 plugins/ 放文件、往 config.yaml 写 enabled，对一个
        # 已经在跑的进程毫无影响 —— 它压根不知道有这个插件。
        stale = _gateway_started_before_install(result.log_path, paths.target)
        if stale:
            result.verdict = (
                f"❌ 网关进程比插件还老（已运行约 {stale}），日志里一行 [livis] "
                "都没有 —— 它启动时这个插件还不存在。**插件是在网关启动时发现的**，"
                "装完必须重启网关才会生效。"
            )
            result.hints = [
                "hermes gateway restart",
                "重启后应看到：[livis] 适配器 … 已初始化 → 握手已发送 → 中继已确认连接",
            ]
            return result

        result.verdict = (
            "❌ 网关日志里一行 [livis] 都没有 —— 适配器**从未被实例化**。"
        )
        result.hints = [
            "① 装完插件后重启过网关吗？插件只在进程启动时被发现：hermes gateway restart",
            f"② 网关看到的凭据目录是否与 CLI 一致？CLI 看到的是 {info['state_dir']}"
            f"（{'存在' if info['state_dir_exists'] else '不存在'}）；"
            "systemd / 容器换了 HOME 就会不一致",
            "③ 无凭据时需要 LIVIS_ENABLED=true 才会启用平台（否则不打扰未配置的用户）",
        ]
        return result

    joined = "\n".join(lines)
    tail = lines[-1]

    if "等待登录" in joined or "尚未登录" in joined:
        result.verdict = (
            "❌ 适配器起来了，但它看到的凭据目录里**没有凭据** —— "
            "十有八九是网关和 CLI 解析到了不同的目录。"
        )
        result.hints = [
            f"CLI 看到的：{info['state_dir']}",
            "网关看到的：日志里 `[livis] 适配器 … state=` 那一行",
            "两者不同就用 LIVIS_STATE_DIR 显式指到同一个目录",
        ]
        return result

    reconnects = joined.count("后重连（第")
    if "中继已确认连接" in joined:
        # 确认连接之后是否又掉了？看最后一条相关记录。
        last_connected = max(
            (i for i, line in enumerate(lines) if "中继已确认连接" in line), default=-1
        )
        later = lines[last_connected + 1 :]
        if any("后重连（第" in line for line in later):
            result.verdict = (
                "⚠️ 连上过但又断了并在重连 —— 同一个 agent_id 被另一条连接顶掉是"
                "最常见的原因（另一台机器的网关、openclaw 的 livis 渠道、"
                "或忘了退出的 hermes-livis echo/probe --hold）。"
            )
            result.hints = [
                "确认全网只有一处在用这个 agent_id",
                "hermes-livis status   # 看 agent_id 是否与 APP 里绑定的一致",
            ]
            return result
        result.ok = True
        result.verdict = (
            "✅ 适配器已连上中继（日志里有「中继已确认连接」）。"
            "如果 APP 仍显示离线，那就不是连接问题 —— 多半是 APP 绑定的 "
            "agent_id 与这里用的不是同一个。"
        )
        result.hints = [
            "hermes-livis status   # 比对 agent_id 与 APP 里绑定的值",
            f"最后一条 livis 日志：{tail}",
        ]
        return result

    if reconnects:
        result.verdict = (
            f"❌ 适配器在反复重连（日志里 {reconnects} 次），一直没收到 "
            "「中继已确认连接」。"
        )
        result.hints = [
            "hermes gateway stop 之后跑：hermes-livis probe",
            "它会把失败翻译成判词（1008=客户端被拒 / 401=令牌 / 超时=出网）",
        ]
        return result

    result.verdict = "⚠️ 有 livis 日志但状态不明确，请看下面的原文。"
    result.hints = [f"最后一条：{tail}"]
    return result


def format_diagnosis(result: Diagnosis, *, show_lines: int = 15) -> list[str]:
    out = ["", "理想眼镜渠道 · 离线诊断", "─" * 46, f"  {result.verdict}", ""]
    for hint in result.hints:
        out.append(f"    → {hint}")
    if result.hints:
        out.append("")
    if result.livis_lines:
        out.append(f"  最近的 [livis] 日志（{result.log_path}）：")
        for line in result.livis_lines[-show_lines:]:
            out.append(f"    {line}")
        out.append("")
    elif result.log_exists:
        out.append(f"  日志文件存在但没有 [livis] 行：{result.log_path}")
        out.append("")
    return out


def tail_command(home: Path | None = None) -> str:
    """给用户一条可以直接复制的实时跟踪命令。"""
    path = log_paths(home)[0]
    return f'tail -f "{path}" | grep --line-buffered "\\[livis\\]"'


def env_summary() -> dict[str, str]:
    """与本插件相关的环境变量当前值（不含任何机密）。"""
    keys = (
        "HERMES_HOME", "LIVIS_STATE_DIR", "LIVIS_ENABLED", "LIVIS_NODE_NAME",
        "LIVIS_ALLOWED_NODE_IDS", "LIVIS_CREDENTIAL_POLL_SECONDS",
        "LIVIS_LOG_RAW_FRAMES",
    )
    return {key: os.getenv(key, "") for key in keys if os.getenv(key, "")}
