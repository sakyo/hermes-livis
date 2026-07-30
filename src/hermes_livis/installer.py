"""把插件负载安装进 Hermes，并维护 ``plugins.enabled``。

Hermes 的插件发现规则决定了这里的两步：

1. 用户插件放在 ``<hermes-home>/plugins/<key>/``，``key`` 就是目录名；
2. 用户插件是**opt-in**的 —— 必须出现在 ``config.yaml`` 的 ``plugins.enabled``
   列表里才会被加载（仓库内置的 ``plugins/platforms/*`` 才自动加载）。

网关侧的平台名与插件目录名无关 —— 用户插件是被直接 import 后调 ``register(ctx)``
的，平台名来自 ``register_platform(name=...)``（只有仓库内置插件才用「清单名去掉
``-platform``」推导）。

安装是原子的：先复制到暂存目录，再 ``os.replace`` 换入；换入前把旧目录移到
备份处，失败则回滚。卸载默认**保留**凭据与投递状态，除非显式 ``--purge``。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN_DIR_NAME = "livis-glass"
PLUGIN_KEY = PLUGIN_DIR_NAME

# 曾用名。安装时必须把它们清掉：两个目录同时存在会各自 register_platform()
# 注册同一个平台名，谁后加载谁生效，行为不可预测。
LEGACY_PLUGIN_DIR_NAMES = ("livis-platform",)

# 网关侧的平台名，与插件目录名**无关**。用户插件（~/.hermes/plugins/）是被
# 直接 import 后调 register(ctx) 的，平台名来自 register_platform(name=...)；
# 只有仓库内置插件才会用"清单名去掉 -platform"去推导。保持 livis 不变意味着
# 环境变量（LIVIS_*）、会话键、已有绑定都不需要迁移。
PLATFORM_NAME = "livis"

# 已验证过的 Hermes 版本区间。低于下界缺少本插件依赖的基类 API；上界是
# "还没做过兼容性复核"的意思，可用 --allow-any-hermes 越过。
MIN_HERMES_VERSION = (0, 19, 0)
MAX_HERMES_VERSION = (0, 21, 0)

# 本插件依赖的 Hermes 基类 API —— 安装时做一次实探，缺哪个立刻报出来，
# 而不是等到运行时才 AttributeError。
REQUIRED_BASE_APIS = (
    "handle_message",
    "build_source",
    "on_processing_complete",
    "interrupt_session_activity",
    "validate_media_delivery_path",
)


class InstallError(RuntimeError):
    pass


@dataclass
class InstallPaths:
    hermes_home: Path
    plugins_dir: Path
    target: Path
    backup_dir: Path
    config: Path


def payload_dir() -> Path:
    """发行包内的插件负载目录。"""
    return Path(__file__).resolve().parent / "plugin"


def hermes_home() -> Path:
    raw = os.getenv("HERMES_HOME", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).resolve()
    except Exception:
        return (Path.home() / ".hermes").resolve()


def resolve_paths(home: Path | None = None) -> InstallPaths:
    base = Path(home).expanduser().resolve() if home else hermes_home()
    return InstallPaths(
        hermes_home=base,
        plugins_dir=base / "plugins",
        target=base / "plugins" / PLUGIN_DIR_NAME,
        backup_dir=base / "plugins" / ".livis-backups",
        config=base / "config.yaml",
    )


# ---------------------------------------------------------------------------
# 版本与 API 探测
# ---------------------------------------------------------------------------

def _parse_version(raw: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def detect_hermes_version() -> tuple[int, int, int] | None:
    """依次尝试：包元数据 → ``hermes --version`` → 源码树 pyproject。"""
    try:
        import importlib.metadata as metadata

        parsed = _parse_version(metadata.version("hermes-agent"))
        if parsed:
            return parsed
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["hermes", "--version"], capture_output=True, text=True, timeout=20
        ).stdout
        parsed = _parse_version(out)
        if parsed:
            return parsed
    except (OSError, subprocess.SubprocessError):
        pass
    repo = os.getenv("HERMES_REPO", "").strip()
    if repo:
        try:
            text = (Path(repo) / "pyproject.toml").read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.strip().startswith("version"):
                    parsed = _parse_version(line)
                    if parsed:
                        return parsed
        except OSError:
            pass
    return None


def version_warning(version: tuple[int, int, int] | None) -> str:
    """版本号只做**告警**，不做硬闸门。

    真正决定能不能跑的是 :func:`check_base_apis` 的接口探测。版本号并不可靠：
    ``importlib.metadata`` 里的 ``hermes-agent`` 版本经常与实际运行的源码树
    不一致（editable 安装、直接跑仓库、多环境混装），据此硬拒安装会误伤真正
    兼容的环境 —— 而接口都在的时候，那种拒绝毫无依据。
    """
    lo = ".".join(map(str, MIN_HERMES_VERSION))
    hi = ".".join(map(str, MAX_HERMES_VERSION))
    if version is None:
        return f"未能确定 Hermes 版本（已验证区间 {lo} ≤ v < {hi}）；以接口探测为准。"
    text = ".".join(map(str, version))
    if version < MIN_HERMES_VERSION:
        return (
            f"检测到的 Hermes 版本 {text} 低于已验证下界 {lo}。"
            "接口探测通过则通常仍可运行；若行为异常请先升级 Hermes。"
        )
    if version >= MAX_HERMES_VERSION:
        return (
            f"Hermes {text} 高于已验证上界 {hi}，尚未做过兼容性复核。"
            "接口探测通过即可运行，但建议关注协议/接口变化。"
        )
    return ""


def check_base_apis(*, allow_missing: bool = False) -> list[str]:
    """实探 ``BasePlatformAdapter`` 上本插件依赖的方法，返回缺失项。"""
    try:
        from gateway.platforms.base import BasePlatformAdapter
    except Exception as exc:
        if allow_missing:
            return []
        raise InstallError(
            f"无法导入 Hermes 的 BasePlatformAdapter（{exc}）。"
            "请在装有 hermes 的环境里执行，或加 --allow-any-hermes。"
        ) from exc
    missing = [
        name for name in REQUIRED_BASE_APIS if not hasattr(BasePlatformAdapter, name)
    ]
    if missing and not allow_missing:
        raise InstallError(
            "当前 Hermes 缺少本插件依赖的接口：" + ", ".join(missing)
        )
    return missing


# ---------------------------------------------------------------------------
# config.yaml 的 plugins.enabled
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml

        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except ImportError as exc:
        raise InstallError(
            "需要 PyYAML 来编辑 config.yaml：pip install pyyaml"
        ) from exc
    except Exception as exc:
        raise InstallError(f"解析 {path} 失败: {exc}") from exc
    return value if isinstance(value, dict) else {}


def _dump_yaml(path: Path, data: dict[str, Any]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    os.replace(tmp, path)


def enable_in_config(paths: InstallPaths) -> bool:
    """把插件 key 加进 ``plugins.enabled``，同时摘掉曾用名。

    返回是否发生了修改。曾用名必须一起清：残留的 key 会让 hermes 去加载一个
    已经不存在的目录（日志里刷 not found），或者更糟 —— 旧目录还在时两份插件
    同时注册同一个平台名。
    """
    data = _load_yaml(paths.config)
    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise InstallError("config.yaml 里的 plugins 不是映射，请手工修正。")
    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list):
        raise InstallError("config.yaml 里的 plugins.enabled 不是列表。")

    stale = [item for item in enabled if item in LEGACY_PLUGIN_DIR_NAMES]
    if stale:
        plugins["enabled"] = enabled = [
            item for item in enabled if item not in LEGACY_PLUGIN_DIR_NAMES
        ]

    if PLUGIN_KEY in enabled:
        if stale:
            _dump_yaml(paths.config, data)
            return True
        return False
    enabled.append(PLUGIN_KEY)
    _dump_yaml(paths.config, data)
    return True


def disable_in_config(paths: InstallPaths) -> bool:
    """从 ``plugins.enabled`` 摘掉当前 key 与所有曾用名。"""
    data = _load_yaml(paths.config)
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return False
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        return False
    drop = {PLUGIN_KEY, *LEGACY_PLUGIN_DIR_NAMES}
    if not (drop & set(enabled)):
        return False
    plugins["enabled"] = [item for item in enabled if item not in drop]
    _dump_yaml(paths.config, data)
    return True


def is_enabled_in_config(paths: InstallPaths) -> bool:
    data = _load_yaml(paths.config)
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return False
    enabled = plugins.get("enabled")
    return isinstance(enabled, list) and PLUGIN_KEY in enabled


# ---------------------------------------------------------------------------
# 安装 / 卸载
# ---------------------------------------------------------------------------

def _remove_legacy_installs(paths: InstallPaths) -> list[str]:
    """删掉曾用名安装的目录，返回被清理的名字。

    不做备份：那是本插件的旧版本，内容在发行包里都有；留着反而会和新目录同时
    被加载，两份都调 ``register_platform("livis")``，谁生效取决于扫描顺序。
    """
    removed: list[str] = []
    for name in LEGACY_PLUGIN_DIR_NAMES:
        legacy = paths.plugins_dir / name
        if legacy.is_dir():
            shutil.rmtree(legacy)
            removed.append(name)
    return removed


def _copy_atomic(source: Path, target: Path, backup_root: Path) -> Path | None:
    """原子换入，失败回滚；返回旧版本的备份路径（没有旧版本则 None）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.staging-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(source, staging, ignore=shutil.ignore_patterns("__pycache__"))

    backup: Path | None = None
    try:
        if target.exists():
            backup_root.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = backup_root / f"{target.name}-{stamp}"
            os.replace(target, backup)
        os.replace(staging, target)
    except Exception:
        # 回滚：把备份放回去，清掉暂存。
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return backup


