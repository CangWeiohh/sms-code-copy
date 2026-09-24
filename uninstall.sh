#!/bin/bash
# sms-code-copy —— 卸载 launchd 开机自启（不影响项目文件）
set -euo pipefail

LABEL="com.cangwei.sms-code-copy"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_N="$(id -u)"

launchctl bootout "gui/$UID_N/$LABEL" >/dev/null 2>&1 \
    || launchctl unload "$PLIST_PATH" >/dev/null 2>&1 \
    || true

rm -f "$PLIST_PATH"

echo "✅ 已停止服务并移除开机自启: $LABEL"
echo "（config.json / state.json / logs/ 保留在项目目录，可按需手动删除）"
