"""Optional music experiment ownership, per-song recording and recovery.

Audio is rendered by the browser; all EEG bytes, units and channel definitions
come from the existing main acquisition/offline path. Event times are software
observations, not claims about hardware or acoustic onset precision.
"""

import asyncio
import json
import logging
import os
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

from core.offline.offline_service import BandpassConfig, ExportTarget


class MusicService:
    def __init__(self, state, library, acquire, release):
        self.state = state
        self.library = library
        self.acquire = acquire
        self.release = release
        self.lock = asyncio.Lock()
        self.run = None
        self.trial = None
        self.last_chunk_at = 0.0
        self.watchdog = None
        self.last_result = None
        self.last_export_dir = None

    @property
    def active(self):
        return self.run is not None

    def on_chunk(self, chunk):
        if self.run and chunk:
            self.last_chunk_at = time.monotonic()

    def _check(self, token):
        if not self.run or token != self.run["token"]:
            raise ValueError("实验已结束或会话不匹配，请重新开始")

    def snapshot(self):
        if not self.run:
            return {"active": False}
        recording = self.state.offline.active_snapshot()
        samples = recording["total_samples"] if recording else 0
        elapsed = max(0, time.monotonic() - self.trial["started_monotonic"]) if self.trial else 0
        return {"active": True, "ready": bool(self.last_chunk_at and time.monotonic() - self.last_chunk_at < 3),
                "recording": self.trial is not None, "samples": samples,
                "elapsed_sec": elapsed, "effective_rate_hz": samples / elapsed if elapsed else 0,
                "completed": len(self.run["results"]), "total": len(self.run["playlist"])}

    def results_snapshot(self):
        """Return the last completed/aborted experiment for the export page."""
        if self.run:
            return {"active": True, "results": list(self.run["results"]), "token": self.run["token"]}
        if self.last_result:
            return dict(self.last_result)
        return {"active": False, "results": []}

    async def export_all(self, targets=None, bandpass=None, saved_results=None):
        """Export every completed trial into one dated directory with 1-10 folders."""
        if self.active:
            raise ValueError("实验仍在进行中，请停止实验后再导出")
        snapshot = self.results_snapshot()
        results = list(snapshot.get("results") or [])
        if not results:
            results = self._exportable_results(saved_results)
        if not results:
            raise ValueError("暂无可导出的文本-音频实验会话")

        requested = targets if isinstance(targets, (list, tuple)) else []
        target_specs = []
        seen_targets = set()
        for value in requested:
            if not isinstance(value, dict):
                continue
            kind = str(value.get("kind") or "").strip().lower()
            fmt = str(value.get("fmt") or "").strip().lower()
            key = (kind, fmt)
            if kind in {"raw", "filtered"} and fmt in {"csv", "edf"} and key not in seen_targets:
                seen_targets.add(key)
                target_specs.append(key)
        if not target_specs:
            raise ValueError("至少选择一种导出文件")

        bandpass = bandpass if isinstance(bandpass, dict) else {}
        want_filtered = any(kind == "filtered" for kind, _ in target_specs)
        bp_enabled = bool(bandpass.get("enabled"))
        try:
            lowcut_hz = float(bandpass.get("lowcut_hz", 3.0))
            highcut_hz = float(bandpass.get("highcut_hz", 50.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("滤波截止频率必须是有效数字") from exc
        if want_filtered and not bp_enabled:
            raise ValueError("已选择滤波导出，请先启用带通滤波")
        if bp_enabled and not (0 < lowcut_hz < highcut_hz):
            raise ValueError("滤波参数非法：需要满足 0 < 低频截止 < 高频截止")
        bp_config = BandpassConfig(
            enabled=bp_enabled,
            lowcut_hz=lowcut_hz,
            highcut_hz=highcut_hz,
            order=int(self.state.config.offline.filter.order),
        )

        root = Path(self.state.offline.data_root_dir)
        date_dir = root / datetime.now().strftime("%Y%m%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        export_dir = date_dir / f"music_export_{stamp}"
        suffix = 1
        while export_dir.exists():
            export_dir = date_dir / f"music_export_{stamp}_{suffix:02d}"
            suffix += 1
        export_dir.mkdir(parents=True, exist_ok=False)
        folders = []
        for number in range(1, 11):
            folder = export_dir / str(number)
            folder.mkdir()
            folders.append(str(folder))

        outputs = []
        errors = []
        missing = []
        for number in range(1, 11):
            folder = export_dir / str(number)
            if number > len(results):
                missing.append(number)
                continue
            result = results[number - 1] or {}
            session = result.get("session") or {}
            song = result.get("song") or {}
            session_id = str(session.get("session_id") or "").strip()
            if not session_id:
                errors.append({"index": number, "message": "会话 ID 缺失"})
                continue
            category = str(song.get("category") or number)
            name = str(song.get("name") or "eeg")
            stem = f"Category_{category}_{name}"
            export_targets = [
                ExportTarget(
                    kind=kind,
                    fmt=fmt,
                    filename=f"{stem}{'_filtered' if kind == 'filtered' else ''}.{fmt}",
                )
                for kind, fmt in target_specs
            ]
            try:
                exported = await asyncio.to_thread(
                    self.state.offline.export,
                    session_id=session_id,
                    base_name_raw=stem,
                    base_name_filtered=f"{stem}_filtered",
                    targets=export_targets,
                    bandpass=bp_config,
                    output_dir=str(folder),
                )
                outputs.extend([{**item, "index": number} for item in exported.get("outputs", [])])
            except Exception as exc:
                errors.append({"index": number, "session_id": session_id, "message": str(exc)})

        self.last_export_dir = str(export_dir)
        return {
            "export_dir": str(export_dir),
            "folders": folders,
            "folder_count": 10,
            "completed_count": len(results),
            "missing_indexes": missing,
            "outputs": outputs,
            "errors": errors,
            "targets": [{"kind": kind, "fmt": fmt} for kind, fmt in target_specs],
            "bandpass": {
                "enabled": bp_enabled,
                "lowcut_hz": lowcut_hz,
                "highcut_hz": highcut_hz,
            },
        }

    @staticmethod
    def _exportable_results(value):
        """Keep only the fields required to recover a completed browser session."""
        if not isinstance(value, list):
            return []
        results = []
        seen = set()
        for item in value[:10]:
            if not isinstance(item, dict):
                continue
            session = item.get("session") if isinstance(item.get("session"), dict) else {}
            song = item.get("song") if isinstance(item.get("song"), dict) else {}
            session_id = str(session.get("session_id") or "").strip()
            name = str(song.get("name") or "").strip()
            if not session_id or not name or session_id in seen:
                continue
            seen.add(session_id)
            results.append({
                "session": {"session_id": session_id},
                "song": {"name": name, "category": song.get("category")},
            })
        return results

    def open_export_folder(self):
        """Open the most recent batch export directory with the system file manager."""
        target = os.path.abspath(str(self.last_export_dir or ""))
        if not target or not os.path.isdir(target):
            raise FileNotFoundError("尚未生成文本-音频批量导出目录")
        if hasattr(os, "startfile"):
            os.startfile(target)
            return target
        if not webbrowser.open(f"file:///{target.replace(os.sep, '/')}", new=1):
            raise RuntimeError("系统文件管理器打开失败")
        return target

    async def start(self, song_ids, subject):
        async with self.lock:
            if self.active or self.state.streamer.is_streaming or self.state.offline.active_session_id:
                raise ValueError("已有采集或文本-音频多模态实验正在运行，请先停止")
            dev = self.state.controller.get_status()
            if not dev.get("running") or dev.get("last", {}).get("type") not in {"connected", "ready"}:
                raise ValueError("请先在设备页连接设备并应用通道配置")
            if dev.get("task_running"):
                raise ValueError("设备正在运行其他模式，请先停止")
            if not song_ids or len(song_ids) > 200 or len(set(song_ids)) != len(song_ids):
                raise ValueError("请选择 1–200 首不重复的歌曲")
            playlist = [self.library.song(song_id) for song_id in song_ids]
            now = time.monotonic()
            self.run = {"token": uuid.uuid4().hex, "subject": subject, "playlist": playlist,
                        "started_at": time.time(), "started_monotonic": now, "heartbeat": now,
                        "results": [], "manifest_dir": None}
            self.last_chunk_at = 0
            try:
                result = await self.acquire()
                if result.get("status") != "success":
                    raise ValueError(result.get("message", "EEG 启动失败"))
            except Exception:
                try:
                    await self.release()
                finally:
                    self.run = None
                raise
            self.watchdog = asyncio.create_task(self._watch(), name="music-experiment-watchdog")
            return {"token": self.run["token"], "playlist": playlist, **self.snapshot()}

    async def heartbeat(self, token):
        # No await or mutation lock: exports may take time, but the browser must
        # still renew ownership while a worker thread writes the previous CSV.
        self._check(token)
        self.run["heartbeat"] = time.monotonic()
        return self.snapshot()

    async def start_trial(self, token, index):
        async with self.lock:
            self._check(token)
            if self.trial or index != len(self.run["results"]) or not 0 <= index < len(self.run["playlist"]):
                raise ValueError("歌曲顺序不匹配或上一首尚未保存")
            if not self.snapshot()["ready"]:
                raise ValueError("尚未接收到 EEG 数据，请检查设备与采集状态")
            song = self.run["playlist"][index]
            session = self.state.offline.start_session()
            self.trial = {"index": index, "song": song, "session": session.to_dict(),
                          "started_monotonic": time.monotonic(), "events": []}
            if not self.run["manifest_dir"]:
                self.run["manifest_dir"] = session.session_dir
            try:
                self._event("recording_started", {})
                self._save_manifest("running")
            except Exception:
                await self._finish("storage_error", {})
                raise
            return {"session": session.to_dict()}

    def _event(self, kind, timing):
        if not self.trial:
            return
        snapshot = self.state.offline.active_snapshot()
        event = {"event": kind, "server_time_unix_ms": time.time() * 1000,
                 "recording_elapsed_ms": (time.monotonic() - self.trial["started_monotonic"]) * 1000,
                 "sample_offset_received": snapshot["total_samples"] if snapshot else 0, **timing}
        if kind in {"playing", "ended", "stopped"}:
            command = "start" if kind == "playing" else "end"
            event["trigger_queued"] = bool(self.state.controller.send_trigger_command(command, "music"))
        self.trial["events"].append(event)
        path = Path(self.trial["session"]["session_dir"]) / "music_events.jsonl"
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        self._save_trial("recording")

    def _write_json(self, path, data):
        path = Path(path)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)

    def _save_trial(self, status, **extra):
        trial = self.trial
        self._write_json(Path(trial["session"]["session_dir"]) / "music_trial.json", {
            "experiment_id": self.run["token"], "subject": self.run["subject"],
            "playlist": self.run["playlist"], "index": trial["index"], "song": trial["song"],
            "session": trial["session"], "status": status, "events": trial["events"],
            "timing_note": "Browser playback events and server receipt times; trigger_queued is not a hardware acknowledgement.",
            **extra})

    def _save_manifest(self, status):
        if self.run["manifest_dir"]:
            self._write_json(Path(self.run["manifest_dir"]) / "music_experiment.json", {
                "experiment_id": self.run["token"], "subject": self.run["subject"],
                "started_at_unix": self.run["started_at"], "status": status,
                "playlist": self.run["playlist"], "results": self.run["results"]})

    async def event(self, token, index, kind, timing):
        async with self.lock:
            self._check(token)
            if not self.trial or self.trial["index"] != index:
                raise ValueError("当前歌曲会话不匹配")
            self._event(kind, timing)
            return self.snapshot()

    async def _finish_trial(self, reason, timing):
        if not self.trial:
            return None
        errors = []
        try:
            self._event("ended" if reason == "completed" else "stopped", timing)
        except Exception as exc:
            errors.append(f"事件保存失败：{exc}")
        session = await asyncio.to_thread(self.state.offline.stop_session)
        trial = self.trial
        if session:
            trial["session"] = session.to_dict()
        result = {"song": trial["song"], "session": trial["session"], "reason": reason, "outputs": []}
        elapsed = max(0.001, time.monotonic() - trial["started_monotonic"])
        result["effective_rate_hz"] = result["session"]["total_samples"] / elapsed
        try:
            self._save_trial(reason)
        except Exception as exc:
            errors.append(f"歌词/实验元数据保存失败：{exc}")
        try:
            export = await asyncio.to_thread(
                self.state.offline.export, session_id=trial["session"]["session_id"],
                base_name_raw=f"Category_{trial['song']['category']}_{trial['song']['name']}",
                targets=[ExportTarget(kind="raw", fmt="csv", filename="")], bandpass=None)
            result["outputs"] = export["outputs"]
        except Exception as exc:
            errors.append(f"CSV 导出失败（原始会话可在离线页面重试）：{exc}")
        result["errors"] = errors
        self.run["results"].append(result)
        self.trial = None
        try:
            self._save_manifest("running")
        except Exception as exc:
            errors.append(f"实验清单保存失败：{exc}")
        return result

    async def finish_trial(self, token, index, timing):
        async with self.lock:
            self._check(token)
            # Idempotent retries must never stop the next song.
            if index < len(self.run["results"]):
                return self.run["results"][index]
            if not self.trial or self.trial["index"] != index:
                raise ValueError("当前歌曲会话不匹配")
            return await self._finish_trial("completed", timing)

    async def stop(self, token=None, reason="stopped", timing=None):
        async with self.lock:
            if not self.run:
                if self.last_result and (token is None or token == self.last_result["token"]):
                    return self.last_result
                return {"active": False, "results": []}
            if token is not None:
                self._check(token)
            if reason == "completed" and (self.trial or len(self.run["results"]) != len(self.run["playlist"])):
                reason = "stopped"
            return await self._finish(reason, timing or {})

    async def _finish(self, reason, timing):
        errors = []
        try:
            await self._finish_trial(reason, timing)
        except Exception as exc:
            errors.append(str(exc))
            logging.exception("Music recording finalization failed")
        finally:
            try:
                response = await self.release()
                if response.get("status") != "success":
                    errors.append(response.get("message", "EEG 停止指令失败"))
            except Exception as exc:
                errors.append(str(exc))
        try:
            self._save_manifest(reason)
        except Exception as exc:
            errors.append(str(exc))
        self.last_result = {"token": self.run["token"], "active": False, "reason": reason,
                            "results": self.run["results"], "errors": errors}
        self.run = None
        self.trial = None
        if self.watchdog and self.watchdog is not asyncio.current_task():
            self.watchdog.cancel()
        self.watchdog = None
        return self.last_result

    async def _watch(self):
        try:
            while self.run:
                await asyncio.sleep(1)
                now = time.monotonic()
                dev = self.state.controller.get_status()
                reason = None
                if not dev.get("running") or dev.get("last", {}).get("type") in {"disconnected", "error", "stopped"}:
                    reason = "device_lost"
                elif now - self.run["heartbeat"] > 12:
                    reason = "browser_lost"
                elif now - (self.last_chunk_at or self.run["started_monotonic"]) > (5 if self.last_chunk_at else 20):
                    reason = "eeg_timeout"
                if reason:
                    await self.stop(reason=reason)
                    return
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.exception("Music watchdog failed")
            await self.stop(reason="watchdog_error")
