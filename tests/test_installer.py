"""安装器测试：原子性、幂等、卸载保留凭据、config.yaml 编辑。"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_livis import installer


def _install(home: Path) -> dict:
    return installer.install(home=home, allow_any_hermes=True)


def test_payload_is_self_contained() -> None:
    """负载会被整目录复制到 ~/.hermes/plugins/，必须自包含。"""
    payload = installer.payload_dir()
    assert (payload / "plugin.yaml").is_file()
    assert (payload / "__init__.py").is_file()
    assert (payload / "adapter.py").is_file()
    # 只允许相对导入：不能依赖外层发行包可导入
    for module in payload.glob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert "from hermes_livis" not in text, f"{module.name} 用了绝对包导入"
        assert "import hermes_livis" not in text, f"{module.name} 用了绝对包导入"


def test_plugin_name_is_independent_of_platform_name() -> None:
    """插件叫 livis-glass，网关平台仍叫 livis。

    用户插件（~/.hermes/plugins/）是被直接 import 后调 register(ctx) 的，平台名
    来自 register_platform(name=...)，与目录/清单名无关。保持 livis 不变意味着
    环境变量、会话键、已有绑定都不需要迁移。
    """
    text = (installer.payload_dir() / "plugin.yaml").read_text(encoding="utf-8")
    assert "name: livis-glass" in text
    assert installer.PLUGIN_DIR_NAME == "livis-glass"
    assert installer.PLATFORM_NAME == "livis"
    adapter_src = (installer.payload_dir() / "adapter.py").read_text(encoding="utf-8")
    assert 'PLATFORM_NAME = "livis"' in (
        (installer.payload_dir() / "constants.py").read_text(encoding="utf-8")
    )
    assert "name=PLATFORM_NAME" in adapter_src


def test_install_places_payload_and_enables_plugin(tmp_path: Path) -> None:
    result = _install(tmp_path)
    target = tmp_path / "plugins" / "livis-glass"
    assert (target / "plugin.yaml").is_file()
    assert (target / "adapter.py").is_file()
    assert result["config_changed"] is True

    import yaml

    config = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert config["plugins"]["enabled"] == ["livis-glass"]


def test_install_is_idempotent_and_backs_up(tmp_path: Path) -> None:
    _install(tmp_path)
    second = _install(tmp_path)
    assert second["config_changed"] is False, "重复安装不该重复写 enabled"
    assert second["backup"], "第二次安装应把旧版本备份起来"
    assert Path(second["backup"]).is_dir()


def test_install_leaves_no_staging_dirs(tmp_path: Path) -> None:
    _install(tmp_path)
    leftovers = [
        p.name for p in (tmp_path / "plugins").iterdir() if p.name.startswith(".")
        and "staging" in p.name
    ]
    assert leftovers == []


def test_install_preserves_other_enabled_plugins(tmp_path: Path) -> None:
    import yaml

    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({"plugins": {"enabled": ["something-else"]}, "model": "x"}),
        encoding="utf-8",
    )
    _install(tmp_path)
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert data["plugins"]["enabled"] == ["something-else", "livis-glass"]
    assert data["model"] == "x", "不能破坏 config.yaml 的其他内容"


def test_no_enable_skips_config(tmp_path: Path) -> None:
    installer.install(home=tmp_path, allow_any_hermes=True, enable=False)
    assert not (tmp_path / "config.yaml").exists()


def test_uninstall_keeps_credentials_by_default(tmp_path: Path) -> None:
    _install(tmp_path)
    state = tmp_path / "livis"
    state.mkdir(parents=True, exist_ok=True)
    (state / "tokens.json").write_text("{}", encoding="utf-8")

    result = installer.uninstall(home=tmp_path)
    assert result["removed"] is True
    assert result["config_changed"] is True
    assert result["state_kept"] is True
    assert (state / "tokens.json").exists(), "误卸载不该让用户重新登录"

    import yaml

    data = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert "livis-glass" not in data["plugins"]["enabled"]


def test_uninstall_purge_removes_state(tmp_path: Path) -> None:
    _install(tmp_path)
    state = tmp_path / "livis"
    state.mkdir(parents=True, exist_ok=True)
    (state / "tokens.json").write_text("{}", encoding="utf-8")

    result = installer.uninstall(home=tmp_path, purge=True)
    assert result["state_removed"] is True
    assert not state.exists()


def test_uninstall_on_clean_home_is_noop(tmp_path: Path) -> None:
    result = installer.uninstall(home=tmp_path)
    assert result["removed"] is False
    assert result["config_changed"] is False


def test_status_is_read_only(tmp_path: Path) -> None:
    info = installer.status(home=tmp_path)
    assert info["installed"] is False
    assert info["enabled_in_config"] is False
    # 只读：不得创建任何东西
    assert not (tmp_path / "plugins").exists()
    assert not (tmp_path / "config.yaml").exists()
    assert not (tmp_path / "livis").exists()


def test_status_after_install(tmp_path: Path) -> None:
    _install(tmp_path)
    info = installer.status(home=tmp_path)
    assert info["installed"] is True
    assert info["enabled_in_config"] is True
    assert info["platform"] == "livis"


def test_version_parser() -> None:
    assert installer._parse_version("0.19.0") == (0, 19, 0)
    assert installer._parse_version("hermes 1.2.3 (abc)") == (1, 2, 3)
    assert installer._parse_version("no digits") is None


def test_version_is_advisory_not_a_hard_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """版本号只告警。

    ``importlib.metadata`` 里的 hermes 版本经常与实际运行的源码树不一致
    （editable 安装、直接跑仓库、多环境混装）。接口都在却因为一个版本号拒绝
    安装是没有依据的误伤 —— 硬闸门只能是接口探测。
    """
    monkeypatch.setattr(installer, "detect_hermes_version", lambda: (0, 8, 0))
    monkeypatch.setattr(installer, "check_base_apis", lambda **_kw: [])

    result = installer.install(home=tmp_path)
    assert (tmp_path / "plugins" / "livis-glass" / "plugin.yaml").is_file()
    assert "低于已验证下界" in result["version_warning"]


def test_version_warning_text(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "低于已验证下界" in installer.version_warning((0, 1, 0))
    assert "高于已验证上界" in installer.version_warning((9, 0, 0))
    assert installer.version_warning((0, 19, 0)) == ""
    assert "未能确定" in installer.version_warning(None)


def test_missing_base_api_is_a_hard_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺接口一定跑不起来，必须在安装期就拒绝。"""
    import gateway.platforms.base as base_mod

    monkeypatch.delattr(
        base_mod.BasePlatformAdapter, "on_processing_complete", raising=False
    )
    with pytest.raises(installer.InstallError, match="缺少本插件依赖的接口"):
        installer.install(home=tmp_path)


