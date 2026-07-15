import socket
from pathlib import Path

import pytest

from app.clients.storage import AssetStore, UnsafeRemoteUrlError, validate_remote_asset_url
from app.config import Settings


def _addrinfo(address: str, port: int = 443):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (address, port))]


def test_validate_remote_asset_url_rejects_local_and_non_http_urls():
    blocked = [
        "ftp://example.com/file.mp4",
        "http://localhost/file.mp4",
        "http://127.0.0.1/file.mp4",
        "http://[::1]/file.mp4",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.8/private.mp4",
    ]

    for url in blocked:
        with pytest.raises(UnsafeRemoteUrlError):
            validate_remote_asset_url(url)


def test_validate_remote_asset_url_rejects_public_name_resolving_to_private(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _addrinfo("192.168.10.4"))

    with pytest.raises(UnsafeRemoteUrlError, match="blocked address"):
        validate_remote_asset_url("https://cdn.example.invalid/generated.mp4")


def test_validate_remote_asset_url_accepts_public_name_resolving_to_global_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _addrinfo("93.184.216.34"))

    validate_remote_asset_url("https://cdn.example.invalid/generated.mp4?Signature=temporary")


def test_public_media_base_url_wins_over_signed_oss_url(tmp_path: Path):
    settings = Settings(
        media_root=tmp_path / "media",
        public_media_base_url="https://directorgraph.example.invalid/media",
    )
    store = AssetStore(settings)

    class FakeBucket:
        def sign_url(self, method, key, expires):
            return f"https://signed.example.invalid/{key}?Expires={expires}"

    store.bucket = FakeBucket()

    asset = store.reference_for_key("projects/p1/shots/S01/storyboard.png")

    assert asset.public_url == "https://directorgraph.example.invalid/media/projects/p1/shots/S01/storyboard.png"


@pytest.mark.asyncio
async def test_save_remote_rejects_private_url_before_creating_destination(tmp_path: Path):
    settings = Settings(media_root=tmp_path / "media")
    store = AssetStore(settings)
    key = "projects/project-1/shots/S01/attempt-1.mp4"

    with pytest.raises(UnsafeRemoteUrlError):
        await store.save_remote("http://127.0.0.1/private.mp4", key)

    assert not store.path_for_key(key).exists()
