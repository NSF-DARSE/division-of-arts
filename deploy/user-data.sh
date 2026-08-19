#!/bin/bash
# EC2 bootstrap for the SceneScout demo site (Amazon Linux 2023).
# Runs once, as root, on first boot. Log: /var/log/scenescout-bootstrap.log
set -euxo pipefail
exec > >(tee /var/log/scenescout-bootstrap.log) 2>&1

REPO="${SCENESCOUT_REPO:-https://github.com/talhaMah56/division-of-arts}"
BRANCH="${SCENESCOUT_BRANCH:-main}"
APP=/opt/scenescout

dnf -y install git python3 python3-pip

rm -rf "$APP"
git clone --recurse-submodules --branch "$BRANCH" --depth 1 "$REPO" "$APP"
cd "$APP"

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r deploy/requirements-site.txt

id -u scenescout >/dev/null 2>&1 || useradd --system --home-dir "$APP" scenescout
mkdir -p "$APP/data" "$APP/out"
chown -R scenescout:scenescout "$APP"

cat >/etc/systemd/system/scenescout.service <<'UNIT'
[Unit]
Description=SceneScout demo site
After=network-online.target
Wants=network-online.target

[Service]
User=scenescout
WorkingDirectory=/opt/scenescout
Environment=SCENESCOUT_PRELOAD=1
# Bind :80 without running the whole service as root.
AmbientCapabilities=CAP_NET_BIND_SERVICE
# --preload imports wsgi.py once in the master, so the calendar is seeded
# before workers fork and they cannot race to seed it twice.
ExecStart=/opt/scenescout/.venv/bin/gunicorn --preload -b 0.0.0.0:80 -w 2 \
          --timeout 120 --access-logfile - wsgi:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now scenescout
echo "scenescout bootstrap complete"
