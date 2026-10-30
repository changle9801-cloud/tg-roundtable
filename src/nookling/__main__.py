from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import webbrowser
from pathlib import Path

from .config import ConfigError, telegram_problems
from .settings import (
    DATA_DIR_ENV,
    SettingsStore,
    copy_data_dir,
    default_data_dir,
    import_legacy_toml,
    resolve_data_dir,
)

LEGACY_CONFIG = Path("config.toml")


def main() -> None:
    parser = argparse.ArgumentParser(prog="nookling", description="Nookling（小窝）：住在你电脑里的 AI 之家")
    parser.add_argument(
        "--data-dir", type=Path,
        help=f"配置和数据的存放目录（默认读取环境变量 {DATA_DIR_ENV}，未设置时为 {default_data_dir()}）",
    )
    parser.add_argument("--move-data-to", type=Path, metavar="PATH", help="把现有数据复制到新位置（原目录保留），之后改用新位置")
    parser.add_argument("--port", type=int, help="管理页面端口（默认 8787）")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开管理页面")
    parser.add_argument("--console", action="store_true", help="在终端里试聊，不连接 Telegram")
    parser.add_argument("--list-models", metavar="ID", help="列出某个 API 成员可用的模型（即使它未启用）")
    parser.add_argument("--import-config", type=Path, metavar="PATH", help="从旧版 config.toml 导入配置（会覆盖现有配置）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args()

    from .runtime import LogBuffer

    log_buffer = LogBuffer()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().addHandler(log_buffer)
    if not args.verbose:
        logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    try:
        data_dir = resolve_data_dir(args.data_dir)
        if args.move_data_to:
            move_data(data_dir, args.move_data_to)
            return
        store = SettingsStore(data_dir)
        notice = migrate(store, args.import_config)
        if args.list_models:
            asyncio.run(list_models(store, args.list_models))
        elif args.console:
            from .console import run_console

            logging.getLogger().setLevel(logging.WARNING)
            asyncio.run(run_console(store.config()))
        else:
            asyncio.run(serve(store, args.port, not args.no_browser, log_buffer, notice))
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        pass


def move_data(source: Path, target: Path) -> None:
    count = copy_data_dir(source, target)
    target = target.expanduser().resolve()
    print(f"已把 {count} 个文件从 {source} 复制到 {target}，并逐个校验了大小。")
    print("接下来：")
    print(f"  1. 把环境变量 {DATA_DIR_ENV} 设为 {target}，例如在 PowerShell 里运行：")
    print(f"     [Environment]::SetEnvironmentVariable('{DATA_DIR_ENV}', '{target}', 'User')")
    print("     然后重新打开终端；或者每次启动时加 --data-dir 参数。")
    print("  2. 用新位置启动，确认一切正常。")
    print(f"  3. 确认无误后再删除旧目录 {source}（里面有明文 token 和 key）。")


def migrate(store: SettingsStore, explicit: Path | None) -> str:
    """Import an old config.toml on first run; return a reminder if it is still lying around."""
    source = explicit or (LEGACY_CONFIG if not store.exists() and LEGACY_CONFIG.exists() else None)
    if source:
        settings, secrets = import_legacy_toml(source)
        _, existing = store.load()
        secrets["web_token"] = existing.get("web_token", "")
        store.save(settings, secrets)
        print(f"已把 {source} 导入到 {store.data_dir}")
    if LEGACY_CONFIG.exists():
        return (
            f"项目目录下的 {LEGACY_CONFIG.resolve()} 已经导入到 {store.data_dir}，"
            "它里面有明文 token 和 key，确认管理页面里的配置没问题后请删除它。"
        )
    return ""


async def serve(store: SettingsStore, port: int | None, open_browser: bool, log_buffer, notice: str) -> None:
    from .runtime import Runtime
    from .web.server import start_web

    runtime = Runtime(store, log_buffer)
    logging.getLogger().addHandler(runtime.events)
    if notice:
        runtime.notices.append(notice)
    port = port or runtime.settings["web"]["port"]
    runner, url = await start_web(runtime, port)
    print(f"\n  管理页面：{url}\n  （只能在本机打开，链接里的口令请勿外传）\n", flush=True)
    if open_browser and telegram_problems(runtime.config):
        webbrowser.open(url)
    try:
        await runtime.start()
        await asyncio.gather(runtime.telegram.run(), runtime.proactive_loop())
    finally:
        await runtime.shutdown()
        await runner.cleanup()


async def list_models(store: SettingsStore, participant_id: str) -> None:
    from .participants import ParticipantError
    from .runtime import Runtime

    runtime = Runtime(store)
    try:
        models = await runtime.list_models(participant_id)
    except ParticipantError as e:
        print(f"查询失败：{e}", file=sys.stderr)
        sys.exit(1)
    print(f"{participant_id} 可用的模型：")
    for m in models:
        print(f"  {m}")


if __name__ == "__main__":
    main()
