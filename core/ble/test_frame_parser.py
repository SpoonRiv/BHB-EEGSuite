#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""脑电数据协议回归测试（可直接用 ``python -m unittest`` 运行）。"""

import unittest

from core.ble.frame_parser import FrameSpec, FrameStreamDecoder, parse_frame, parse_trigger_per_sample


def _spec() -> FrameSpec:
    return FrameSpec(
        channels=8,
        header_len_bytes=3,
        bytes_per_sample_per_channel=3,
        samples_per_frame=5,
        trigger_len_bytes=1,
        ppg_len_bytes=10,
        reserved_len_bytes=2,
        imu_len_bytes=0,
        battery_len_bytes=2,
        tail_len_bytes=2,
    )


def _frame(sequence: int = 7, battery: bytes = b"\x00\x64") -> bytes:
    spec = _spec()
    eeg = bytearray()
    for sample_idx in range(5):
        for channel_idx in range(8):
            value = sample_idx * 100 + channel_idx
            eeg.extend(int(value).to_bytes(3, byteorder="big", signed=True))
    trigger = b"\x1c"  # low bits: 0, 0, 1, 1, 1
    ppg = b"\x00" + bytes.fromhex("010203 040506 070809")
    reserved = b"\xde\xad"
    payload = bytes([0xAA, 0xBB, sequence & 0xFF]) + bytes(eeg) + trigger + ppg + reserved + battery
    checksum = sum(payload[2:]) & 0xFF
    frame = payload + bytes([checksum, 0xCC])
    assert len(frame) == spec.frame_len_bytes == 140
    return frame


class FrameParserTests(unittest.TestCase):
    def test_ch8_frame_layout_and_ppg_are_decoded(self) -> None:
        decoded = parse_frame(_frame(), _spec())
        self.assertEqual(decoded.sequence, 7)
        self.assertEqual(decoded.samples[0][:8], [float(i) for i in range(8)])
        self.assertEqual([row[8] for row in decoded.samples], [0.0, 0.0, 1.0, 1.0, 1.0])
        self.assertEqual(decoded.battery_level, 100)
        self.assertEqual(decoded.reserved, b"\xde\xad")
        self.assertEqual(decoded.ppg, {
            "marker": 0,
            "valid": True,
            "green": 0x010203,
            "red": 0x040506,
            "infrared": 0x070809,
            "raw": "00010203040506070809",
        })

    def test_checksum_includes_sequence_and_rejects_corruption(self) -> None:
        spec = _spec()
        frame = bytearray(_frame(sequence=1))
        self.assertTrue(spec.validate_checksum(bytes(frame)))
        frame[2] ^= 0x01
        self.assertFalse(spec.validate_checksum(bytes(frame)))

    def test_battery_ffff_is_invalid_and_trigger_reserved_bits_are_masked(self) -> None:
        decoded = parse_frame(_frame(battery=b"\xff\xff"), _spec())
        self.assertIsNone(decoded.battery_level)
        self.assertEqual(parse_trigger_per_sample(b"\xff", 5), [1.0] * 5)

    def test_stream_decoder_handles_split_and_concatenated_frames(self) -> None:
        spec = _spec()
        first = _frame(sequence=10)
        second = _frame(sequence=11)
        decoder = FrameStreamDecoder(spec)
        self.assertEqual(decoder.feed(first[:37]).frames, ())
        self.assertEqual(decoder.feed(first[37:] + second).frames, (first, second))
        self.assertEqual(decoder.buffered_bytes, 0)


if __name__ == "__main__":
    unittest.main()

