"""Сведения о программе и реквизиты для поддержки автора.

Чтобы изменить реквизиты, отредактируйте DONATE и пересоберите exe.
"""
from . import __version__

APP_NAME = "CloudMount"
VERSION = __version__
DESCRIPTION = ("Подключает Яндекс Диск, Облако Билайн, Dropbox, Google Drive\n"
               "и WebDAV как обычные диски Windows.")
GITHUB_URL = "https://github.com/marat560-tech/cloudmount"

# Поддержать автора (перевод по номеру телефона через СБП)
DONATE = {
    "name": "",     # имя получателя, например «Марат Х.»
    "bank": "",     # банк, например «Т-Банк»
    "phone": "",    # номер телефона, например «+7 900 000-00-00»
}
