from app.clients.storage import AssetStore
from app.config import Settings
from app.providers.base import StudioProvider
from app.providers.mock import MockStudioProvider


def create_provider(settings: Settings, store: AssetStore) -> StudioProvider:
    if settings.provider_mode == "live":
        from app.providers.live import LiveStudioProvider

        return LiveStudioProvider(settings, store)
    return MockStudioProvider(settings, store)
