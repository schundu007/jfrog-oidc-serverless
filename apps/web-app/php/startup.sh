#!/bin/bash
# App Service Linux PHP start command (set by the azure-web-app stack): serve every path from
# index.php (nginx.conf), then nginx and php-fpm run as the image starts them.
cp /home/site/wwwroot/nginx.conf /etc/nginx/sites-available/default
[ -e /etc/nginx/sites-enabled/default ] || ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
# PHP sees the App Service settings (JFROG_*, IDENTITY_*) only when php-fpm keeps the environment.
www=/usr/local/etc/php-fpm.d/www.conf
if [ -f "$www" ] && ! grep -q '^clear_env *= *no' "$www"; then
  echo 'clear_env = no' >> "$www"
  pkill -USR2 php-fpm 2>/dev/null || true
fi
service nginx reload 2>/dev/null || true
