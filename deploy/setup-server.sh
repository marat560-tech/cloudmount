#!/usr/bin/env bash
# Установка сайта CloudMount на сервер (Ubuntu / Debian).
# Запуск на сервере под root:
#   curl -fsSL https://raw.githubusercontent.com/marat560-tech/cloudmount/main/deploy/setup-server.sh | bash
# Что делает: ставит nginx, берёт сайт из GitHub (папка docs), настраивает домен,
# получает бесплатный HTTPS-сертификат Let's Encrypt и каждые 10 минут обновляет сайт.
set -euo pipefail

DOMAIN="xn--80aaboujabcc5ae8b.xn--p1ai"        # облакокакдиск.рф
REPO="https://github.com/marat560-tech/cloudmount.git"
SRC="/opt/cloudmount-site"
WEBROOT="/var/www/cloudmount"

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then echo "Запустите от root (sudo -i)"; exit 1; fi
if ! command -v apt-get >/dev/null; then echo "Скрипт рассчитан на Ubuntu/Debian"; exit 1; fi

say "Устанавливаю nginx, git и certbot"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx git certbot python3-certbot-nginx curl >/dev/null

say "Скачиваю сайт из GitHub"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" pull -q --ff-only
else
  git clone -q --depth 1 "$REPO" "$SRC"
fi
mkdir -p "$(dirname "$WEBROOT")"
ln -sfn "$SRC/docs" "$WEBROOT"

say "Настраиваю nginx для облакокакдиск.рф"
cat > /etc/nginx/sites-available/cloudmount <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN www.$DOMAIN;
    root $WEBROOT;
    index index.html;
    charset utf-8;
    location / { try_files \$uri \$uri/ =404; }
    location ~* \.(png|ico|svg|jpg|webp|css|js)$ { expires 7d; }
    location ~ /\. { deny all; }
}
NGINX
ln -sf /etc/nginx/sites-available/cloudmount /etc/nginx/sites-enabled/cloudmount
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx >/dev/null
systemctl reload nginx

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 'Nginx Full' >/dev/null || true
fi

say "Автообновление сайта каждые 10 минут"
cat > /etc/cron.d/cloudmount-site <<CRON
*/10 * * * * root git -C $SRC pull -q --ff-only >/dev/null 2>&1
CRON

say "HTTPS-сертификат"
MYIP=$(curl -fsS4 --max-time 10 https://api.ipify.org || true)
DNSIP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)
if [ -n "$DNSIP" ] && [ "$DNSIP" = "$MYIP" ]; then
  WWW=""
  if [ "$(getent ahostsv4 "www.$DOMAIN" | awk 'NR==1{print $1}' || true)" = "$MYIP" ]; then
    WWW="-d www.$DOMAIN"
  fi
  certbot --nginx -d "$DOMAIN" $WWW --non-interactive --agree-tos \
    --register-unsafely-without-email --redirect
  say "Готово: https://облакокакдиск.рф"
else
  echo "Домен $DOMAIN указывает на '${DNSIP:-никуда}', а этот сервер — $MYIP."
  echo "Добавьте у регистратора домена A-запись @ (и www) → $MYIP,"
  echo "подождите, пока DNS обновится (от минут до нескольких часов), и запустите скрипт ещё раз."
  say "Сайт уже работает по адресу http://$MYIP/ (без домена)"
fi
