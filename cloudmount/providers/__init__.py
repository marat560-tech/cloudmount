from .base import AlreadyExists, AuthError, Entry, NotFound, Provider, ProviderError
from .box import BoxProvider
from .dropbox import DropboxProvider
from .gdrive import GDriveProvider
from .onedrive import OneDriveProvider
from .pcloud import PCloudProvider
from .s3 import S3Provider
from .sftp import SFTPProvider
from .webdav import (BeelineProvider, KoofrProvider, MailruProvider, NextcloudProvider,
                     WebDAVProvider)
from .yandex import YandexProvider

# Порядок — как в окне «Добавить облако»
PROVIDERS = {cls.kind: cls for cls in (
    YandexProvider, BeelineProvider, MailruProvider, GDriveProvider, OneDriveProvider,
    DropboxProvider, PCloudProvider, BoxProvider, KoofrProvider, NextcloudProvider,
    S3Provider, SFTPProvider, WebDAVProvider)}


def make_provider(account: dict, save=None) -> Provider:
    cls = PROVIDERS[account["type"]]
    return cls(account.setdefault("data", {}), save)


__all__ = ["PROVIDERS", "make_provider", "Entry", "Provider", "ProviderError",
           "NotFound", "AlreadyExists", "AuthError"]
