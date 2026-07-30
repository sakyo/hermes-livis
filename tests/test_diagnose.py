"""离线诊断测试。

判词必须准确 —— 把「装完没重启网关」说成「凭据目录不一致」会让人查错方向
好几个小时。下面每条对应一种真实遇到过（或极易遇到）的现场。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_livis import diagnose as diag
from hermes_livis import installer

# 2026-07-30 真实现场的日志片段：网关跑了 16 天，整段没有一行 [livis]。
REAL_LOG_NO_LIVIS = """\
2026-07-30 21:37:10,466 ERROR gateway.platforms.weixin: [Weixin] Session expired; pausing for 10 minutes
2026-07-30 21:39:01,241 INFO gateway.memory_monitor: [MEMORY] rss=285MB gc=(3, 2, 5) threads=11 uptime=1380906s
2026-07-30 22:44:01,250 INFO gateway.memory_monitor: [MEMORY] rss=285MB gc=(330, 7, 6) threads=11 uptime=1384806s
2026-07-30 22:47:11,027 ERROR gateway.platforms.weixin: [Weixin] Session expired; pausing for 10 minutes
2026-07-30 22:49:01,250 INFO gateway.memory_monitor: [MEMORY] rss=285MB gc=(87, 0, 7) threads=11 uptime=1385106s
"""


def _write_log(home: Path, text: str) -> Path:
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "gateway.log"
    path.write_text(text, encoding="utf-8")
    return path


def _install(home: Path) -> None:
    installer.install(home=home, allow_any_hermes=True)


# ---------------------------------------------------------------------------
# 未安装 / 未启用
# ---------------------------------------------------------------------------

def test_not_installed_is_reported_first(tmp_path: Path) -> None:
    result = diag.diagnose(tmp_path)
    assert "没装到这个 Hermes 主目录" in result.verdict
    assert any("hermes-livis install" in hint for hint in result.hints)


def test_installed_but_not_enabled(tmp_path: Path) -> None:
    installer.install(home=tmp_path, allow_any_hermes=True, enable=False)
    result = diag.diagnose(tmp_path)
    assert "plugins.enabled" in result.verdict


# ---------------------------------------------------------------------------
# 装完没重启网关 —— 真实现场
# ---------------------------------------------------------------------------

def test_gateway_older_than_plugin_is_the_verdict(tmp_path: Path) -> None:
    """插件目录比网关进程新 ⇒ 它启动时插件还不存在，必须重启。

    这是 2026-07-30 那次「已绑定但一直离线」的真实原因：网关连续跑了 16 天，
    往 plugins/ 放文件对它毫无影响。
    """
    log = _write_log(tmp_path, REAL_LOG_NO_LIVIS)
    # 日志最后一条 uptime=1385106s，把 mtime 设成"现在"，即进程 16 天前启动
    os.utime(log, (time.time(), time.time()))
    _install(tmp_path)  # 插件是刚刚才装的

    result = diag.diagnose(tmp_path)

    assert "比插件还老" in result.verdict
    assert "16 天" in result.verdict
    assert result.hints[0] == "hermes gateway restart"
    assert result.ok is False


def test_gateway_newer_than_plugin_falls_through_to_other_causes(
    tmp_path: Path,
) -> None:
    """网关是插件装好之后才起的，那就不是「没重启」，要往别的原因引导。"""
    _install(tmp_path)
    log = _write_log(
        tmp_path,
        "2026-07-30 22:49:01 INFO gateway.memory_monitor: [MEMORY] uptime=30s\n",
    )
    os.utime(log, (time.time(), time.time()))

    result = diag.diagnose(tmp_path)

    assert "比插件还老" not in result.verdict
    assert "从未被实例化" in result.verdict
    # 三条候选原因都要给出来
    assert any("重启" in hint for hint in result.hints)
    assert any("凭据目录" in hint for hint in result.hints)
    assert any("LIVIS_ENABLED" in hint for hint in result.hints)


# ---------------------------------------------------------------------------
# 有 [livis] 日志时的分类
# ---------------------------------------------------------------------------

def test_waiting_for_login_points_at_the_state_dir(tmp_path: Path) -> None:
    _install(tmp_path)
    _write_log(tmp_path, "22:00 WARNING [livis] 等待登录中……凭据目录：/root/.hermes/livis\n")
    result = diag.diagnose(tmp_path)
    assert "没有凭据" in result.verdict
    assert any("LIVIS_STATE_DIR" in hint for hint in result.hints)


def test_connected_is_reported_as_ok(tmp_path: Path) -> None:
    _install(tmp_path)
    _write_log(
        tmp_path,
        "22:00 INFO [livis] 握手已发送 (agent_id=openclaw-x)\n"
        "22:00 INFO [livis] 中继已确认连接\n",
    )
    result = diag.diagnose(tmp_path)
    assert result.ok is True
    assert "已连上中继" in result.verdict
    assert any("agent_id" in hint for hint in result.hints)


def test_connected_then_dropped_suggests_connection_contention(tmp_path: Path) -> None:
    """连上又掉、反复重连 ⇒ 同一 agent_id 被另一条连接顶掉。"""
    _install(tmp_path)
    _write_log(
        tmp_path,
        "22:00 INFO [livis] 中继已确认连接\n"
        "22:01 INFO [livis] 1.0s 后重连（第 1 次）\n"
        "22:02 INFO [livis] 2.0s 后重连（第 2 次）\n",
    )
    result = diag.diagnose(tmp_path)
    assert result.ok is False
    assert "顶掉" in result.verdict


def test_never_connected_but_retrying_points_at_probe(tmp_path: Path) -> None:
    _install(tmp_path)
    _write_log(
        tmp_path,
        "22:00 INFO [livis] 握手已发送 (agent_id=x)\n"
        "22:01 INFO [livis] 1.0s 后重连（第 1 次）\n",
    )
    result = diag.diagnose(tmp_path)
    assert "反复重连" in result.verdict
    assert any("probe" in hint for hint in result.hints)


def test_missing_log_file_is_explained(tmp_path: Path) -> None:
    _install(tmp_path)
    result = diag.diagnose(tmp_path)
    assert "找不到网关日志" in result.verdict
    assert any("前台" in hint for hint in result.hints)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def test_log_reader_filters_and_limits(tmp_path: Path) -> None:
    _write_log(
        tmp_path,
        "".join(f"line {i} noise\n" for i in range(100))
        + "".join(f"[livis] event {i}\n" for i in range(10)),
    )
    _, lines = diag.read_livis_lines(tmp_path, limit=5)
    assert len(lines) == 5
    assert all("[livis]" in line for line in lines)

    _, everything = diag.read_livis_lines(tmp_path, limit=200, only_livis=False)
    assert len(everything) == 110


def test_log_reader_handles_huge_files(tmp_path: Path) -> None:
    """日志可能几百 MB，只读尾部，不能整个读进内存。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    path = log_dir / "gateway.log"
    with path.open("w", encoding="utf-8") as handle:
        handle.write("x" * 3 * 1024 * 1024 + "\n")
        handle.write("[livis] 中继已确认连接\n")
    _, lines = diag.read_livis_lines(tmp_path)
    assert lines == ["[livis] 中继已确认连接"]


