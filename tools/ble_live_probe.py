"""Capture real BLE notifications without running the UI or synthesizing data."""

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, replace
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bleak import BleakClient, BleakScanner
from configs.config_loader import load_config
from core.ble.frame_parser import FrameSpec, FrameStreamDecoder, parse_frame


async def capture(args):
    device = adv = None
    for attempt in range(4):
        devices = await BleakScanner.discover(timeout=8, return_adv=True)
        item = next((value for address, value in devices.items() if address.upper() == args.address.upper()), None)
        if item is not None:
            device, adv = item
            break
        print(f"Target not seen in scan {attempt + 1}/4; retrying", flush=True)
    if device is None or adv is None:
        raise RuntimeError(f"BLE target {args.address} was not discovered")
    print(f"Connecting {device}, RSSI {adv.rssi}", flush=True)
    records = []
    async with BleakClient(device, timeout=20) as client:
        print(f"Connected, MTU {client.mtu_size}", flush=True)
        notify = "0000fff1-0000-1000-8000-00805f9b34fb"
        write = "0000fff2-0000-1000-8000-00805f9b34fb"

        def on_notify(_, data):
            records.append((time.perf_counter(), bytes(data)))

        await client.start_notify(notify, on_notify)
        try:
            await client.write_gatt_char(write, bytes([2, 2]), response=True)
            await asyncio.sleep(0.3)
            records.clear()
            started = time.perf_counter()
            await client.write_gatt_char(write, bytes([2, 1]), response=True)
            await asyncio.sleep(args.seconds)
            finished = time.perf_counter()
        finally:
            await client.write_gatt_char(write, bytes([2, 2]), response=True)
            await client.stop_notify(notify)
    records = [(t, data) for t, data in records if t <= finished]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(json.dumps({"t": t - started, "hex": data.hex()}) for t, data in records), encoding="utf-8")
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "configs/config.yaml"))
    spec = FrameSpec(channels=8, **asdict(cfg.eeg.protocol.ch8.frame))
    decoder = FrameStreamDecoder(spec)
    strict_decoder = FrameStreamDecoder(replace(spec, allow_ch8_ppg_checksum_quirk=False))
    strict_valid = 0
    valid = invalid = dropped = lost = 0
    last_seq = None
    parsing_ms = []
    marker = Counter()
    raw_sums = Counter()
    raw_lengths = Counter()
    for _, data in records:
        strict_valid += len(strict_decoder.feed(data).frames)
        before = time.perf_counter()
        batch = decoder.feed(data)
        invalid += batch.invalid_frames
        dropped += batch.dropped_bytes
        for frame in batch.frames:
            decoded = parse_frame(frame, spec)
            valid += 1
            marker[decoded.ppg.get("marker")] += 1
            if last_seq is not None:
                lost += (decoded.sequence - last_seq - 1) & 255
            last_seq = decoded.sequence
        parsing_ms.append((time.perf_counter() - before) * 1000)
        if len(data) == 140 and data[:2] == b"\xaa\xbb":
            raw_lengths["framed_140"] += 1
            raw_sums["tail_cc"] += data[-1] == 0xcc
            raw_sums["sum_includes_seq"] += (sum(data[2:-2]) & 255) == data[-2]
            raw_sums["sum_excludes_seq"] += (sum(data[3:-2]) & 255) == data[-2]
    intervals = [(b[0] - a[0]) * 1000 for a, b in zip(records, records[1:])]
    def distribution(values):
        if not values:
            return {}
        ordered = sorted(values)
        return {"median": statistics.median(values), "p95": ordered[int((len(ordered)-1)*.95)], "max": max(values)}
    summary = {"address": args.address, "rssi": adv.rssi, "duration_sec": finished-started,
               "notifications": len(records), "lengths": dict(Counter(len(d) for _, d in records)),
               "bytes": sum(len(d) for _, d in records), "valid_frames": valid, "invalid_candidates": invalid,
               "lost_by_seq": lost, "dropped_bytes": dropped, "valid_fps": valid/(finished-started),
               "allow_ch8_ppg_checksum_quirk": spec.allow_ch8_ppg_checksum_quirk,
               "valid_frames_without_quirk": strict_valid,
               "ppg_markers": dict(marker), "raw_140_checks": dict(raw_sums),
               "arrival_interval_ms": distribution(intervals), "parse_ms": distribution(parsing_ms),
               "gaps_over_100ms": sum(t > 100 for t in intervals),
               "notifications_per_second": dict(Counter(int(t-started) for t, _ in records))}
    output.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="DC:4D:6F:93:1B:80")
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--output", default="offlinedata/diagnostics/ble_real.jsonl")
    asyncio.run(capture(parser.parse_args()))
