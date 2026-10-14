"""FastAPI app: REST, one WebSocket per room, and the static pages (SPEC section 6)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from adda.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_KEYS_PATH,
    KEY_VARIABLES,
    ConfigError,
    Settings,
    load_settings,
    save_key,
    save_settings,
    settings_view,
)
from adda.harnesses import clear_model_cache, detect_harnesses
from adda.models import Harness, Mode, Program, Room, Seat, Thinking, seat_handle
from adda.personas import list_personas, load_persona
from adda.routers.base import Router
from adda.routers.heuristic import HeuristicRouter
from adda.routers.jev import JevRouter
from adda.scheduler import (
    AdapterBuilder,
    BudgetReachedError,
    RoomEndedError,
    Scheduler,
    UnknownSeatError,
)
from adda.seats.registry import build_adapter
from adda.store import NotFoundError, Store

logger = logging.getLogger(__name__)

STATIC_DIRECTORY = Path(__file__).resolve().parent.parent / "static"

# Errors that become a 4xx response (REST) or an `error` frame (WebSocket).
CLIENT_ERRORS = (
    ValueError,  # includes ConfigError, PersonaError and pydantic validation errors
    NotFoundError,
    UnknownSeatError,
    RoomEndedError,
    BudgetReachedError,
)
MAX_COPIES = 12
# Colours for seats whose persona does not supply one; picked by roster position.
SEAT_COLOR_PALETTE = ("#e5b800", "#4fa3d9", "#d9694f", "#7bc47f", "#b08be0", "#e08bb4", "#5bc5b8")


class NewSeat(BaseModel):
    name: str
    program: Program
    model: str | None = None
    thinking: Thinking = "default"
    persona: str | None = None
    instruction: str | None = None
    color: str | None = None
    copies: int = Field(default=1, ge=1, le=MAX_COPIES)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, name: str) -> str:
        if not name.strip():
            raise ValueError("seat name must not be empty")
        return name.strip()


class NewRoom(BaseModel):
    title: str
    folder: str | None = None
    mode: Mode | None = None
    liveliness: int | None = Field(default=None, ge=0, le=10)
    budget_usd: float | None = Field(default=None, ge=0)
    seats: list[NewSeat] = Field(default_factory=list)


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


def slugify_title(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    return slug or "room"


def expand_copies(new_seat: NewSeat) -> list[str]:
    """Names for one NewSeat: the name itself, or "Scout1".."ScoutN" for N copies."""
    if new_seat.copies == 1:
        return [new_seat.name]
    return [f"{new_seat.name}{number}" for number in range(1, new_seat.copies + 1)]


class Hub:
    """Everything the app shares: store, routers, one scheduler and socket set per room."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        router: Router | None,
        adapter_builder: AdapterBuilder,
        detect: Callable[[Settings], list[Harness]],
        environment: dict[str, str],
    ) -> None:
        self.settings = settings
        self.store = store
        self.router_is_injected = router is not None
        self.router = router if router is not None else build_router(settings)
        self.fallback_router = HeuristicRouter()
        self.adapter_builder = adapter_builder
        self.detect = detect
        self.environment = environment
        self.schedulers: dict[str, Scheduler] = {}
        self.connections: dict[str, set[WebSocket]] = {}
        self.background_tasks: set[asyncio.Task] = set()
        self.harness_cache: list[Harness] | None = None

    # Harnesses

    def scan_harnesses(self, rescan: bool = False) -> list[Harness]:
        """Blocking (runs `<program> --version`); call through asyncio.to_thread."""
        if rescan:
            clear_model_cache()
        if rescan or self.harness_cache is None:
            self.harness_cache = self.detect(self.settings)
        return self.harness_cache

    def harness_versions(self) -> dict[str, str | None]:
        """Versions from the last scan; empty until the first scan finished."""
        if self.harness_cache is None:
            return {}
        return {harness.program: harness.version for harness in self.harness_cache}

    # Rooms

    def scheduler(self, room_id: str) -> Scheduler:
        """The room's scheduler, created on first use; raises NotFoundError for no room."""
        self.store.get_room(room_id)
        if room_id not in self.schedulers:
            self.schedulers[room_id] = Scheduler(
                room_id=room_id,
                store=self.store,
                settings=self.settings,
                router=self.router,
                fallback_router=self.fallback_router,
                broadcast=lambda frame: self.broadcast(room_id, frame),
                adapter_builder=self.adapter_builder,
                harness_versions=self.harness_versions,
            )
        return self.schedulers[room_id]

    def room_view(self, room: Room) -> dict[str, Any]:
        described = self.scheduler(room.id).describe(room)
        return described.model_dump(mode="json")

    def create_room(self, body: NewRoom) -> Room:
        title = body.title.strip()
        if not title:
            raise ValueError("title must not be empty")
        seat_names = [name for new_seat in body.seats for name in expand_copies(new_seat)]
        self._check_new_seat_names(seat_names, existing=[])
        for new_seat in body.seats:
            self._check_persona(new_seat.persona)

        mode = body.mode or self.settings.mode
        liveliness = self.settings.liveliness if body.liveliness is None else body.liveliness
        budget_usd = self.settings.budget_usd if body.budget_usd is None else body.budget_usd
        if body.folder and body.folder.strip():
            folder = self._make_folder(Path(body.folder.strip()).expanduser())
            room = self.store.create_room(title, str(folder), mode, liveliness, budget_usd)
        else:
            room = self.store.create_room(title, "", mode, liveliness, budget_usd)
            try:
                folder = self._make_folder(
                    self.settings.rooms_dir / f"{slugify_title(title)}-{room.id}"
                )
            except ValueError:
                # The room exists already; mark it ended so it cannot be used half-made.
                self.store.end_room(room.id)
                raise
            self.store.update_room(room.id, folder=str(folder))
        for new_seat in body.seats:
            self._add_seats(room.id, new_seat)
        return self.store.get_room(room.id)

    @staticmethod
    def _make_folder(folder: Path) -> Path:
        if not folder.is_absolute():
            raise ValueError(f"folder must be an absolute path, got {str(folder)!r}")
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ValueError(f"cannot create folder {folder}: {error}") from error
        return folder

    def add_seats(self, room_id: str, new_seat: NewSeat) -> Room:
        room = self.store.get_room(room_id)
        self._check_new_seat_names(
            expand_copies(new_seat), existing=[seat.name for seat in room.seats]
        )
        self._check_persona(new_seat.persona)
        self._add_seats(room_id, new_seat)
        return self.store.get_room(room_id)

    def _add_seats(self, room_id: str, new_seat: NewSeat) -> list[Seat]:
        persona_color = None
        if new_seat.persona:
            persona_color = load_persona(self.settings.personas_dir, new_seat.persona).color
        added = []
        for name in expand_copies(new_seat):
            roster_size = len(self.store.get_room(room_id).seats)
            color = (
                new_seat.color
                or persona_color
                or SEAT_COLOR_PALETTE[roster_size % len(SEAT_COLOR_PALETTE)]
            )
            added.append(
                self.store.add_seat(
                    room_id,
                    name=name,
                    program=new_seat.program,
                    model=new_seat.model or None,
                    thinking=new_seat.thinking,
                    persona=new_seat.persona or None,
                    instruction=(new_seat.instruction or "").strip() or None,
                    color=color,
                )
            )
        return added

    @staticmethod
    def _check_new_seat_names(names: list[str], existing: list[str]) -> None:
        """Mentions use handles, so two names with the same handle would be ambiguous. Check
        the whole batch up front so a failure does not leave half the copies behind."""
        taken = {seat_handle(name) for name in existing}
        for name in names:
            handle = seat_handle(name)
            if handle in taken:
                raise ValueError(f"a seat named {name!r} already exists in this room")
            taken.add(handle)

    def _check_persona(self, slug: str | None) -> None:
        if slug:
            load_persona(self.settings.personas_dir, slug)  # PersonaError is a ValueError

    # Settings

    def update_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        """Save the TOML file and any pasted keys, reload, and tell the schedulers."""
        config_path = Path(self.environment.get("ADDA_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
        keys_path = Path(self.environment.get("ADDA_KEYS_FILE") or DEFAULT_KEYS_PATH).expanduser()
        pasted_keys = body.get("keys") or {}
        if not isinstance(pasted_keys, dict):
            raise ConfigError('keys must be an object like {"openrouter": "sk-..."}')
        for key_name in pasted_keys:
            if key_name not in KEY_VARIABLES:
                raise ConfigError(
                    f"unknown key {key_name!r}; expected one of {sorted(KEY_VARIABLES)}"
                )

        save_settings(body, config_path)
        for key_name, value in pasted_keys.items():
            # The settings view marks keys as booleans; only a pasted string is a new value.
            if isinstance(value, str) and value.strip():
                save_key(key_name, value, keys_path)

        self.apply_settings(load_settings(self.environment, config_path))
        return settings_view(self.settings)

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        if not self.router_is_injected:
            self.router = build_router(settings)
        self.harness_cache = None
        for scheduler in self.schedulers.values():
            scheduler.update_settings(
                settings, self.router if not self.router_is_injected else None
            )

    # Sockets

    async def broadcast(self, room_id: str, frame: dict[str, Any]) -> None:
        for websocket in list(self.connections.get(room_id, set())):
            try:
                await websocket.send_json(frame)
            except (WebSocketDisconnect, RuntimeError, OSError) as error:
                # A tab closed mid-send; drop it so the other tabs keep receiving.
                logger.info("dropping websocket for room %s: %s", room_id, error)
                self.connections[room_id].discard(websocket)

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
    router: Router | None = None,
    adapter_builder: AdapterBuilder = build_adapter,
    detect: Callable[[Settings], list[Harness]] = detect_harnesses,
    environment: dict[str, str] | None = None,
    static_directory: Path = STATIC_DIRECTORY,
) -> FastAPI:
    """Build the app. Tests inject fakes; `adda serve` passes nothing and loads from config.

    `environment` is where ADDA_CONFIG and ADDA_KEYS_FILE are read when settings are saved and
    reloaded; it defaults to the process environment.
    """
    if environment is None:
        environment = dict(os.environ)
    if settings is None:
        settings = load_settings(environment)
    if store is None:
        store = Store(settings.database_path)
    hub = Hub(settings, store, router, adapter_builder, detect, environment)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Scan in the background so a slow `opencode models` cannot delay start-up.
        hub.run_in_background(asyncio.to_thread(hub.scan_harnesses))
        yield
        await hub.shutdown()

    app = FastAPI(title="Adda", lifespan=lifespan)
    app.state.hub = hub
    app.mount("/static", StaticFiles(directory=static_directory, check_dir=False), name="static")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'] if part != 'body')}: {item['msg']}"
            for item in error.errors()
        )
        return JSONResponse({"detail": problems}, status_code=400)

    @app.exception_handler(NotFoundError)
    @app.exception_handler(UnknownSeatError)
    async def not_found(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error.args[0] if error.args else error)}, 404)

    @app.exception_handler(RoomEndedError)
    @app.exception_handler(BudgetReachedError)
    async def conflict(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, 409)

    @app.exception_handler(ValueError)
    async def bad_request(request: Request, error: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, 400)

    # Pages

    def page(file_name: str) -> FileResponse:
        page_file = static_directory / file_name
        if not page_file.is_file():
            raise HTTPException(404, f"{page_file} not found; the UI is not built yet")
        return FileResponse(page_file)

    @app.get("/")
    async def rooms_page() -> FileResponse:
        return page("index.html")

    @app.get("/room/{room_id}")
    async def room_page(room_id: str) -> FileResponse:
        return page("room.html")

    @app.get("/settings")
    async def settings_page() -> FileResponse:
        return page("settings.html")

    # Library and settings

    @app.get("/api/harnesses")
    async def get_harnesses(rescan: bool = False) -> list[dict]:
        harnesses = await asyncio.to_thread(hub.scan_harnesses, rescan)
        return [harness.model_dump(mode="json") for harness in harnesses]

    @app.get("/api/personas")
    async def get_personas() -> list[dict]:
        return [
            persona.model_dump(mode="json") for persona in list_personas(hub.settings.personas_dir)
        ]

    @app.get("/api/settings")
    async def get_settings() -> dict:
        return settings_view(hub.settings)

    @app.put("/api/settings")
    async def put_settings(body: dict[str, Any]) -> dict:
        return hub.update_settings(body)

    # Rooms

    @app.get("/api/rooms")
    async def list_rooms() -> list[dict]:
        return [hub.room_view(room) for room in store.list_rooms()]

    @app.post("/api/rooms")
    async def create_room(body: NewRoom) -> dict:
        return hub.room_view(hub.create_room(body))

    # Registered before /api/rooms/{room_id}, which would otherwise swallow "last-seats".
    @app.get("/api/rooms/last-seats")
    async def last_seats() -> list[dict]:
        return [
            {
                "name": seat.name,
                "program": seat.program,
                "model": seat.model,
                "thinking": seat.thinking,
                "persona": seat.persona,
                "instruction": seat.instruction,
                "color": seat.color,
            }
            for seat in store.last_room_seats()
        ]

    @app.get("/api/rooms/{room_id}")
    async def get_room(room_id: str) -> dict:
        room = hub.scheduler(room_id).current_room()
        messages = store.list_messages(room_id)
        return {
            "room": room.model_dump(mode="json"),
            "messages": [message.model_dump(mode="json") for message in messages],
        }

    @app.patch("/api/rooms/{room_id}")
    async def patch_room(room_id: str, body: dict[str, Any]) -> dict:
        room = await hub.scheduler(room_id).apply_room_patch(body)
        return room.model_dump(mode="json")

    @app.post("/api/rooms/{room_id}/seats")
    async def add_seat(room_id: str, body: NewSeat) -> dict:
        scheduler = hub.scheduler(room_id)
        hub.add_seats(room_id, body)
        return (await scheduler.publish_room()).model_dump(mode="json")

    @app.patch("/api/rooms/{room_id}/seats/{seat_id}")
    async def patch_seat(room_id: str, seat_id: str, body: dict[str, Any]) -> dict:
        room = await hub.scheduler(room_id).apply_seat_patch(seat_id, body)
        return room.model_dump(mode="json")

    @app.delete("/api/rooms/{room_id}/seats/{seat_id}")
    async def delete_seat(room_id: str, seat_id: str) -> dict:
        room = await hub.scheduler(room_id).remove_seat(seat_id)
        return room.model_dump(mode="json")

    @app.post("/api/rooms/{room_id}/end")
    async def end_room(room_id: str) -> dict:
        scheduler = hub.scheduler(room_id)
        notes = await scheduler.end()
        return {"room": scheduler.current_room().model_dump(mode="json"), "notes": notes}

    @app.get("/api/search")
    async def search(q: str = "") -> list[dict]:
        return [
            {
                "message": hit.message.model_dump(mode="json"),
                "room_title": hit.room_title,
                "snippet": hit.snippet,
            }
            for hit in store.search(q)
        ]

    # WebSocket

    @app.websocket("/ws/{room_id}")
    async def room_socket(websocket: WebSocket, room_id: str) -> None:
        await websocket.accept()
        try:
            scheduler = hub.scheduler(room_id)
        except NotFoundError as error:
            await websocket.send_json({"type": "error", "text": str(error)})
            await websocket.close(code=4404)
            return
        hub.connections.setdefault(room_id, set()).add(websocket)
        try:
            await websocket.send_json(
                {
                    "type": "hello",
                    "room": scheduler.current_room().model_dump(mode="json"),
                    "messages": [
                        message.model_dump(mode="json") for message in store.list_messages(room_id)
                    ],
                    "owner": {"name": hub.settings.owner_name},
                    "router": hub.router.name,
                }
            )
            while True:
                raw_text = await websocket.receive_text()
                await handle_frame(hub, scheduler, websocket, raw_text)
        except WebSocketDisconnect:
            logger.info("websocket closed for room %s", room_id)
        finally:
            hub.connections.get(room_id, set()).discard(websocket)

    return app


