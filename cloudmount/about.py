"""Сведения о программе, лицензии и реквизиты для поддержки автора."""
from . import __version__

APP_NAME = "CloudMount"
VERSION = __version__
DESCRIPTION = ("Облачные хранилища — как обычные диски Windows.\n"
               "Яндекс Диск, Облако Билайн, Mail.ru, Google Drive, OneDrive, Dropbox,\n"
               "pCloud, Box, Koofr, Nextcloud, S3, SFTP и WebDAV.")
GITHUB_URL = "https://github.com/marat560-tech/cloudmount"
SITE_URL = "https://marat560-tech.github.io/cloudmount/"
AUTHOR = "Stella"
LICENSE = "GNU GPL v3"
LICENSE_TEXT = (f"Copyright (C) 2026 {AUTHOR}\n"
                "Свободная программа с открытым исходным кодом: её можно использовать,\n"
                "изучать, изменять и распространять на условиях GNU GPL версии 3.\n"
                "Программа распространяется «как есть», без каких-либо гарантий.")

# Обязательное указание по условиям лицензии WinFsp
WINFSP_NOTICE = "WinFsp - Windows File System Proxy, Copyright (C) Bill Zissimopoulos"
WINFSP_URL = "https://github.com/winfsp/winfsp"

# Поддержать автора (перевод по номеру телефона через СБП)
DONATE = {
    "name": "Стелла",
    "bank": "Т-Банк",
    "phone": "+7 967 467-10-33",
}
