"""Remote release index, verified downloads, and bounded ZIP extraction."""

import hashlib
import json
import re
import stat
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse


MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 300 * 1024 * 1024
MAX_FILES = 2000
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _https_url(value):
    if not isinstance(value, str) or urlparse(value).scheme != "https" or not urlparse(value).netloc:
        raise ValueError("模块源和下载地址必须是 HTTPS URL")
    return value


def _open_https(url, timeout):
    response = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "BHB-EEGSuite/1"}), timeout=timeout)
    if urlparse(response.geturl()).scheme != "https":
        response.close()
        raise ValueError("模块下载被重定向到非 HTTPS 地址")
    return response


class ModuleStore:
    def __init__(self, catalog_url):
        self.catalog_url = catalog_url.strip()

    def release(self, module_id):
        if not self.catalog_url:
            raise ValueError("未配置远程模块源，请设置 modules.catalog_url")
        try:
            with _open_https(_https_url(self.catalog_url), 8) as response:
                raw = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"模块源返回 HTTP {exc.code}，请检查发布地址") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValueError("模块源连接失败，请检查网络连接或配置地址") from exc
        if len(raw) > 1024 * 1024:
            raise ValueError("模块索引文件过大")
        try:
            index = json.loads(raw)
            if index["schema"] != 1 or not isinstance(index["modules"], list):
                raise ValueError("模块索引格式不兼容")
            release = next(item for item in index["modules"] if item["id"] == module_id)
            if release["host_api"] != 1 or not isinstance(release["version"], str) or not release["version"]:
                raise ValueError("模块版本与上位机不兼容")
            _https_url(release["url"])
            if not SHA256.fullmatch(release["sha256"]):
                raise ValueError("模块索引缺少有效的 SHA-256 校验值")
            size = release["size"]
            if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
                raise ValueError("模块包大小无效")
            return release
        except (KeyError, StopIteration, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("模块索引格式无效或未包含所需模块") from exc

    def download(self, release, target, progress):
        size = release["size"]
        digest = hashlib.sha256()
        downloaded = 0
        try:
            with _open_https(release["url"], 30) as response, open(target, "wb") as output:
                while chunk := response.read(256 * 1024):
                    downloaded += len(chunk)
                    if downloaded > size or downloaded > MAX_ARCHIVE_BYTES:
                        raise ValueError("下载内容超过索引声明的大小")
                    digest.update(chunk)
                    output.write(chunk)
                    progress(downloaded, size)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"模块下载返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValueError("模块下载失败，请检查网络连接") from exc
        if downloaded != size or digest.hexdigest() != release["sha256"]:
            raise ValueError("模块包大小或 SHA-256 校验失败")

    @staticmethod
    def extract(archive, destination, module_id):
        destination.mkdir()
        with zipfile.ZipFile(archive) as package:
            entries = package.infolist()
            if len(entries) > MAX_FILES or sum(item.file_size for item in entries) > MAX_EXTRACTED_BYTES:
                raise ValueError("模块包内容超过安全限制")
            seen = set()
            for item in entries:
                name = item.filename.rstrip("/")
                raw_parts = name.split("/")
                parts = PurePosixPath(name).parts
                if (not parts or parts[0] != module_id or any(part in ("", ".", "..") for part in raw_parts)
                        or "\\" in name or ":" in name or name.startswith("/")):
                    raise ValueError("模块包包含非法路径")
                if name in seen:
                    raise ValueError("模块包包含重复文件")
                seen.add(name)
                mode = item.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("模块包包含外部链接或特殊文件")
                target = destination.joinpath(*parts[1:])
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(item) as source, open(target, "wb") as output:
                        remaining = item.file_size
                        while chunk := source.read(min(256 * 1024, remaining + 1)):
                            remaining -= len(chunk)
                            if remaining < 0:
                                raise ValueError("模块包内容大小不符")
                            output.write(chunk)