def install(
    *,
    home: Path | None = None,
    allow_any_hermes: bool = False,
    enable: bool = True,
) -> dict[str, Any]:
    paths = resolve_paths(home)
    version = detect_hermes_version()
    # 硬闸门是接口探测，不是版本号：缺接口一定跑不起来，版本号只是参考。
    missing = check_base_apis(allow_missing=allow_any_hermes)
    warning = "" if allow_any_hermes else version_warning(version)

    source = payload_dir()
    if not (source / "plugin.yaml").exists():
        raise InstallError(f"发行包损坏：{source} 里没有 plugin.yaml")

    removed_legacy = _remove_legacy_installs(paths)
    backup = _copy_atomic(source, paths.target, paths.backup_dir)
    changed = enable_in_config(paths) if enable else False

    return {
        "removed_legacy": removed_legacy,
        "hermes_version": ".".join(map(str, version)) if version else "",
        "version_warning": warning,
        "missing_base_apis": missing,
        "target": str(paths.target),
        "backup": str(backup) if backup else "",
        "config": str(paths.config),
        "config_changed": changed,
        "platform": PLATFORM_NAME,
    }


def uninstall(*, home: Path | None = None, purge: bool = False) -> dict[str, Any]:
    """移除插件目录并从 ``plugins.enabled`` 摘掉。

    默认**保留** ``<hermes-home>/livis/`` 下的凭据与投递状态 —— 误卸载不该让
    用户重新登录、重新绑定眼镜。``purge=True`` 才连状态一起删。
    """
    paths = resolve_paths(home)
    removed = False
    if paths.target.exists():
        shutil.rmtree(paths.target)
        removed = True
    # 曾用名一并清掉，免得卸载后还留着一份能被加载的旧插件。
    if _remove_legacy_installs(paths):
        removed = True
    config_changed = disable_in_config(paths)

    state = paths.hermes_home / "livis"
    state_removed = False
    if purge and state.exists():
        shutil.rmtree(state)
        state_removed = True

    return {
        "removed": removed,
        "config_changed": config_changed,
        "state_dir": str(state),
        "state_removed": state_removed,
        "state_kept": state.exists(),
    }


def status(*, home: Path | None = None) -> dict[str, Any]:
    """只读状态：不创建任何文件，也不生成 agent_id。"""
    paths = resolve_paths(home)
    version = detect_hermes_version()
    installed = (paths.target / "plugin.yaml").exists()
    state = paths.hermes_home / "livis"
    return {
        "hermes_home": str(paths.hermes_home),
        "hermes_version": ".".join(map(str, version)) if version else "",
        "installed": installed,
        "install_path": str(paths.target),
        "enabled_in_config": is_enabled_in_config(paths),
        "config": str(paths.config),
        "state_dir": str(state),
        "state_dir_exists": state.exists(),
        "platform": PLATFORM_NAME,
        "python": sys.version.split()[0],
    }