def test_allow_any_skips_the_api_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gateway.platforms.base as base_mod

    monkeypatch.delattr(
        base_mod.BasePlatformAdapter, "on_processing_complete", raising=False
    )
    result = installer.install(home=tmp_path, allow_any_hermes=True)
    assert "on_processing_complete" in result["missing_base_apis"]


def test_broken_config_is_reported_not_silently_overwritten(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("plugins: not-a-mapping\n", encoding="utf-8")
    with pytest.raises(installer.InstallError, match="不是映射"):
        _install(tmp_path)


# ---------------------------------------------------------------------------
# 曾用名迁移：livis-platform → livis-glass
# ---------------------------------------------------------------------------

def test_install_removes_the_legacy_directory(tmp_path: Path) -> None:
    """两个目录同时存在会各自注册同一个平台名，谁生效取决于扫描顺序。"""
    legacy = tmp_path / "plugins" / "livis-platform"
    legacy.mkdir(parents=True)
    (legacy / "plugin.yaml").write_text("name: livis-platform\n", encoding="utf-8")

    result = _install(tmp_path)

    assert result["removed_legacy"] == ["livis-platform"]
    assert not legacy.exists()
    assert (tmp_path / "plugins" / "livis-glass" / "plugin.yaml").is_file()


def test_install_drops_the_legacy_enabled_key(tmp_path: Path) -> None:
    import yaml

    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({"plugins": {"enabled": ["other", "livis-platform"]}}),
        encoding="utf-8",
    )
    result = _install(tmp_path)

    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert data["plugins"]["enabled"] == ["other", "livis-glass"]
    assert result["config_changed"] is True


def test_legacy_key_is_dropped_even_when_new_key_present(tmp_path: Path) -> None:
    """残留的旧 key 会让 hermes 去加载一个已不存在的目录。"""
    import yaml

    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({"plugins": {"enabled": ["livis-platform", "livis-glass"]}}),
        encoding="utf-8",
    )
    result = _install(tmp_path)

    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert data["plugins"]["enabled"] == ["livis-glass"]
    assert result["config_changed"] is True


def test_uninstall_also_clears_the_legacy_install(tmp_path: Path) -> None:
    import yaml

    _install(tmp_path)
    legacy = tmp_path / "plugins" / "livis-platform"
    legacy.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["plugins"]["enabled"].append("livis-platform")
    config.write_text(yaml.safe_dump(data), encoding="utf-8")

    installer.uninstall(home=tmp_path)

    assert not legacy.exists()
    left = yaml.safe_load(config.read_text(encoding="utf-8"))["plugins"]["enabled"]
    assert "livis-platform" not in left and "livis-glass" not in left
