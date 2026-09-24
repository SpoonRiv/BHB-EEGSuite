"""Optional module's local audio catalogue and timestamped lyrics."""

import hashlib
import re
from pathlib import Path


class MusicLibrary:
    EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a"}

    def __init__(self, root):
        self.root = Path(root).resolve()

    def songs(self):
        directory = self.root / "Musics"
        if not directory.is_dir():
            return []
        files = sorted((p for p in directory.iterdir() if p.suffix.lower() in self.EXTENSIONS
                        and p.is_file() and p.resolve().parent == directory.resolve()), key=lambda p: p.name)
        return [{"id": hashlib.sha256(p.name.encode("utf-8")).hexdigest()[:20],
                 "category": i + 1, "name": p.stem, "filename": p.name,
                 "has_lyrics": self.lyrics_path(p.stem) is not None}
                for i, p in enumerate(files)]

    def song(self, song_id):
        for song in self.songs():
            if song["id"] == song_id:
                return song
        raise ValueError("歌曲不存在，请刷新曲库后重试")

    def audio_path(self, song_id):
        return self.root / "Musics" / self.song(song_id)["filename"]

    def lyrics_path(self, name):
        directory = self.root / "Lyrics"
        for ext in (".lrc", ".txt"):
            path = directory / (name + ext)
            if path.is_file() and path.resolve().parent == directory.resolve():
                return path
        return None

    def lyrics(self, song_id):
        song = self.song(song_id)
        path = self.lyrics_path(song["name"])
        if not path:
            return {"text": "", "cues": []}
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("歌词文件超过 1 MiB，请缩小文件后重试")
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            content = path.read_text(encoding="gb18030")
        return parse_lyrics(content)


def parse_lyrics(content):
    stamps = re.compile(r"\[(\d{1,3}):(\d{2}(?:\.\d{1,3})?)\]")
    offset = re.search(r"\[offset:([+-]?\d+)\]", content, re.I)
    shift = int(offset.group(1)) / 1000 if offset else 0
    cues = []
    for line in content.splitlines():
        matches = list(stamps.finditer(line))
        if matches:
            text = stamps.sub("", line).strip()
            for match in matches:
                at = int(match[1]) * 60 + float(match[2]) + shift
                cues.append({"time": max(0, at), "text": text})
    return {"text": content.strip(), "cues": sorted(cues, key=lambda cue: cue["time"])}
