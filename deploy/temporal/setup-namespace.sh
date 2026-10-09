#!/bin/sh
# 只等待本地服务启动；不承担 AI Task 的调度或状态迁移。
set -eu

attempt=0
until temporal operator cluster health --address "$TEMPORAL_ADDRESS"; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        echo 'Temporal did not become ready' >&2
        exit 1
    fi
    sleep 2
done

if ! temporal operator namespace describe --address "$TEMPORAL_ADDRESS" \
    --namespace default >/dev/null 2>&1; then
    temporal operator namespace create --address "$TEMPORAL_ADDRESS" --namespace default
fi

exec sleep infinity
