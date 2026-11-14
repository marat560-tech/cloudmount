from .base import AlreadyExists, AuthError, Entry, NotFound, Provider, ProviderError
from .dropbox import DropboxProvider
from .gdrive import GDriveProvider
from .webdav import BeelineProvider, WebDAVProvider
from .yandex import YandexProvider

PROVIDERS = {cls.kind: cls for cls in (
    YandexProvider, BeelineProvider, DropboxProvider, GDriveProvider, WebDAVProvider)}


def make_provider(account: dict, save=None) -> Provider:
    cls = PROVIDERS[account["type"]]
    return cls(account.setdefault("data", {}), save)


__all__ = ["PROVIDERS", "make_provider", "Entry", "Provider", "ProviderError",
           "NotFound", "AlreadyExists", "AuthError"]
