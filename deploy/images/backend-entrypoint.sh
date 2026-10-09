#!/bin/sh
set -eu
if [ "$#" -ne 1 ]; then
    echo '用法：后端镜像仅接受 api 或 worker 入口' >&2
    exit 64
fi
case "$1" in
    api|worker) exec "$1" ;;
    *) echo '后端镜像入口必须为 api 或 worker' >&2; exit 64 ;;
esac
