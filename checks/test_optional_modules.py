"""Exercise real module installs and HTTP gates without connecting hardware."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from core.modules import OptionalModuleManager, create_module_router


class OptionalModuleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.callbacks = []
        self.state = SimpleNamespace(streamer=SimpleNamespace(
            add_callback=lambda cb: self.callbacks.append(cb),
            remove_callback=lambda cb: self.callbacks.remove(cb)))
        self.acquire = AsyncMock()
        self.release = AsyncMock()
        self.manager = self.make_manager()
        self.app = FastAPI()
        self.app.include_router(create_module_router(self.manager))
        self.app.mount('/api/music', self.manager)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')

    def make_manager(self):
        return OptionalModuleManager(self.root, self.state, self.acquire, self.release)

    def add_package(self):
        source = Path(__file__).resolve().parents[1] / 'optional_modules/music'
        shutil.copytree(source, self.manager.package_dir, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

    async def asyncTearDown(self):
        await self.client.aclose()
        self.manager._clear_imports()
        self.directory.cleanup()

    async def test_base_host_works_without_package_and_rejects_direct_access(self):
        self.manager.restore()
        info = (await self.client.get('/api/modules')).json()['modules'][0]
        self.assertFalse(info['installed'])
        self.assertFalse(info['available'])
        self.assertEqual(self.callbacks, [])
        self.assertNotIn(self.manager.namespace, sys.modules)
        for path in ('/api/music/library', '/api/music/results', '/api/modules/music/assets/music.js'):
            self.assertEqual((await self.client.get(path)).status_code, 404, path)
        self.assertEqual((await self.client.post('/api/music/start', json={'song_ids': ['anything']})).status_code, 404)
        self.assertEqual((await self.client.post('/api/modules/music/install')).status_code, 409)
        self.acquire.assert_not_awaited()

    async def test_install_restores_resources_and_gates_uninstall(self):
        self.add_package()
        self.assertFalse(self.manager.catalog()['modules'][0]['installed'])
        response = await self.client.post('/api/modules/music/install')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['modules'][0]['installed'])
        self.assertEqual(len(self.callbacks), 1)
        songs = (await self.client.get('/api/music/library')).json()['songs']
        self.assertEqual(len(songs), 10)
        self.assertEqual((await self.client.get('/api/music/lyrics/' + songs[0]['id'])).status_code, 200)
        self.assertEqual((await self.client.get('/api/music/audio/' + songs[0]['id'])).status_code, 200)
        for name in ('pages.html', 'music.js', 'api.js', 'music.css'):
            response = await self.client.get('/api/modules/music/assets/' + name)
            self.assertEqual(response.status_code, 200, name)
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual((await self.client.post('/api/modules/music/install')).status_code, 200)
        self.assertEqual(len(self.callbacks), 1, 'Repeat installation must not duplicate acquisition callbacks')

        raw = self.root / 'offlinedata/example/raw_float32.bin'
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b'recorded EEG')
        self.manager.service.run = {'token': 'running'}
        self.assertEqual((await self.client.post('/api/modules/music/uninstall')).status_code, 409)
        self.manager.service.run = None
        self.manager.requests = 1
        self.assertEqual((await self.client.post('/api/modules/music/uninstall')).status_code, 409)
        self.manager.requests = 0
        self.assertEqual((await self.client.post('/api/modules/music/uninstall')).status_code, 200)
        self.assertEqual(raw.read_bytes(), b'recorded EEG')
        self.assertFalse(self.manager.installed_dir.exists())
        self.assertEqual(self.callbacks, [])
        self.assertEqual((await self.client.get('/api/music/library')).status_code, 404)
        self.assertEqual((await self.client.get('/api/modules/music/assets/music.js')).status_code, 404)
        self.assertEqual((await self.client.post('/api/modules/music/install')).status_code, 200)
        self.assertEqual(len(self.callbacks), 1)
        self.acquire.assert_not_awaited()
        self.release.assert_not_awaited()

    async def test_restart_uses_installed_copy_without_distribution_package(self):
        self.add_package()
        await self.manager.install()
        source = self.manager.package_dir
        source.rename(source.with_name('music-removed'))
        restored = self.make_manager()
        try:
            restored.restore()
            self.assertTrue(restored.catalog()['modules'][0]['installed'])
            self.assertFalse(restored.catalog()['modules'][0]['available'])
            self.assertEqual(len(restored.service.library.songs()), 10)
            restored.restore()
            self.assertEqual(len(self.callbacks), 2)
        finally:
            restored._clear_imports()

    async def test_incomplete_and_unloadable_package_never_installed(self):
        self.add_package()
        css = self.manager.package_dir / 'web/music.css'
        css.unlink()
        response = await self.client.post('/api/modules/music/install')
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.manager.installed_dir.exists())
        css.write_text('/* repaired asset */')
        (self.manager.package_dir / '__init__.py').write_text('raise RuntimeError("invalid module")')
        with self.assertLogs(level='ERROR'):
            response = await self.client.post('/api/modules/music/install')
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.manager.installed_dir.exists())
        self.assertIsNone(self.manager.service)
        self.assertEqual(self.callbacks, [])

    async def test_damaged_installed_package_can_still_be_uninstalled(self):
        self.add_package()
        await self.manager.install()
        (self.manager.installed_dir / 'manifest.json').unlink()
        response = await self.client.get('/api/modules')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['modules'][0]['installed'])
        response = await self.client.post('/api/modules/music/uninstall')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['modules'][0]['installed'])

    async def test_resource_path_and_module_id_checks(self):
        self.add_package()
        await self.manager.install()
        for relative in ('../manifest.json', '../service.py', '../../music/service.py'):
            with self.assertRaises(Exception) as error:
                self.manager.asset(relative)
            self.assertEqual(error.exception.status_code, 404)
        self.assertEqual((await self.client.post('/api/modules/unknown/install')).status_code, 404)
        with self.assertRaises(ValueError):
            self.manager._remove_install_tree(self.root / 'offlinedata')


if __name__ == '__main__':
    unittest.main()
