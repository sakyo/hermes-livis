"""``hermes-livis`` 命令行入口。

安装插件之前就能用（``install`` / ``doctor``），也覆盖凭据管理
（``login`` / ``logout`` / ``status`` / ``import-openclaw`` / ``reset-agent-id``），
后者与插件注册进 Hermes 的 ``hermes livis ...`` 是同一套实现。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .installer import (
    InstallError,
    check_base_apis,
    install,
    payload_dir,
    status,
    uninstall,
)
from .plugin import cli as plugin_cli


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-livis",
        description="理想眼镜 (Livis) —— Hermes Agent 平台适配器",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command")

    inst = subs.add_parser("install", help="把插件安装进 Hermes 并启用")
    inst.add_argument("--home", help="Hermes 主目录（默认 $HERMES_HOME 或 ~/.hermes）")
    inst.add_argument(
        "--allow-any-hermes", action="store_true", help="跳过 Hermes 版本/接口检查"
    )
    inst.add_argument(
        "--no-enable", action="store_true", help="只复制文件，不写 plugins.enabled"
    )

    uninst = subs.add_parser("uninstall", help="卸载插件（默认保留凭据）")
    uninst.add_argument("--home")
    uninst.add_argument(
        "--purge", action="store_true", help="连凭据与投递状态一起删除"
    )

    inst_status = subs.add_parser("doctor", help="检查安装与运行环境")
    inst_status.add_argument("--home")
    inst_status.add_argument("--json", action="store_true", dest="as_json")

    echo = subs.add_parser(
        "echo",
        help="联调回声模式：绕过 Hermes 生命周期直接跑适配器，用桩回复代替 agent",
    )
    echo.add_argument(
        "--reply",
        default="你好啊 #{n}",
        metavar="模板",
        help="回复模板，可用 {n} 序列号 / {text} 原文 / {node} 发送方（默认「你好啊 #{n}」）",
    )
    echo.add_argument(
        "--duration", type=float, default=0.0, metavar="秒",
        help="运行这么多秒后自动退出（默认一直跑到 Ctrl-C）",
    )
    echo.add_argument("--verbose", "-v", action="store_true", help="打开 DEBUG 日志")

    # 凭据管理子命令，与插件内 CLI 共用实现
    plugin_cli.build_subcommands_into(subs)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = getattr(args, "command", None)

    try:
        if command == "install":
            result = install(
                home=Path(args.home) if args.home else None,
                allow_any_hermes=bool(args.allow_any_hermes),
                enable=not bool(args.no_enable),
            )
            print(f"✅ 插件已安装到 {result['target']}")
            print(f"   Hermes 版本  : {result['hermes_version'] or '<未检测到>'}")
            if result["version_warning"]:
                print(f"   ⚠️ {result['version_warning']}")
            if result["missing_base_apis"]:
                print(f"   ⚠️ 缺失接口  : {', '.join(result['missing_base_apis'])}")
            if result["backup"]:
                print(f"   旧版本备份  : {result['backup']}")
            if result["config_changed"]:
                print(f"   已在 {result['config']} 启用 plugins.enabled")
            else:
                print("   plugins.enabled 无需修改")
            print()
            print("下一步：")
            print("   hermes-livis login        # 登录并拿 Agent ID")
            print("   # 在理想 APP 里绑定该 Agent ID")
            print("   hermes gateway start")
            return 0

        if command == "uninstall":
            result = uninstall(
                home=Path(args.home) if args.home else None, purge=bool(args.purge)
            )
            print("✅ 已卸载" if result["removed"] else "ℹ️  插件目录本就不存在")
            if result["config_changed"]:
                print("   已从 plugins.enabled 移除")
            if result["state_removed"]:
                print(f"   已删除状态目录 {result['state_dir']}")
            elif result["state_kept"]:
                print(f"   保留凭据与状态：{result['state_dir']}（--purge 可一并删除）")
            return 0

        if command == "doctor":
            info = status(home=Path(args.home) if args.home else None)
            info["payload"] = str(payload_dir())
            info["package_version"] = __version__
            try:
                info["missing_base_apis"] = check_base_apis(allow_missing=True)
            except InstallError:
                info["missing_base_apis"] = ["<无法导入 hermes>"]
            if getattr(args, "as_json", False):
                print(json.dumps(info, ensure_ascii=False, indent=2))
                return 0
            print()
            print("hermes-livis doctor")
            print("─" * 46)
            print(f"  发行包版本    : {info['package_version']}")
            print(f"  Python        : {info['python']}")
            print(f"  Hermes 主目录 : {info['hermes_home']}")
            print(f"  Hermes 版本   : {info['hermes_version'] or '<未检测到>'}")
            print(f"  已安装        : {'是' if info['installed'] else '否'} ({info['install_path']})")
            print(f"  已启用        : {'是' if info['enabled_in_config'] else '否'} ({info['config']})")
            print(f"  状态目录      : {info['state_dir']}"
                  f"{'（存在）' if info['state_dir_exists'] else '（不存在）'}")
            missing = info.get("missing_base_apis") or []
            print(f"  基类接口      : {'齐备' if not missing else '缺 ' + ', '.join(missing)}")
            print()
            if not info["installed"]:
                print("  下一步：hermes-livis install")
            elif not info["enabled_in_config"]:
                print("  下一步：hermes-livis install（会补写 plugins.enabled）")
            else:
                print("  下一步：hermes-livis status  # 看凭据是否就绪")
            print()
            return 0

        if command == "echo":
            from .echo import main as echo_main

            return echo_main(
                reply_template=args.reply,
                duration=float(args.duration),
                verbose=bool(args.verbose),
            )

        if command in {
            "login", "logout", "status", "probe",
            "import-openclaw", "reset-agent-id",
        }:
            args.livis_command = command
            return plugin_cli.run_subcommand(args)

        parser.print_help()
        return 0

    except InstallError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
