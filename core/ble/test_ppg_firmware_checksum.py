"""Regression using a frame captured from DC:4D:6F:93:1B:80, not generated EEG."""

from dataclasses import asdict, replace
from pathlib import Path
import unittest

from configs.config_loader import load_config
from core.ble.frame_parser import FrameSpec, FrameStreamDecoder, parse_frame


# First complete frame from the 2026-09-22 live capture (SUM 0x44).
CAPTURED_FRAME = bytes.fromhex(
    "aabb000127b000f94f012b8700afe101415700983a017163019669fdc80efd9c97fdd65b"
    "fd5a5cfda900fd291efe1943fe1dc0fb9b6bfb73b4fbb17dfb32dffb573efaf230fbe282"
    "fbe5cffb0a04fadd39fb2052faa05efab77bfa5915fb4db0fb49dcfc9eb4fc6c72fcb01d"
    "fc2f67fc6733fbf2bafcdf9cfce9b8000000011c0004830002b80000004c44cc"
)


class CapturedPpgChecksumTests(unittest.TestCase):
    def setUp(self):
        config = load_config(str(Path(__file__).resolve().parents[2] / "configs/config.yaml"))
        self.spec = FrameSpec(channels=8, **asdict(config.eeg.protocol.ch8.frame))

    def test_real_frame_requires_explicit_firmware_compatibility(self):
        self.assertFalse(replace(self.spec, allow_ch8_ppg_checksum_quirk=False).validate_checksum(CAPTURED_FRAME))
        self.assertTrue(self.spec.allow_ch8_ppg_checksum_quirk)
        decoded = parse_frame(CAPTURED_FRAME, self.spec)
        self.assertEqual(len(decoded.samples), 5)
        self.assertEqual(decoded.ppg["green"], 284)
        self.assertEqual(decoded.ppg["red"], 1155)
        self.assertEqual(decoded.ppg["infrared"], 696)

    def test_stream_resynchronization_retains_captured_frame(self):
        decoder = FrameStreamDecoder(self.spec)
        self.assertEqual(decoder.feed(CAPTURED_FRAME[:37]).frames, ())
        result = decoder.feed(CAPTURED_FRAME[37:])
        self.assertEqual(result.frames, (CAPTURED_FRAME,))
        self.assertEqual(result.invalid_frames, 0)

    def test_covered_bytes_and_frame_boundaries_still_checked(self):
        for offset in (0, 1, 3, 30, 123, 124, 125, 126, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139):
            with self.subTest(offset=offset):
                damaged = bytearray(CAPTURED_FRAME)
                damaged[offset] ^= 1
                self.assertFalse(self.spec.validate_checksum(bytes(damaged)))

    def test_quirk_does_not_apply_to_legacy_imu_layout(self):
        legacy = replace(self.spec, ppg_len_bytes=0, reserved_len_bytes=0, imu_len_bytes=12)
        self.assertFalse(legacy.validate_checksum(CAPTURED_FRAME))

    def test_contact_frames_survive_notification_fragmentation(self):
        fixture = Path(__file__).with_name("fixtures") / "msm008s00_ppg_contact.hex"
        frames = [
            bytes.fromhex(line.strip())
            for line in fixture.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(frames), 18)
        decoder = FrameStreamDecoder(self.spec)
        raw = b"".join(frames)
        received = []
        for offset in range(0, len(raw), 244):
            result = decoder.feed(raw[offset:offset + 244])
            received.extend(result.frames)
            self.assertEqual(result.invalid_frames, 0)
            self.assertEqual(result.dropped_bytes, 0)
        self.assertEqual(received, frames)
        self.assertEqual(decoder.buffered_bytes, 0)
        contact = parse_frame(frames[7], self.spec)
        self.assertEqual(contact.sequence, 167)
        self.assertEqual([contact.ppg[k] for k in ("green", "red", "infrared")], [28701, 133109, 120868])
        self.assertFalse(parse_frame(frames[10], self.spec).ppg["valid"])
        for offset in (3, 123, 126, 130, 133, 137, 138, 139):
            with self.subTest(contact_corruption=offset):
                damaged = bytearray(frames[7])
                damaged[offset] ^= 1
                self.assertFalse(self.spec.validate_checksum(bytes(damaged)))


if __name__ == "__main__":
    unittest.main()
