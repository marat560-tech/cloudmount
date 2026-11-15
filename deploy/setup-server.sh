#!/usr/bin/env bash
# Установка сайта CloudMount на сервер (Ubuntu / Debian).
# Запуск на сервере под root:
#   curl -fsSL https://raw.githubusercontent.com/marat560-tech/cloudmount/main/deploy/setup-server.sh | bash
# Что делает: ставит nginx, берёт сайт из GitHub (папка docs), настраивает домен,
# получает бесплатный HTTPS-сертификат Let's Encrypt и каждые 10 минут обновляет сайт.
set -euo pipefail

MAIN="cloudmount.ru"                            # основной адрес сайта
ALIASES="xn--80aaboujabcc5ae8b.xn--p1ai"        # облакокакдиск.рф → перенаправляется на основной
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

say "Настраиваю nginx: $MAIN (и облакокакдиск.рф)"
ALIAS_NAMES=""
for d in $ALIASES; do ALIAS_NAMES="$ALIAS_NAMES $d www.$d"; done
cat > /etc/nginx/sites-available/cloudmount <<NGINX
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name $MAIN www.$MAIN;
    root $WEBROOT;
    index index.html;
    charset utf-8;
    location / { try_files \$uri \$uri/ =404; }
    location ~* \.(png|ico|svg|jpg|webp|css|js)$ { expires 7d; }
    location ~ /\. { deny all; }
}
server {
    listen 80;
    listen [::]:80;
    server_name $ALIAS_NAMES;
    return 301 \$scheme://$MAIN\$request_uri;
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
CERT_ARGS=""
MISSING=""
for d in $MAIN www.$MAIN $ALIAS_NAMES; do
  ip=$(getent ahostsv4 "$d" | awk 'NR==1{print $1}' || true)
  if [ -n "$ip" ] && [ "$ip" = "$MYIP" ]; then
    CERT_ARGS="$CERT_ARGS -d $d"
  else
    MISSING="$MISSING $d(${ip:-нет})"
  fi
done
if echo "$CERT_ARGS" | grep -q -- "-d $MAIN"; then
  certbot --nginx --cert-name cloudmount $CERT_ARGS --expand --non-interactive --agree-tos \
    --register-unsafely-without-email --redirect
  say "Готово: https://$MAIN"
else
  say "Сайт уже работает по адресу http://$MYIP/"
fi
if [ -n "$MISSING" ]; then
  echo "Эти адреса пока не указывают на этот сервер ($MYIP):$MISSING"
  echo "Добавьте у регистратора A-записи @ и www → $MYIP, подождите обновления DNS"
  echo "и запустите скрипт ещё раз — HTTPS включится и для них."
fi
