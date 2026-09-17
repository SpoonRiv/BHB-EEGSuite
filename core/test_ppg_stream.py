"""PPG stream regression checks; no Bluetooth hardware is required."""

import asyncio
import queue
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from core.ble.acquisition_process import _connect_and_stream
from core.ble.test_frame_parser import _frame
from core.eeg_controller import EEGController
from core.ppg_stream import PpgBuffer


CONFIG = str(Path(__file__).resolve().parents[1] / "configs" / "config.yaml")
VALUE = {"valid": True, "green": 0xFFFFFF, "red": 0, "infrared": 123456}


def controller_with_queue():
    controller = EEGController(CONFIG)
    controller.status_queue = queue.Queue()
    controller.process = MagicMock()
    controller.process.is_alive.return_value = True
    return controller


class PpgStreamTests(unittest.TestCase):
    def test_validity_bounds_and_independent_cursors(self):
        buffer = PpgBuffer(max_samples=3)
        for index in range(5):
            buffer.append(VALUE, index / 100)
        buffer.append({**VALUE, "valid": False}, 1)
        buffer.append({**VALUE, "green": 0x1000000}, 1)
        buffer.append(VALUE, float("nan"))
        first = buffer.snapshot()
        self.assertEqual([s["id"] for s in first["data"]], [3, 4, 5])
        self.assertEqual(buffer.snapshot(), first)  # one viewer cannot consume another's data
        self.assertEqual([s["id"] for s in buffer.snapshot(4)["data"]], [5])
        buffer.clear()
        buffer.append(VALUE, 2)
        self.assertNotEqual(buffer.snapshot()["session"], first["session"])
        self.assertEqual(buffer.snapshot(5)["data"][0]["id"], 6)

    def test_mode_boundaries_and_diagnostic_frames(self):
        controller = controller_with_queue()
        controller.status_queue.put({"type": "mode_started", "mode": "eeg"})
        for index in range(60):
            controller.status_queue.put({"type": "ppg", "value": VALUE, "ts": index / 100})
        controller.status_queue.put({"type": "ppg", "value": {**VALUE, "valid": False}, "ts": 0.6})
        snapshot = controller.get_ppg_snapshot()
        self.assertTrue(snapshot["active"])
        self.assertEqual(len(snapshot["data"]), 60)
        self.assertFalse(controller.get_status()["ppg"]["value"]["valid"])
        controller.status_queue.put({"type": "mode_stopped", "mode": "eeg"})
        stopped = controller.get_ppg_snapshot()
        self.assertFalse(stopped["active"])
        self.assertEqual(stopped["data"], [])
        self.assertNotEqual(snapshot["session"], stopped["session"])

    def test_websocket_delivers_samples_to_two_viewers_and_resets(self):
        from fastapi.testclient import TestClient
        import main

        controller = controller_with_queue()
        controller.status_queue.put({"type": "mode_started", "mode": "eeg"})
        for index in range(60):
            controller.status_queue.put({"type": "ppg", "value": VALUE, "ts": index / 100})
        with patch.object(main.state, "controller", controller):
            client = TestClient(main.app)
            with client.websocket_connect("/ws/ppg") as first, client.websocket_connect("/ws/ppg") as second:
                a, b = first.receive_json(), second.receive_json()
                self.assertEqual(a["data"], b["data"])
                self.assertEqual(len(a["data"]), 60)
                controller.status_queue.put({"type": "mode_stopped", "mode": "eeg"})
                for _ in range(10):
                    stopped = first.receive_json()
                    if not stopped["active"]:
                        break
                self.assertFalse(stopped["active"])
                self.assertEqual(stopped["data"], [])


class PpgAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_coalesced_frames_keep_every_valid_update_and_device_timing(self):
        stop = threading.Event()
        statuses, commands = queue.Queue(), queue.Queue()
        commands.put({"type": "start_mode", "mode": "eeg"})
        client = MagicMock()
        client.is_connected = True
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.start_notify = AsyncMock()
        client.stop_notify = AsyncMock()
        client.write_gatt_char = AsyncMock()
        writer = MagicMock()
        with patch("core.ble.acquisition_process.BleakClient", return_value=client), patch(
            "core.ble.acquisition_process.LslOutletWriter", return_value=writer
        ):
            task = asyncio.create_task(_connect_and_stream(CONFIG, stop, statuses, commands, None, "test", "test"))
            try:
                started = False
                for _ in range(150):
                    await asyncio.sleep(0.01)
                    while not statuses.empty():
                        message = statuses.get_nowait()
                        self.assertNotEqual(message["type"], "error", message)
                        started |= message["type"] == "mode_started"
                    if started:
                        break
                self.assertTrue(started, "simulated acquisition did not start")
                notify = client.start_notify.call_args.args[1]
                frames = []
                for index in range(60):
                    frame = bytearray(_frame(sequence=(250 + index * 2) & 0xFF))
                    frame[124] = 1 if index % 3 == 0 else 0
                    frame[-2] = sum(frame[2:-2]) & 0xFF
                    frames.append(bytes(frame))
                packet = b"".join(frames)
                notify(None, packet[:37])
                notify(None, packet[37:])
                messages = []
                while not statuses.empty():
                    messages.append(statuses.get_nowait())
                samples = [m for m in messages if m["type"] == "ppg" and m["value"]["valid"]]
                self.assertEqual(len(samples), 40)
                self.assertAlmostEqual(samples[1]["ts"] - samples[0]["ts"], 0.02, places=5)
                self.assertAlmostEqual(samples[2]["ts"] - samples[1]["ts"], 0.04, places=5)
                self.assertEqual(writer.push_samples.call_count, 60)
                self.assertEqual(len(writer.push_samples.call_args.args[0][0]), 9)
            finally:
                stop.set()
                await asyncio.wait_for(task, timeout=3)


if __name__ == "__main__":
    unittest.main()
