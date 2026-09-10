"""HTTP transport for the music paradigm; no independent BLE or Qt stack."""

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


class StartRequest(BaseModel):
    song_ids: List[str] = Field(min_length=1, max_length=200)
    subject: str = Field(default="", max_length=80)


class TokenRequest(BaseModel):
    token: str = Field(min_length=32, max_length=32)


class TimedRequest(TokenRequest):
    client_time_unix_ms: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    client_monotonic_ms: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    audio_time_sec: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)

    def timing(self):
        return self.model_dump(exclude={"token", "index", "event", "reason"}, exclude_none=True)


class TrialRequest(TimedRequest):
    index: int = Field(ge=0, le=199)


class EventRequest(TrialRequest):
    event: Literal["playing", "waiting"]


class StopRequest(TimedRequest):
    reason: Literal["completed", "stopped", "escape", "page_left", "playback_error", "request_error"] = "stopped"


class ExportRequest(BaseModel):
    formats: List[Literal["csv", "edf"]] = Field(default_factory=lambda: ["csv", "edf"], min_length=1, max_length=2)
    results: List[Dict[str, Any]] = Field(default_factory=list, max_length=10)


def create_music_router(service):
    router = APIRouter(tags=["music"])

    async def invoke(awaitable):
        try:
            return await awaitable
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/library")
    async def library():
        return {"songs": service.library.songs()}

    @router.get("/audio/{song_id}")
    async def audio(song_id: str):
        try:
            return FileResponse(service.library.audio_path(song_id))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/lyrics/{song_id}")
    async def lyrics(song_id: str):
        try:
            return service.library.lyrics(song_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/results")
    async def results():
        return service.results_snapshot()

    @router.post("/export")
    async def export(req: ExportRequest):
        return await invoke(service.export_all(req.formats, req.results))

    @router.post("/open-export-folder")
    async def open_export_folder():
        try:
            return {"status": "success", "opened_dir": service.open_export_folder()}
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(500, str(exc)) from exc

    @router.post("/start")
    async def start(req: StartRequest):
        return await invoke(service.start(req.song_ids, req.subject.strip()))

    @router.post("/heartbeat")
    async def heartbeat(req: TokenRequest):
        return await invoke(service.heartbeat(req.token))

    @router.post("/trial/start")
    async def start_trial(req: TrialRequest):
        return await invoke(service.start_trial(req.token, req.index))

    @router.post("/event")
    async def event(req: EventRequest):
        return await invoke(service.event(req.token, req.index, req.event, req.timing()))

    @router.post("/trial/stop")
    async def finish_trial(req: TrialRequest):
        return await invoke(service.finish_trial(req.token, req.index, req.timing()))

    @router.post("/stop")
    async def stop(req: StopRequest):
        return await invoke(service.stop(req.token, req.reason, req.timing()))

    return router
