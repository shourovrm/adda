"""FastAPI app: REST, one WebSocket per thread, and the static UI (SPEC section 6)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from adda.config import Settings, load_settings
from adda.models import Seat, ThreadMode
from adda.personas import Persona, load_personas
from adda.routers.base import Router
from adda.routers.heuristic import HeuristicRouter
from adda.routers.jev import JevRouter
from adda.scheduler import Scheduler, ThreadEndedError, UnknownSeatError
from adda.seats.base import SeatAdapter
from adda.seats.registry import build_adapters
from adda.store import NotFoundError, Store

logger = logging.getLogger(__name__)

STATIC_DIRECTORY = Path(__file__).resolve().parent.parent / "static"


class CreateThreadBody(BaseModel):
    title: str
    mode: ThreadMode = "bidding"


class ModeBody(BaseModel):
    mode: ThreadMode


def build_router(settings: Settings) -> Router:
    if settings.router == "heuristic":
        return HeuristicRouter()
    if not settings.jev_api_key:
        # The server must boot without keys; the heuristic needs no network.
        logger.warning(
            "no API key for Jev provider %s; falling back to the heuristic router",
            settings.jev_provider,
        )
        return HeuristicRouter()
    return JevRouter(
        provider=settings.jev_provider, model=settings.jev_model, api_key=settings.jev_api_key
    )


class Room:
    """Everything the app shares: store, seats, routers, one scheduler and socket set per
    thread."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        personas: dict[str, Persona],
        adapters: dict[str, SeatAdapter],
        router: Router,
    ) -> None:
        self.settings = settings
        self.store = store
        self.personas = personas
        self.adapters = adapters
        self.router = router
        self.fallback_router = HeuristicRouter()
        self.schedulers: dict[str, Scheduler] = {}
        self.connections: dict[str, set[WebSocket]] = {}
        self.background_tasks: set[asyncio.Task] = set()

    def seats(self) -> list[Seat]:
        seats = []
        for name, persona in self.personas.items():
            adapter = self.adapters.get(name)
            seats.append(
                Seat(
                    name=name,
                    program=persona.program,
                    model=persona.model,
                    color=persona.color,
                    role=persona.role,
                    available=adapter is not None and adapter.available(),
                )
            )
        return seats

    def scheduler(self, thread_id: str) -> Scheduler:
        """The thread's scheduler, created on first use; raises NotFoundError for no thread."""
        self.store.get_thread(thread_id)
        if thread_id not in self.schedulers:
            self.schedulers[thread_id] = Scheduler(
                thread_id=thread_id,
                store=self.store,
                settings=self.settings,
                seats=self.seats(),
                adapters=self.adapters,
                personas=self.personas,
                router=self.router,
                fallback_router=self.fallback_router,
                broadcast=lambda frame: self.broadcast(thread_id, frame),
            )
        return self.schedulers[thread_id]

    async def broadcast(self, thread_id: str, frame: dict[str, Any]) -> None:
        for websocket in list(self.connections.get(thread_id, set())):
            try:
                await websocket.send_json(frame)
            except (WebSocketDisconnect, RuntimeError, OSError) as error:
                # A tab closed mid-send; drop it so the other tabs keep receiving.
                logger.info("dropping websocket for thread %s: %s", thread_id, error)
                self.connections[thread_id].discard(websocket)

    def run_in_background(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    async def shutdown(self) -> None:
        for scheduler in self.schedulers.values():
            await scheduler.stop()
        for task in list(self.background_tasks):
            task.cancel()


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    personas: dict[str, Persona] | None = None,
    adapters: dict[str, SeatAdapter] | None = None,
    router: Router | None = None,
    static_directory: Path = STATIC_DIRECTORY,
) -> FastAPI:
    """Build the app. Tests inject fakes; `adda serve` passes nothing and loads from config."""
    if settings is None:
        settings = load_settings()
    if store is None:
        store = Store(settings.database_path)
    if personas is None:
        personas = load_personas(settings.personas_dir)
        if not personas:
            logger.warning("no personas found in %s; the room has no seats", settings.personas_dir)
    if adapters is None:
        adapters = build_adapters(personas, settings.seat_workdir.resolve())
    if router is None:
        router = build_router(settings)
    room = Room(settings, store, personas, adapters, router)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await room.shutdown()

    app = FastAPI(title="Adda", lifespan=lifespan)
    app.state.room = room
    app.mount("/static", StaticFiles(directory=static_directory, check_dir=False), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        index_file = static_directory / "index.html"
        if not index_file.is_file():
            raise HTTPException(404, f"{index_file} not found; the UI is not built yet")
        return FileResponse(index_file)

    @app.get("/api/seats")
    async def list_seats() -> list[dict]:
        return [seat.model_dump(mode="json") for seat in room.seats()]

    @app.get("/api/threads")
    async def list_threads() -> list[dict]:
        return [thread.model_dump(mode="json") for thread in store.list_threads()]

    @app.post("/api/threads")
    async def create_thread(body: CreateThreadBody) -> dict:
        title = body.title.strip()
        if not title:
            raise HTTPException(422, "title must not be empty")
        return store.create_thread(title, body.mode).model_dump(mode="json")

    @app.get("/api/threads/{thread_id}")
    async def get_thread(thread_id: str) -> dict:
        thread = _thread_or_404(store, thread_id)
        messages = store.list_messages(thread_id)
        return {
            "thread": thread.model_dump(mode="json"),
            "messages": [message.model_dump(mode="json") for message in messages],
        }

    @app.post("/api/threads/{thread_id}/mode")
    async def set_mode(thread_id: str, body: ModeBody) -> dict:
        _thread_or_404(store, thread_id)
        thread = await room.scheduler(thread_id).set_mode(body.mode)
        return thread.model_dump(mode="json")

    @app.post("/api/threads/{thread_id}/end")
    async def end_thread(thread_id: str) -> dict:
        _thread_or_404(store, thread_id)
        try:
            notes = await room.scheduler(thread_id).end()
        except ThreadEndedError as error:
            raise HTTPException(409, str(error)) from error
        thread = store.get_thread(thread_id)
        return {"thread": thread.model_dump(mode="json"), "notes": notes}

    @app.get("/api/search")
    async def search(q: str = "") -> list[dict]:
        return [
            {
                "message": hit.message.model_dump(mode="json"),
                "thread_title": hit.thread_title,
                "snippet": hit.snippet,
            }
            for hit in store.search(q)
        ]

    @app.websocket("/ws/{thread_id}")
    async def thread_socket(websocket: WebSocket, thread_id: str) -> None:
        await websocket.accept()
        try:
            thread = store.get_thread(thread_id)
        except NotFoundError as error:
            await websocket.send_json({"type": "error", "text": str(error)})
            await websocket.close(code=4404)
            return
        room.connections.setdefault(thread_id, set()).add(websocket)
        try:
            await websocket.send_json(
                {
                    "type": "hello",
                    "thread": thread.model_dump(mode="json"),
                    "seats": [seat.model_dump(mode="json") for seat in room.seats()],
                    "messages": [
                        message.model_dump(mode="json")
                        for message in store.list_messages(thread_id)
                    ],
                }
            )
            while True:
                frame = await websocket.receive_json()
                await _handle_frame(room, thread_id, websocket, frame)
        except WebSocketDisconnect:
            logger.info("websocket closed for thread %s", thread_id)
        finally:
            room.connections.get(thread_id, set()).discard(websocket)

    return app


def _thread_or_404(store: Store, thread_id: str):
    try:
        return store.get_thread(thread_id)
    except NotFoundError as error:
        raise HTTPException(404, str(error)) from error


async def _handle_frame(room: Room, thread_id: str, websocket: WebSocket, frame: Any) -> None:
    """Apply one client frame; problems go back to the sending tab as an `error` frame."""
    scheduler = room.scheduler(thread_id)
    frame_type = frame.get("type") if isinstance(frame, dict) else None
    try:
        if frame_type == "say":
            text = str(frame.get("text") or "").strip()
            if not text:
                raise ValueError("say needs non-empty text")
            await scheduler.say(text)
        elif frame_type == "wake":
            await scheduler.wake(str(frame.get("seat") or ""))
        elif frame_type == "mode":
            await scheduler.set_mode(str(frame.get("mode") or ""))
        elif frame_type == "stop":
            await scheduler.stop()
        elif frame_type == "end":
            # Ending runs one more turn per seat; do it in the background so this socket can
            # still receive frames meanwhile.
            room.run_in_background(_end_and_report(scheduler, websocket))
        else:
            raise ValueError(f"unknown frame type {frame_type!r}")
    except (ValueError, ThreadEndedError, UnknownSeatError) as error:
        await websocket.send_json({"type": "error", "text": str(error)})


async def _end_and_report(scheduler: Scheduler, websocket: WebSocket) -> None:
    try:
        await scheduler.end()
    except ThreadEndedError as error:
        await websocket.send_json({"type": "error", "text": str(error)})
