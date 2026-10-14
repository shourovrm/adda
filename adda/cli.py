"""`adda serve|seats|search|threads`."""

from __future__ import annotations

import argparse
import logging

from adda.config import load_settings
from adda.personas import load_personas
from adda.seats.registry import build_adapters
from adda.store import Store


def serve_command(arguments: argparse.Namespace) -> None:
    import uvicorn

    from adda.server import create_app

    settings = load_settings()
    host = arguments.host or settings.host
    port = arguments.port or settings.port
    uvicorn.run(create_app(settings), host=host, port=port)


def seats_command(arguments: argparse.Namespace) -> None:
    settings = load_settings()
    personas = load_personas(settings.personas_dir)
    if not personas:
        print(f"no personas in {settings.personas_dir}")
        return
    adapters = build_adapters(personas, settings.seat_workdir.resolve())
    for name, persona in personas.items():
        availability = "available" if adapters[name].available() else "NOT FOUND"
        model = persona.model or "default model"
        print(f"{name:<12} {persona.program:<9} {model:<24} {availability:<10} {persona.role}")


def search_command(arguments: argparse.Namespace) -> None:
    store = Store(load_settings().database_path)
    hits = store.search(arguments.query, limit=arguments.limit)
    if not hits:
        print("no matches")
    for hit in hits:
        message = hit.message
        print(f"[{message.thread_id}] {hit.thread_title} | {message.author}: {hit.snippet}")


def threads_command(arguments: argparse.Namespace) -> None:
    store = Store(load_settings().database_path)
    threads = store.list_threads()
    if not threads:
        print("no threads")
    for thread in threads:
        status = "ended" if thread.ended_at else "open"
        created = thread.created_at.strftime("%Y-%m-%d %H:%M")
        print(f"{thread.id}  {created}  {status:<5}  {thread.mode:<10}  {thread.title}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="adda", description="Local chat room for AI agents.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser("serve", help="run the server (default 127.0.0.1:8765)")
    serve.add_argument("--host", help="override ADDA_HOST")
    serve.add_argument("--port", type=int, help="override ADDA_PORT")
    serve.set_defaults(handler=serve_command)

    seats = subcommands.add_parser("seats", help="list seats and whether their program is found")
    seats.set_defaults(handler=seats_command)

    search = subcommands.add_parser("search", help="full-text search over all messages")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(handler=search_command)

    threads = subcommands.add_parser("threads", help="list threads, newest first")
    threads.set_defaults(handler=threads_command)
    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    arguments = build_parser().parse_args(argv)
    arguments.handler(arguments)
