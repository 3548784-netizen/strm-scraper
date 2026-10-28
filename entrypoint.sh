#!/bin/sh
# 容器入口：首次启动时生成默认配置，之后尊重用户改动
set -e

CONFIG_DIR="${CONFIG_DIR:-/config}"
CONFIG_FILE="$CONFIG_DIR/config.yaml"

mkdir -p "$CONFIG_DIR" "$CONFIG_DIR/logs"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "[init] 未找到配置，生成默认配置：$CONFIG_FILE"
    cp /app/config.example.yaml "$CONFIG_FILE"
fi

# 允许用环境变量覆盖扫描目录，方便同一份镜像服务多个路径
if [ -n "$WS_SCAN__ROOTS" ]; then
    echo "[init] 扫描目录由环境变量指定：$WS_SCAN__ROOTS"
fi

exec "$@"