async def handle_frame(hub: Hub, scheduler: Scheduler, websocket: WebSocket, raw_text: str) -> None:
    """Apply one client frame; problems go back to the sending tab as an `error` frame."""
    try:
        frame = json.loads(raw_text)
        if not isinstance(frame, dict):
            raise ValueError("a frame must be a JSON object")
        await apply_frame(hub, scheduler, websocket, frame)
    except CLIENT_ERRORS as error:
        text = str(error.args[0]) if isinstance(error, LookupError) and error.args else str(error)
        await websocket.send_json({"type": "error", "text": text})


async def apply_frame(
    hub: Hub, scheduler: Scheduler, websocket: WebSocket, frame: dict[str, Any]
) -> None:
    frame_type = frame.get("type")
    if frame_type == "say":
        text = str(frame.get("text") or "").strip()
        if not text:
            raise ValueError("say needs non-empty text")
        await scheduler.say(text)
    elif frame_type == "wake":
        await scheduler.wake(str(frame.get("seat") or ""))
    elif frame_type == "wake_all":
        await scheduler.wake_all()
    elif frame_type == "stop":
        await scheduler.stop()
    elif frame_type == "end":
        # Ending runs one more turn per seat; do it in the background so this socket can
        # still receive frames meanwhile.
        hub.run_in_background(end_and_report(scheduler, websocket))
    elif frame_type == "room":
        await scheduler.apply_room_patch(_patch_of(frame))
    elif frame_type == "seat":
        await scheduler.apply_seat_patch(str(frame.get("seat") or ""), _patch_of(frame))
    else:
        raise ValueError(f"unknown frame type {frame_type!r}")


def _patch_of(frame: dict[str, Any]) -> dict[str, Any]:
    patch = frame.get("patch")
    if not isinstance(patch, dict):
        raise ValueError("this frame needs a 'patch' object")
    return patch


async def end_and_report(scheduler: Scheduler, websocket: WebSocket) -> None:
    try:
        await scheduler.end()
    except CLIENT_ERRORS as error:
        await websocket.send_json({"type": "error", "text": str(error)})
