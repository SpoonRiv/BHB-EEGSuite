"""Installed module entry point. Acquisition remains owned by the host."""

from pathlib import Path


def create_module(state, acquire, release):
    from .library import MusicLibrary
    from .service import MusicService
    from .routes import create_music_router

    library = MusicLibrary(Path(__file__).parent / "resources")
    service = MusicService(state, library, acquire, release)
    return service, create_music_router(service)
