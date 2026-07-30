"""测试夹具。

适配器测试需要能 import Hermes 的 ``gateway.*``。解析顺序：
1. 已经能 import ⇒ 直接用；
2. ``HERMES_REPO`` 环境变量指向的仓库；
3. 常见的相邻目录（与本仓库同级的 ``hermes-agent``）。

都找不到就把依赖 Hermes 的测试标记为 skip —— 协议层、store、safeio 这些
纯逻辑测试不受影响，照常运行。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _locate_hermes() -> Path | None:
    try:
        import gateway.platforms.base  # noqa: F401

        return Path("<already importable>")
    except Exception:
        pass

    candidates: list[Path] = []
    env_repo = os.getenv("HERMES_REPO", "").strip()
    if env_repo:
        candidates.append(Path(env_repo).expanduser())
    candidates.append(REPO_ROOT.parent / "hermes-agent")

    for candidate in candidates:
        if (candidate / "gateway" / "platforms" / "base.py").is_file():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return candidate
    return None


HERMES_PATH = _locate_hermes()
try:
    import gateway.platforms.base  # noqa: F401

    HERMES_AVAILABLE = True
except Exception:
    HERMES_AVAILABLE = False

requires_hermes = pytest.mark.skipif(
    not HERMES_AVAILABLE,
    reason="需要可导入的 hermes-agent（设 HERMES_REPO 指向仓库）",
)


AGENT_ID = "openclaw-test-0001"
DEVICE_ID = "pc_testdevice"


@pytest.fixture(scope="session", autouse=True)
def _register_platform_name() -> None:
    """让 ``Platform("livis")`` 可解析。

    Hermes 的 ``Platform`` 是动态枚举：插件平台名要么出现在仓库内置的
    ``plugins/platforms/`` 目录里，要么已经登记进 ``platform_registry``。生产
    环境的顺序是"插件先 ``register(ctx)``、网关再实例化适配器"，测试里直接 new
    适配器就少了第一步，所以这里补上 —— 复刻真实顺序，而不是绕过枚举校验。
    """
    if not HERMES_AVAILABLE:
        return
    from gateway.platform_registry import PlatformEntry, platform_registry

    if not platform_registry.is_registered("livis"):
        platform_registry.register(
            PlatformEntry(
                name="livis",
                label="理想眼镜 (Livis)",
                adapter_factory=lambda cfg: None,
                check_fn=lambda: True,
                source="plugin",
            )
        )


@pytest.fixture()
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个已"登录"的凭据目录。"""
    root = tmp_path / "livis"
    root.mkdir(parents=True, exist_ok=True)
    (root / "tokens.json").write_text(
        json.dumps({"relay_refresh_token": "rt-test"}), encoding="utf-8"
    )
    (root / "agent.id").write_text(AGENT_ID, encoding="utf-8")
    (root / "device.id").write_text(DEVICE_ID, encoding="utf-8")
    monkeypatch.setenv("LIVIS_STATE_DIR", str(root))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # 收口兜底调到最小，测试里不用真等 5 秒。
    monkeypatch.setenv("LIVIS_RESULT_FALLBACK_MS", "50")
    monkeypatch.setenv("LIVIS_JOB_WATCHDOG_SECONDS", "10")
    monkeypatch.delenv("LIVIS_ALLOWED_NODE_IDS", raising=False)
    monkeypatch.delenv("LIVIS_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("LIVIS_AGENT_ID", raising=False)
    monkeypatch.delenv("LIVIS_DEVICE_ID", raising=False)
    return root


@pytest.fixture()
def empty_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """没有任何凭据，并且屏蔽掉开发机上可能真实存在的 ~/.openclaw。"""
    root = tmp_path / "empty"
    monkeypatch.setenv("LIVIS_STATE_DIR", str(root))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("LIVIS_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("LIVIS_AGENT_ID", raising=False)

    from hermes_livis.plugin import constants

    monkeypatch.setattr(constants, "OPENCLAW_TOKENS_FILE", tmp_path / "no-tokens.json")
    monkeypatch.setattr(constants, "OPENCLAW_AGENT_ID_FILE", tmp_path / "no-agent.id")
    monkeypatch.setattr(constants, "OPENCLAW_DEVICE_ID_FILE", tmp_path / "no-device.id")
    return root
