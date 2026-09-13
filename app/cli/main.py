"""Installable entry point; argument parsing never initializes the agent runtime."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path


def _options(parser: argparse.ArgumentParser, *, child: bool = False) -> None:
    default = argparse.SUPPRESS if child else None
    parser.add_argument(
        "--config", type=Path, default=default, help="Explicit non-secret configuration file"
    )
    parser.add_argument(
        "--credential-store", choices=("keyring", "env", "session"), default=default
    )
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS if child else False
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentgate", description="Run guarded agents locally, with no application server"
    )
    _options(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("init", "Guided local setup"),
        ("doctor", "Safe setup diagnostics"),
        ("connect", "Authorize an integration"),
        ("disconnect", "Remove credentials"),
        ("run", "Run a foreground agent"),
        ("history", "List previous runs"),
        ("show", "Inspect a saved run"),
        ("setup", "Install optional runtime assets"),
        ("payments", "Explicit payment reconciliation"),
    ):
        command = commands.add_parser(name, help=description)
        _options(command, child=True)
        if name in ("connect", "disconnect"):
            command.add_argument(
                "provider", choices=("llm", "github", "gmail", "calendar", "stripe")
            )
        if name in ("init", "run"):
            command.add_argument(
                "--workspace", type=Path, help="Explicitly allowed filesystem root"
            )
            command.add_argument("--non-interactive", action="store_true")
        if name == "run":
            command.add_argument("prompt")
        elif name == "show":
            command.add_argument("run_id")
        elif name == "history":
            command.add_argument(
                "--limit", type=int, choices=range(1, 101), default=20, metavar="1..100"
            )
        elif name == "setup":
            command.add_argument("component", choices=("browser",))
        elif name == "payments":
            command.add_argument("operation", choices=("sync",))
    return parser


async def dispatch(args: argparse.Namespace, console) -> int:
    from app.cli import commands
    from app.runtime.config import LocalPaths

    paths = LocalPaths.resolve(args.config)
    if args.command == "init":
        return await commands.initialize(args, paths, console)
    if args.command in ("history", "show"):
        return commands.inspect_run(args, paths, console)
    config = commands.configured(args, paths)
    if args.command == "doctor":
        return await commands.diagnose(config, paths, console)
    if args.command == "connect":
        return await commands.connect(args.provider, config, paths, console)
    if args.command == "disconnect":
        return await commands.disconnect(args.provider, config, paths, console)
    if args.command == "setup":
        return await commands.setup_browser(config, paths, console)
    if args.command == "payments":
        return await commands.sync_payments(config, paths, console)
    return await commands.execute(args, config, paths, console)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from app.cli.console import Console, InteractionRequired, SafeFormatter
    from app.runtime.safety import Sanitizer

    console = Console(Sanitizer(), json_output=args.json)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SafeFormatter(console.sanitizer))
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    root.handlers, root.level = [handler], logging.WARNING
    try:
        return asyncio.run(dispatch(args, console))
    except InteractionRequired as exc:
        if args.json and not exc.reported:
            console.result(
                {"schema_version": 1, "type": "interaction_required", "message": str(exc)}
            )
        elif not exc.reported:
            console.say(str(exc))
        return 3
    except KeyboardInterrupt:
        console.say("Interrupted. Remote effects are not automatically undone or retried")
        return 130
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(console.sanitizer.text(f"Error: {exc}"), file=sys.stderr)
        return 2
    except Exception as exc:
        # Never dump an arbitrary provider response/exception containing secrets.
        print(
            f"Error: operation failed ({type(exc).__name__}); run 'agentgate doctor'",
            file=sys.stderr,
        )
        return 1
    finally:
        root.handlers, root.level = previous_handlers, previous_level
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
