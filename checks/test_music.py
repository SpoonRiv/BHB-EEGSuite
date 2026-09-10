"""Music lifecycle checks with the real offline writer; never connects hardware.

Run: python -m unittest discover -s checks -p test_music.py -v
"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from optional_modules.music.library import MusicLibrary, parse_lyrics
from optional_modules.music.service import MusicService
from core.offline.offline_service import OfflineService


class MusicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "Musics").mkdir()
        (self.root / "Lyrics").mkdir()
        for name in ("A", "B"):
            (self.root / "Musics" / f"{name}.MP3").write_bytes(b"test")
        self.library = MusicLibrary(self.root)
        self.ids = [song["id"] for song in self.library.songs()]
        self.controller = Mock()
        self.controller.get_status.return_value = {"running": True, "last": {"type": "ready"}, "task_running": False}
        self.controller.send_trigger_command.return_value = True
        self.state = SimpleNamespace(controller=self.controller, streamer=SimpleNamespace(is_streaming=False), offline=self.offline(8))
        self.acquired = 0
        self.released = 0

        async def acquire():
            self.acquired += 1
            self.state.streamer.is_streaming = True
            return {"status": "success"}

        async def release():
            self.released += 1
            self.state.streamer.is_streaming = False
            self.state.offline.stop_session()
            return {"status": "success"}

        self.service = MusicService(self.state, self.library, acquire, release)

    def offline(self, channels):
        return OfflineService(str(self.root), "recordings", 500, [f"CH{i}" for i in range(channels)],
                              True, "TRIG", "uV", 120, 50, 30, 5, 3, 50, 32, "merge")

    async def asyncTearDown(self):
        if self.service.active:
            await self.service.stop()
        self.directory.cleanup()

    async def begin(self):
        response = await self.service.start(self.ids, "P001")
        self.service.on_chunk([[0] * 9])
        return response["token"]

    async def record(self, token, index, channels=8, samples=500):
        await self.service.start_trial(token, index)
        self.state.offline.append_chunk([[float(i)] * channels + [1] for i in range(samples)])
        await self.service.event(token, index, "playing", {"client_time_unix_ms": 12345, "audio_time_sec": 0})

    async def test_two_songs_preserve_channel_counts_and_export(self):
        for channels in (8, 16):
            with self.subTest(channels=channels):
                self.state.offline = self.offline(channels)
                token = await self.begin()
                for index in (0, 1):
                    await self.record(token, index, channels)
                    result = await self.service.finish_trial(token, index, {"audio_time_sec": 1})
                    self.assertEqual(result["session"]["total_samples"], 500)
                    self.assertEqual(len(result["session"]["channel_names"]), channels + 1)
                    self.assertFalse(result["errors"])
                    path = Path(result["session"]["session_dir"])
                    self.assertTrue((path / f"Category_{index+1}_{'AB'[index]}.csv").is_file())
                    trial = json.loads((path / "music_trial.json").read_text(encoding="utf-8"))
                    self.assertEqual(trial["subject"], "P001")
                    self.assertEqual([e["event"] for e in trial["events"]], ["recording_started", "playing", "ended"])
                    self.assertEqual(await self.service.finish_trial(token, index, {}), result)
                result = await self.service.stop(token, "completed")
                self.assertEqual(result["reason"], "completed")
                self.assertEqual(len(result["results"]), 2)
                self.assertEqual(await self.service.stop(token), result)
                self.assertFalse(self.service.active)

    async def test_prepare_cancel_creates_no_trial(self):
        token = await self.begin()
        result = await self.service.stop(token, "escape")
        self.assertEqual(result["results"], [])
        self.assertEqual(self.service.results_snapshot()["reason"], "escape")
        self.assertIsNone(self.state.offline.active_session_id)
        self.assertEqual(self.released, 1)

    async def test_partial_cancel_exports_and_clears_trigger(self):
        token = await self.begin()
        await self.record(token, 0, samples=75)
        result = await self.service.stop(token, "escape")
        self.assertEqual(result["results"][0]["session"]["total_samples"], 75)
        self.assertTrue(result["results"][0]["outputs"])
        self.assertEqual(self.service.results_snapshot()["results"][0]["song"]["name"], "A")
        self.controller.send_trigger_command.assert_called_with("end", "music")

    async def test_batch_export_creates_one_directory_with_ten_numbered_folders(self):
        token = await self.begin()
        for index in (0, 1):
            await self.record(token, index, samples=20)
            await self.service.finish_trial(token, index, {})
        await self.service.stop(token, "completed")

        exported = await self.service.export_all(["csv"])
        export_dir = Path(exported["export_dir"])
        self.assertTrue(export_dir.is_dir())
        self.assertEqual([path.name for path in sorted(export_dir.iterdir(), key=lambda path: int(path.name))], [str(i) for i in range(1, 11)])
        self.assertEqual(exported["missing_indexes"], list(range(3, 11)))
        self.assertTrue((export_dir / "1" / "Category_1_A.csv").is_file())
        self.assertTrue((export_dir / "2" / "Category_2_B.csv").is_file())
        self.assertEqual(len(exported["outputs"]), 2)

        saved_results = self.service.last_result["results"]
        self.service.last_result = None
        recovered = await self.service.export_all(["csv"], saved_results)
        self.assertEqual(len(recovered["outputs"]), 2)

    async def test_no_data_and_foreign_owner_rejected(self):
        response = await self.service.start(self.ids, "")
        with self.assertRaises(ValueError):
            await self.service.start_trial(response["token"], 0)
        with self.assertRaises(ValueError):
            await self.service.stop("x" * 32)
        with self.assertRaises(ValueError):
            await self.service.start(self.ids, "other")
        self.assertTrue(self.service.active)

    async def test_export_failure_retains_raw_and_reports_error(self):
        token = await self.begin()
        await self.record(token, 0)
        self.state.offline.export = Mock(side_effect=OSError("disk full"))
        result = await self.service.stop(token, "escape")
        saved = result["results"][0]
        self.assertIn("disk full", saved["errors"][0])
        self.assertTrue((Path(saved["session"]["session_dir"]) / "raw_float32.bin").is_file())
        self.assertFalse(self.state.streamer.is_streaming)

    async def test_heartbeat_remains_responsive_during_export(self):
        token = await self.begin()
        await self.record(token, 0)
        original = self.state.offline.export
        def slow_export(**kwargs):
            time.sleep(.2)
            return original(**kwargs)
        self.state.offline.export = slow_export
        saving = asyncio.create_task(self.service.finish_trial(token, 0, {}))
        await asyncio.sleep(.03)
        snap = await asyncio.wait_for(self.service.heartbeat(token), .05)
        self.assertTrue(snap["active"])
        await saving

    async def test_browser_loss_watchdog_preserves_partial_recording(self):
        token = await self.begin()
        await self.record(token, 0, samples=40)
        self.service.run["heartbeat"] = time.monotonic() - 13
        await asyncio.wait_for(self.service.watchdog, 3)
        self.assertFalse(self.service.active)
        self.assertEqual(self.service.last_result["reason"], "browser_lost")
        self.assertEqual(self.service.last_result["results"][0]["session"]["total_samples"], 40)

    async def test_device_loss_watchdog_stops_stream(self):
        await self.begin()
        self.controller.get_status.return_value = {"running": False, "last": {"type": "disconnected"}}
        await asyncio.wait_for(self.service.watchdog, 3)
        self.assertEqual(self.service.last_result["reason"], "device_lost")
        self.assertFalse(self.state.streamer.is_streaming)

    def test_lyrics_offsets_multiple_stamps_and_plain_text(self):
        result = parse_lyrics("[offset:-100]\n[00:01.20][00:02.345]hello\n[00:05]world")
        self.assertEqual([round(c["time"], 3) for c in result["cues"]], [1.1, 2.245, 4.9])
        self.assertEqual(parse_lyrics("plain lyrics")["cues"], [])
        self.assertEqual(parse_lyrics("plain lyrics")["text"], "plain lyrics")

    def test_catalogue_rejects_paths(self):
        with self.assertRaises(ValueError):
            self.library.audio_path("../../main.py")


if __name__ == "__main__":
    unittest.main()
