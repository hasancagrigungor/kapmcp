#!/usr/bin/env bash
# Deploy to the CloudPanel Python site (no Docker): rsync source, build venv, collectstatic, restart systemd.
set -euo pipefail
HOST=${HOST:-root@5.10.220.31}
SITE=/home/cagrigungor-kapmcp/htdocs/kapmcp.cagrigungor.com
USER_=cagrigungor-kapmcp
cd "$(dirname "$0")/.."
rsync -az --delete --exclude .venv --exclude .git --exclude '__pycache__' --exclude '*.pyc' --exclude web/staticfiles --exclude .env \
  ./ "$HOST:$SITE/app/"
ssh "$HOST" bash -s <<REMOTE
set -euo pipefail
cd $SITE
[ -x venv/bin/python ] || python3.12 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -e "app[web]"
if [ ! -f .env ]; then
  echo "DJANGO_SECRET_KEY=\$(venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(48))')" > .env
  cat >> .env <<'ENV'
DJANGO_ALLOWED_HOSTS=kapmcp.cagrigungor.com,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://kapmcp.cagrigungor.com
SITE_URL=https://kapmcp.cagrigungor.com
KAP_LOG_LEVEL=INFO
MCP_RATE_LIMIT_PER_MINUTE=120
# KAP_API_KEY=
# KAP_API_SECRET=
# KAP_TEST_MODE=1
ENV
  echo ">> created .env — add KAP_API_KEY / KAP_API_SECRET / KAP_TEST_MODE"
fi
chmod 600 .env
cd app/web && DJANGO_ALLOWED_HOSTS=x ../../venv/bin/python manage.py collectstatic --noinput >/dev/null
# CloudPanel's vhost serves *.css/*.js straight from the site root: expose Django's collected files there.
ln -sfn $SITE/app/web/staticfiles $SITE/static
cd $SITE
chown -R $USER_:$USER_ $SITE
install -m 644 $SITE/app/deploy/kapmcp.service /etc/systemd/system/kapmcp.service
systemctl daemon-reload
systemctl enable -q kapmcp
systemctl restart kapmcp
sleep 8
systemctl is-active kapmcp && curl -fsS http://127.0.0.1:8194/healthz && echo
REMOTE
