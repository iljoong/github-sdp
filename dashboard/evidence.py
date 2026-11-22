import copy
import io
import json
import re
import threading
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

from .config import positive
from .github import RemoteError

MAX_ZIP = 16 * 1024 * 1024
MAX_MEMBER = 1024 * 1024


class UnsafeArtifact(ValueError):
    pass


def read_member(blob, name):
    if len(blob) > MAX_ZIP:
        raise UnsafeArtifact("Artifact is too large")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            if len(archive.infolist()) > 3000:
                raise UnsafeArtifact("Too many archive members")
            for entry in archive.infolist():
                member = PurePosixPath(entry.filename)
                if member.is_absolute() or ".." in member.parts or "\\" in entry.filename:
                    raise UnsafeArtifact("Invalid archive member")
            matches = [entry for entry in archive.infolist() if entry.filename == name]
            if not matches:
                return None
            if len(matches) != 1 or matches[0].file_size > MAX_MEMBER:
                raise UnsafeArtifact("Invalid evidence member")
            with archive.open(matches[0]) as stream:
                member = stream.read(MAX_MEMBER + 1)
            if len(member) > MAX_MEMBER:
                raise UnsafeArtifact("Evidence exceeds the size limit")
            value = json.loads(member)
            if not isinstance(value, dict):
                raise UnsafeArtifact("Evidence is not an object")
            return value
    except (zipfile.BadZipFile, RuntimeError, UnicodeError, RecursionError, json.JSONDecodeError) as error:
        raise UnsafeArtifact("Invalid artifact archive") from error


def download(url):
    if not isinstance(url, str):
        raise UnsafeArtifact("Artifact download URL missing")
    endpoint = urlsplit(url)
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.netloc != endpoint.hostname
            or not re.fullmatch(r"[a-z0-9-]+\.blob\.core\.windows\.net|[a-z0-9-]+\.actions\.githubusercontent\.com",
                                endpoint.hostname)):
        raise UnsafeArtifact("Artifact redirect host is not allowed")
    try:
        with httpx.Client(timeout=12, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise UnsafeArtifact("Artifact download failed")
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > MAX_ZIP:
                        raise UnsafeArtifact("Artifact download too large")
                    body.extend(chunk)
                return body
    except httpx.HTTPError:
        raise UnsafeArtifact("Artifact download unavailable") from None


class Evidence:
    """Reads one JSON member from a run artifact.

    Artifact archives are immutable, so parsed members are cached by (artifact ID, member) for display.
    The artifact listing is always re-read so expiry is honoured; ``fresh=True`` (write-time checks)
    bypasses the cache and downloads again.
    """

    def __init__(self, github, download_file=download, cache_size=256):
        self.github, self.download_file = github, download_file
        self.cache_size, self._cache, self._lock = cache_size, {}, threading.Lock()

    def file(self, run_id, artifact_name, member, fresh=False):
        try:
            rows = [row for row in self.github.artifacts(run_id)
                    if row.get("name") == artifact_name and row.get("expired") is False]
            if len(rows) != 1:
                return None
            key = (positive(rows[0]["id"]), member)
            if not fresh:
                with self._lock:
                    if key in self._cache:
                        return copy.deepcopy(self._cache[key])
            value = read_member(self.download_file(self.github.artifact_location(key[0])), member)
            with self._lock:
                self._cache.pop(key, None)
                while len(self._cache) >= self.cache_size:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = value
            return copy.deepcopy(value)
        except (UnsafeArtifact, RemoteError, ValueError, KeyError, TypeError):
            return None