def test_humanize() -> None:
    assert diag._humanize(1385106) == "16 天 0 小时"
    assert diag._humanize(3700) == "1 小时 1 分"
    assert diag._humanize(120) == "2 分钟"


def test_tail_command_is_copy_pasteable(tmp_path: Path) -> None:
    command = diag.tail_command(tmp_path)
    assert command.startswith("tail -f")
    assert "gateway.log" in command
    assert "livis" in command


def test_env_summary_omits_unset_and_carries_no_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVIS_ENABLED", "true")
    monkeypatch.delenv("LIVIS_NODE_NAME", raising=False)
    monkeypatch.setenv("LIVIS_REFRESH_TOKEN", "super-secret")
    summary = diag.env_summary()
    assert summary["LIVIS_ENABLED"] == "true"
    assert "LIVIS_NODE_NAME" not in summary
    assert "LIVIS_REFRESH_TOKEN" not in summary, "机密不得出现在诊断输出里"


# ---------------------------------------------------------------------------
# profile 模式（~/.hermes/profiles/<name>/）
#
# 网关跑在非默认 profile 下时，插件 / 凭据 / 日志全都在 profiles/<name>/ 底下。
# 在交互 shell 里跑 hermes-livis 很容易装进 default，于是「装了、绑定了，
# 但一行 [livis] 日志都没有」。
# ---------------------------------------------------------------------------

def test_profile_home_layout() -> None:
    root = Path("/root/.hermes")
    assert installer.profile_home("default", root) == root
    assert installer.profile_home("55", root) == root / "profiles" / "55"


def test_profile_home_from_inside_a_profile_home() -> None:
    """已经身处 profiles/<name>/ 时要能推回根，不能再套一层。"""
    inside = Path("/root/.hermes/profiles/55")
    assert installer.profile_home("77", inside) == Path("/root/.hermes/profiles/77")


def test_active_profile_reads_the_sticky_file(tmp_path: Path) -> None:
    assert installer.active_profile(tmp_path) == "default"
    (tmp_path / "active_profile").write_text("55\n", encoding="utf-8")
    assert installer.active_profile(tmp_path) == "55"


def test_hermes_home_follows_the_active_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HERMES_HOME 未设时要跟随 active_profile，而不是退回 default。"""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    (tmp_path / "active_profile").write_text("55", encoding="utf-8")
    monkeypatch.setattr(installer, "active_profile", lambda root=None: "55")
    monkeypatch.setattr(
        installer, "profile_home", lambda p, root=None: tmp_path / "profiles" / p
    )
    monkeypatch.setitem(
        __import__("sys").modules, "hermes_constants", None
    )  # 强制走 except 分支
    assert installer.hermes_home() == tmp_path / "profiles" / "55"


def test_explicit_hermes_home_wins_over_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "explicit"))
    assert installer.hermes_home() == (tmp_path / "explicit").resolve()


def test_not_installed_under_a_profile_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer, "active_profile", lambda root=None: "55")
    monkeypatch.setattr(
        installer, "profile_home", lambda p, root=None: tmp_path / "profiles" / p
    )
    result = diag.diagnose(tmp_path)
    assert "没装到" in result.verdict
    assert any("profile 是 55" in hint for hint in result.hints)
    assert any("profiles/55" in hint for hint in result.hints)
