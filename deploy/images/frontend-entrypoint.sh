#!/bin/sh
set -eu
case "$API_UPSTREAM" in
    *[!A-Za-z0-9.:-]*) echo 'API_UPSTREAM 必须是单行 host:port' >&2; exit 64 ;;
esac
if ! printf '%s' "$API_UPSTREAM" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9.-]*:[0-9]{1,5}$'; then
    echo 'API_UPSTREAM 必须是无路径或凭证的 host:port' >&2
    exit 64
fi
port=${API_UPSTREAM##*:}
if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    echo 'API_UPSTREAM 端口必须在 1–65535 之间' >&2
    exit 64
fi
# 只替换这一项环境变量，保留 nginx 的 $host/$http_origin 等变量。
envsubst '${API_UPSTREAM}' < /etc/nginx/weipai.conf.template > /tmp/weipai-nginx.conf
exec nginx -c /tmp/weipai-nginx.conf -g 'daemon off;'
