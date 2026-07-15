from __future__ import annotations

import ipaddress
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

try:
    import oss2
except ImportError:  # local mock mode does not require the OSS SDK
    oss2 = None  # type: ignore[assignment]

from app.config import Settings

MAX_REMOTE_REDIRECTS = 5


@dataclass(slots=True)
class StoredAsset:
    key: str
    local_path: Path
    public_url: str


class UnsafeRemoteUrlError(ValueError):
    pass


def _blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        [
            address.is_loopback,
            address.is_private,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
            not address.is_global,
        ]
    )


def _raise_if_blocked_address(host: str, address: str) -> None:
    parsed = ipaddress.ip_address(address)
    if _blocked_address(parsed):
        raise UnsafeRemoteUrlError(f"Remote asset host {host!r} resolves to blocked address {parsed}")


def validate_remote_asset_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeRemoteUrlError("Remote asset URL must use http or https")
    if not parsed.hostname:
        raise UnsafeRemoteUrlError("Remote asset URL must include a hostname")

    host = parsed.hostname.strip("[]")
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise UnsafeRemoteUrlError("Remote asset URL must not target localhost")

    try:
        _raise_if_blocked_address(host, host)
        return
    except UnsafeRemoteUrlError:
        raise
    except ValueError:
        pass

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise UnsafeRemoteUrlError(f"Remote asset host could not be resolved: {host}") from exc
    if not addresses:
        raise UnsafeRemoteUrlError(f"Remote asset host could not be resolved: {host}")
    for address in addresses:
        _raise_if_blocked_address(host, address)


class AssetStore:
    """Local write-through cache with optional Alibaba Cloud OSS publication."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.media_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.bucket = None
        if settings.oss_ready:
            if oss2 is None:
                raise RuntimeError("Install the oss2 package to use Alibaba Cloud OSS")
            auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
            self.bucket = oss2.Bucket(auth, settings.oss_endpoint, settings.oss_bucket)

    def _public_url(self, key: str) -> str:
        if self.settings.public_media_base_url:
            return f"{self.settings.public_media_base_url.rstrip('/')}/{key}"
        if self.bucket:
            if self.settings.oss_public_base_url:
                return f"{self.settings.oss_public_base_url.rstrip('/')}/{key}"
            return self.bucket.sign_url("GET", key, 86_400)
        return f"/media/{key}"

    def path_for_key(self, key: str) -> Path:
        destination = (self.root / key).resolve()
        if self.root not in destination.parents and destination != self.root:
            raise ValueError("Asset key escapes media root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination

    def put_file(self, source: str | Path, key: str) -> StoredAsset:
        source_path = Path(source).resolve()
        destination = self.path_for_key(key)
        if source_path != destination:
            shutil.copy2(source_path, destination)
        if self.bucket:
            self.bucket.put_object_from_file(key, str(destination))
        return StoredAsset(key=key, local_path=destination, public_url=self._public_url(key))

    def reference_for_key(self, key: str) -> StoredAsset:
        return StoredAsset(key=key, local_path=self.path_for_key(key), public_url=self._public_url(key))

    def ensure_local(self, key: str) -> StoredAsset:
        destination = self.path_for_key(key)
        if not destination.exists():
            if not self.bucket:
                raise FileNotFoundError(key)
            self.bucket.get_object_to_file(key, str(destination))
        return StoredAsset(key=key, local_path=destination, public_url=self._public_url(key))

    async def save_remote(self, url: str, key: str) -> StoredAsset:
        validate_remote_asset_url(url)
        destination = self.path_for_key(key)
        current_url = url
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30), follow_redirects=False) as client:
            for _ in range(MAX_REMOTE_REDIRECTS + 1):
                validate_remote_asset_url(current_url)
                async with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise UnsafeRemoteUrlError("Remote asset redirect omitted Location header")
                        current_url = urljoin(str(response.url), location)
                        continue
                    response.raise_for_status()
                    with destination.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            output.write(chunk)
                break
            else:
                raise UnsafeRemoteUrlError("Remote asset URL exceeded redirect limit")
        if self.bucket:
            self.bucket.put_object_from_file(key, str(destination))
        return StoredAsset(key=key, local_path=destination, public_url=self._public_url(key))

    def local_path_from_url(self, url: str | None) -> Path | None:
        if not url:
            return None
        base = self.settings.public_media_base_url.rstrip("/") + "/"
        if url.startswith(base):
            candidate = self.path_for_key(url[len(base) :])
            return candidate if candidate.exists() else None
        parsed = urlparse(url)
        filename = Path(parsed.path).name
        matches = list(self.root.rglob(filename)) if filename else []
        return matches[0] if matches else None
