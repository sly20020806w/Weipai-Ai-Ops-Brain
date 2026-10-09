#!/bin/sh
# Temporal 官方 SQL schema 工具管理自身数据库；重复启动不清空数据。
set -eu

: "${POSTGRES_SEEDS:?POSTGRES_SEEDS is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${SQL_PASSWORD:?SQL_PASSWORD is required}"

for database in temporal temporal_visibility; do
    temporal-sql-tool --plugin postgres12 --ep "$POSTGRES_SEEDS" \
        -u "$POSTGRES_USER" -p "${DB_PORT:-5432}" --db "$database" create
    temporal-sql-tool --plugin postgres12 --ep "$POSTGRES_SEEDS" \
        -u "$POSTGRES_USER" -p "${DB_PORT:-5432}" --db "$database" setup-schema -v 0.0

    if [ "$database" = temporal ]; then
        schema_path=/etc/temporal/schema/postgresql/v12/temporal/versioned
    else
        schema_path=/etc/temporal/schema/postgresql/v12/visibility/versioned
    fi
    temporal-sql-tool --plugin postgres12 --ep "$POSTGRES_SEEDS" \
        -u "$POSTGRES_USER" -p "${DB_PORT:-5432}" --db "$database" update-schema -d "$schema_path"
done
