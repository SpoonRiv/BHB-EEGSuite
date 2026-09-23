"""Local optional modules: atomic installation, lazy loading and gated assets.

The base application does not import or serve an optional package before it is
installed. Packages are distributed separately under optional_modules/<id>.
"""

import asyncio
import importlib.util
import json
import logging
import shutil
import sys
import uuid
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse


class OptionalModuleManager:
    MODULE_ID = "music"
    NAME = "文本-音频多模态范式"
    REQUIRED = ("__init__.py", "manifest.json", "library.py", "service.py", "routes.py",
                "web/pages.html", "web/music.js", "web/api.js", "web/music.css")

    def __init__(self, project_root, state, acquire, release):
        self.project_root = Path(project_root).resolve()
        self.package_dir = self.project_root / "optional_modules" / self.MODULE_ID
        self.install_root = self.project_root / "module_data"
        self.installed_dir = self.install_root / self.MODULE_ID
        self.state = state
        self.acquire = acquire
        self.release = release
        self.service = None
        self.application = None
        self.lock = asyncio.Lock()
        self.requests = 0
        self.error = ""
        self.manifest = {}
        self.namespace = f"_eegsuite_module_{uuid.uuid4().hex}"

    @property
    def active(self):
        return bool(self.service and self.service.active)

    def snapshot(self):
        return {"installed": self.service is not None,
                **(self.service.snapshot() if self.service else {"active": False})}

    async def stop(self, **kwargs):
        if self.service:
            return await self.service.stop(**kwargs)
        return {"active": False, "results": []}

    def _validate_package(self, directory):
        directory = Path(directory)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("id") != self.MODULE_ID or manifest.get("host_api") != 1:
            raise ValueError("模块安装包不兼容当前上位机")
        for relative in self.REQUIRED:
            path = directory / relative
            if not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError(f"模块安装包缺少文件：{relative}")
        if not (directory / "resources" / "Musics").is_dir() or not (directory / "resources" / "Lyrics").is_dir():
            raise ValueError("模块安装包缺少音频资源")
        return manifest

    def catalog(self):
        package = {}
        available = False
        try:
            package = self._validate_package(self.package_dir)
            available = True
        except (OSError, ValueError):
            pass
        installed = self.service is not None
        if installed:
            package = self.manifest
        return {"modules": [{
            "id": self.MODULE_ID, "name": self.NAME,
            "description": "文本与音频同步呈现，复用上位机脑电采集，支持逐项记录和批量导出。",
            "installed": installed, "available": available,
            "busy": self.active or self.requests > 0 or self.lock.locked(),
            "error": self.error,
        }]}

    def _clear_imports(self):
        for name in list(sys.modules):
            if name == self.namespace or name.startswith(self.namespace + "."):
                sys.modules.pop(name, None)

    def _load(self):
        manifest = self._validate_package(self.installed_dir)
        spec = importlib.util.spec_from_file_location(
            self.namespace, self.installed_dir / "__init__.py",
            submodule_search_locations=[str(self.installed_dir)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[self.namespace] = package
        try:
            spec.loader.exec_module(package)
            service, router = package.create_module(self.state, self.acquire, self.release)
            application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
            application.include_router(router)
            self.state.streamer.add_callback(service.on_chunk)
        except Exception:
            self._clear_imports()
            raise
        self.service = service
        self.application = application
        self.manifest = manifest
        self.error = ""

    def restore(self):
        if self.service or not self.installed_dir.is_dir():
            return
        try:
            self._load()
        except Exception as exc:
            self.error = f"模块加载失败，请重新安装：{exc}"
            logging.exception("Optional module could not be restored")

    def _remove_install_tree(self, path):
        # Only private runtime package directories may be removed. Experiment
        # recordings live under the independent offline root and are never here.
        path = Path(path)
        root = self.install_root.resolve()
        resolved = path.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError("拒绝操作模块安装目录之外的文件")
        if path.is_symlink():
            raise ValueError("模块目录不能是符号链接")
        if path.exists():
            shutil.rmtree(path)

    def _copy_package(self, staging):
        self._validate_package(self.package_dir)
        for path in self.package_dir.rglob("*"):
            if path.is_symlink() or not path.resolve().is_relative_to(self.package_dir.resolve()):
                raise ValueError("模块安装包不能包含外部链接")
        shutil.copytree(self.package_dir, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self._validate_package(staging)

    async def install(self):
        async with self.lock:
            if self.service:
                return self.catalog()
            if not self.package_dir.is_dir():
                raise ValueError("未找到安装包，请将 music 模块放入 optional_modules 目录后重试")
            self.install_root.mkdir(parents=True, exist_ok=True)
            staging = self.install_root / f".music-install-{uuid.uuid4().hex}"
            backup = self.install_root / f".music-backup-{uuid.uuid4().hex}"
            moved = False
            try:
                await asyncio.to_thread(self._copy_package, staging)
                if self.installed_dir.exists():
                    self.installed_dir.rename(backup)
                staging.rename(self.installed_dir)
                moved = True
                self._load()
            except Exception:
                if moved:
                    self._remove_install_tree(self.installed_dir)
                if backup.exists():
                    backup.rename(self.installed_dir)
                raise
            finally:
                self._remove_install_tree(staging)
            self._remove_install_tree(backup)
        return self.catalog()

    async def uninstall(self):
        async with self.lock:
            if self.active or self.requests:
                raise ValueError("模块正在运行或导出，请结束后再卸载")
            # Rename first, so a file-lock failure keeps the live module intact.
            retired = self.install_root / f".music-uninstall-{uuid.uuid4().hex}"
            if self.installed_dir.exists():
                self.installed_dir.rename(retired)
            if self.service:
                self.state.streamer.remove_callback(self.service.on_chunk)
            self.service = self.application = None
            self.manifest = {}
            self._clear_imports()
            self.error = ""
            if retired.exists():
                await asyncio.to_thread(self._remove_install_tree, retired)
        return self.catalog()

    def asset(self, relative):
        if not self.service:
            raise HTTPException(404, "模块尚未安装")
        root = (self.installed_dir / "web").resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "模块资源不存在")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    async def __call__(self, scope, receive, send):
        if not self.application:
            await JSONResponse({"detail": "请先在模块管理中安装文本-音频多模态范式"}, status_code=404)(scope, receive, send)
            return
        self.requests += 1
        try:
            await self.application(scope, receive, send)
        finally:
            self.requests -= 1


def create_module_router(manager):
    router = APIRouter(prefix="/api/modules", tags=["modules"])

    def check_id(module_id):
        if module_id != manager.MODULE_ID:
            raise HTTPException(404, "模块不存在")

    @router.get("")
    async def catalog():
        return manager.catalog()

    @router.post("/{module_id}/install")
    async def install(module_id: str):
        check_id(module_id)
        try:
            return await manager.install()
        except (ValueError, OSError) as exc:
            raise HTTPException(409, f"安装失败：{exc}") from exc
        except Exception as exc:
            logging.exception("Optional module installation failed")
            raise HTTPException(409, "模块加载失败，请检查安装包后重试") from exc

    @router.post("/{module_id}/uninstall")
    async def uninstall(module_id: str):
        check_id(module_id)
        try:
            return await manager.uninstall()
        except (ValueError, OSError) as exc:
            raise HTTPException(409, f"卸载失败：{exc}") from exc

    @router.get("/{module_id}/assets/{relative:path}")
    async def asset(module_id: str, relative: str):
        check_id(module_id)
        return manager.asset(relative)

    return router
