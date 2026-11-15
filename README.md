# CloudMount — облака как диски Windows

Свободная программа с открытым исходным кодом (GNU GPL v3), бесплатный аналог RaiDrive.
Подключает облачные хранилища как обычные диски с буквой: их видно в «Этом компьютере»,
в окнах «Открыть» и «Сохранить» любых программ.

**Сайт:** [cloudmount.ru](https://cloudmount.ru/) (он же [облакокакдиск.рф](https://облакокакдиск.рф/))

| Облако | Как подключается |
|---|---|
| Яндекс Диск | официальный REST API |
| Облако Билайн | WebDAV |
| Облако Mail.ru | WebDAV — **только на платных тарифах** |
| Google Drive | Google Drive API |
| OneDrive | Microsoft Graph |
| Dropbox | Dropbox API v2 |
| pCloud | официальный API |
| Box | официальный API |
| Koofr | WebDAV (10 ГБ бесплатно) |
| Nextcloud / ownCloud | WebDAV |
| S3-хранилища | Yandex Object Storage, VK Cloud, Selectel, Backblaze B2, Cloudflare R2, MinIO |
| SFTP | свой сервер, VPS или NAS |
| WebDAV | любой другой сервер |

## Установка

1. Скачайте **[CloudMount-Setup.exe](https://github.com/marat560-tech/cloudmount/releases/latest/download/CloudMount-Setup.exe)**
   и запустите. Установщик сам поставит драйвер WinFsp, если его нет.
2. Windows может предупредить «Неизвестный издатель» (у программы нет платной подписи):
   «Подробнее» → «Выполнить в любом случае».
3. В программе нажмите **«Добавить облако»**, выберите сервис и следуйте подсказкам —
   в окне настройки есть ссылки на нужные страницы каждого сервиса.

<details><summary>Из исходников / своя сборка</summary>

- Python 3.10+ (галочка «Add python.exe to PATH») и [WinFsp](https://winfsp.dev/rel/), затем `run.bat`.
- Свой exe: `build.bat` → `dist\CloudMount.exe`.
- Каждый push в `main` собирает exe и установщик (вкладка Actions); смена версии в
  `cloudmount/__init__.py` публикует релиз.
- Сайт (папка `docs`) разворачивается на сервере одной командой — см. `deploy/setup-server.sh`;
  сервер сам подтягивает изменения из GitHub каждые 10 минут.
</details>

## Подключение облаков

**Облако Билайн.** cloudbeeline.ru → «Профиль и настройки» → «Безопасность» →
включите «Доступ через WebDAV». Там же логин и пароль.

**Облако Mail.ru** (только платные тарифы). [Mail ID → Безопасность](https://id.mail.ru/security) →
«Пароли для внешних приложений» → создайте пароль с доступом к Облаку.

**Яндекс Диск.** [Создайте приложение](https://oauth.yandex.ru/client/new): платформа «Веб-сервисы»,
Redirect URI `http://127.0.0.1:53682/`, доступы `cloud_api:disk.read`, `cloud_api:disk.write`,
`cloud_api:disk.info`. ClientID и Client secret — в CloudMount → «Войти через браузер».

**Google Drive.** [Включите Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com) →
[экран согласия](https://console.cloud.google.com/auth/audience): External, добавьте себя в Test users →
[создайте клиента](https://console.cloud.google.com/auth/clients) типа «Desktop app».
Пока приложение в режиме «Testing», вход действует 7 дней.

**OneDrive.** [Microsoft Entra → Регистрация приложения](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade):
типы учётных записей «…и личные учётные записи Майкрософт», платформа «Мобильные и
классические приложения», URI перенаправления `http://localhost:53682/`. Нужен только
«Идентификатор приложения (клиента)».

**Dropbox.** [App Console](https://www.dropbox.com/developers/apps) → Create app → Full Dropbox.
Settings → Redirect URIs: `http://127.0.0.1:53682/`; Permissions: `files.metadata.read/write`,
`files.content.read/write`. Нужен App key.

**pCloud.** [My applications](https://docs.pcloud.com/my_apps/) → New app, Redirect URI
`http://127.0.0.1:53682/`. Client ID и Client secret.

**Box.** [Developer Console](https://app.box.com/developers/console) → Custom App → User Authentication
(OAuth 2.0), Redirect URI `http://localhost:53682/`, право «Read and write all files and folders».

**Koofr.** [Preferences → Password](https://app.koofr.net/app/admin/preferences/password) →
App passwords → Generate. Логин — почта.

**Nextcloud.** Адрес сервера, пользователь и пароль приложения (Настройки → Безопасность).

**S3.** Адрес (endpoint), регион, бакет и ключи доступа. Размер диска задайте вручную в
настройках — у S3 нет квоты.

**SFTP.** Сервер, порт, пользователь и пароль или файл ключа. При первом подключении
CloudMount запоминает ключ сервера и откажется подключаться, если он изменится.

### TeraBox (неофициально, через AList)

У TeraBox нет официального API и WebDAV. Подключить его можно через бесплатную программу
[AList](https://alist.nn.ci/) (или её форк OpenList): добавьте в неё TeraBox по
[инструкции AList](https://alist.nn.ci/guide/drivers/terabox.html), включите в AList WebDAV и
подключите его в CloudMount как «WebDAV» (обычно `http://127.0.0.1:5244/dav`).
⚠️ AList использует неофициальный способ доступа: он может перестать работать после
обновления TeraBox и нарушает правила сервиса — используйте на свой риск.

## Как это работает

- Каждый диск — отдельный процесс: зависание одного облака не трогает остальные.
- Список файлов кэшируется на минуту — Проводник работает быстро.
- Маленькие файлы скачиваются целиком, большие читаются кусками по 4 МБ — видео открывается сразу.
- Изменённый файл отправляется в облако при закрытии. Если связь пропала — изменения
  остаются в `%LOCALAPPDATA%\CloudMount\cache` и догружаются автоматически.
- Удалённые файлы попадают в корзину облака (Яндекс, Google, OneDrive, Dropbox, Box).
- Пароли и токены зашифрованы Windows DPAPI — прочитать их может только ваша учётная запись.
- Журналы: «Журналы» внизу окна или `%LOCALAPPDATA%\CloudMount\logs`.

## Контейнеры VeraCrypt

Файлы от 64 МБ отправляются в облако **только при закрытии** (режим можно выключить
в настройках диска):
1. Подключите контейнер в VeraCrypt с облачного диска — при первом изменении он один раз скачается.
2. Работайте с локальной скоростью.
3. Отключите контейнер в VeraCrypt — он загрузится, в окне CloudMount видно «Загрузка … NN%».
   Не выключайте компьютер до 100%.

Держите контейнеры небольшими (бесплатный Яндекс: до 1 ГБ на файл) и не открывайте один
контейнер одновременно на двух компьютерах.

## Если не работает

| Симптом | Что делать |
|---|---|
| «не установлен WinFsp» | Переустановите CloudMount-Setup.exe с галочкой WinFsp |
| «нет связи с облаком или ошибка входа» | ⋯ → Настройки → «Проверить»; для OAuth — «Войти через браузер» |
| «не удалось создать диск» | Другая буква диска или снимите «Показывать как сетевой диск» |
| Неверный размер диска | Задайте «Размер диска, ГБ» в настройках |
| Порт 53682 занят при входе | Закройте rclone или другую программу на этом порту |

## Поддержать автора

Перевод по номеру телефона через СБП: **+7 967 467-10-33**, Т-Банк, получатель Стелла.

## Лицензия

CloudMount — Copyright (C) 2026 Stella, распространяется на условиях
[GNU GPL v3](LICENSE). Программа предоставляется «как есть», без гарантий.

Использует **WinFsp - Windows File System Proxy, Copyright (C) Bill Zissimopoulos** —
https://github.com/winfsp/winfsp ([лицензия](licenses/WinFsp-License.txt)).
