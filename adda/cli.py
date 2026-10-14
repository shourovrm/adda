"""`adda serve|rooms|search|harnesses`."""

from __future__ import annotations

import argparse
import logging

from adda.config import load_settings
from adda.harnesses import detect_harnesses
from adda.store import Store


def serve_command(arguments: argparse.Namespace) -> None:
    import uvicorn

    from adda.server import create_app

    settings = load_settings()
    host = arguments.host or settings.host
    port = arguments.port or settings.port
    uvicorn.run(create_app(settings), host=host, port=port)


def rooms_command(arguments: argparse.Namespace) -> None:
    store = Store(load_settings().database_path)
    rooms = store.list_rooms()
    if not rooms:
        print("no rooms")
    for room in rooms:
        status = "ended" if room.ended_at else "open"
        created = room.created_at.strftime("%Y-%m-%d %H:%M")
        budget = f"${room.spent_usd:.2f}/${room.budget_usd:.2f}" if room.budget_usd else "no budget"
        seat_names = ", ".join(seat.name for seat in room.seats) or "no seats"
        print(
            f"{room.id}  {created}  {status:<5}  {room.mode:<10}  L{room.liveliness:<2}  "
            f"{budget:<14}  {room.title}  [{seat_names}]"
        )


def search_command(arguments: argparse.Namespace) -> None:
    store = Store(load_settings().database_path)
    hits = store.search(arguments.query, limit=arguments.limit)
    if not hits:
        print("no matches")
    for hit in hits:
        message = hit.message
        print(f"[{message.room_id}] {hit.room_title} | {message.author}: {hit.snippet}")


def harnesses_command(arguments: argparse.Namespace) -> None:
    settings = load_settings()
    print(f"{'program':<10} {'found':<6} {'version':<12} {'default model':<26} path")
    for harness in detect_harnesses(settings):
        found = "yes" if harness.found else "NO"
        enabled = "" if harness.enabled else "  (disabled in settings)"
        print(
            f"{harness.program:<10} {found:<6} {harness.version or '-':<12} "
            f"{harness.default_model or '-':<26} {harness.path or harness.login_hint}{enabled}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="adda", description="Local chat room for AI agents.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser("serve", help="run the server (default 127.0.0.1:8765)")
    serve.add_argument("--host", help="override ADDA_HOST")
    serve.add_argument("--port", type=int, help="override ADDA_PORT")
    serve.set_defaults(handler=serve_command)

    rooms = subcommands.add_parser("rooms", help="list rooms, newest first")
    rooms.set_defaults(handler=rooms_command)

    search = subcommands.add_parser("search", help="full-text search over all messages")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(handler=search_command)

    harnesses = subcommands.add_parser("harnesses", help="show which agent programs are installed")
    harnesses.set_defaults(handler=harnesses_command)
    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    arguments = build_parser().parse_args(argv)
    arguments.handler(arguments)
