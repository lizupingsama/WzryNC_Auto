#!/bin/bash
# 在服务器上以 root 执行：bash install.sh
# 装到 /opt/wzry-logserver，数据在 /var/lib/wzry-logserver，监听 8421。
# 重复执行即升级（保留数据与管理口令）。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

install -d -m 755 /opt/wzry-logserver
install -m 644 "$HERE/log_server.py" /opt/wzry-logserver/log_server.py
python3 -c "import ast; ast.parse(open('/opt/wzry-logserver/log_server.py', encoding='utf-8').read())"
install -m 644 "$HERE/wzry-logserver.service" /etc/systemd/system/wzry-logserver.service

systemctl daemon-reload
systemctl enable wzry-logserver >/dev/null
systemctl restart wzry-logserver

for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS http://127.0.0.1:8421/api/ping >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
curl -fsS http://127.0.0.1:8421/api/ping && echo
systemctl --no-pager --lines=5 status wzry-logserver || true
echo "管理口令: $(cat /var/lib/wzry-logserver/admin_token.txt)"
