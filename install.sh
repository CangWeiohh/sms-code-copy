#!/bin/bash
# sms-code-copy —— 安装/更新 launchd 开机自启
#
# 用法：
#   ./install.sh                          # 安装并立即启动
#   SMS_CODE_COPY_PYTHON=/path/to/python3 ./install.sh   # 指定 Python 解释器
#
# 卸载：./uninstall.sh
set -euo pipefail

LABEL="com.cangwei.sms-code-copy"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
CONFIG_PATH="$SCRIPT_DIR/config.json"
LOG_DIR="$SCRIPT_DIR/logs"

# 选择 Python 解释器：环境变量优先，否则取 PATH 中的 python3，
# 并解析为真实可执行文件（/usr/bin/python3 等垫片会 exec 到真实二进制）
PYTHON_BIN="${SMS_CODE_COPY_PYTHON:-$(command -v python3)}"
if [ -z "$PYTHON_BIN" ]; then
    echo "错误：未找到 python3，请先安装或通过 SMS_CODE_COPY_PYTHON 指定路径" >&2
    exit 1
fi
PY_REAL="$("$PYTHON_BIN" -c 'import sys; print(sys.executable)')"

echo "项目目录:     $SCRIPT_DIR"
echo "Python:       $PY_REAL"

# 首次安装时生成配置文件
if [ ! -f "$CONFIG_PATH" ]; then
    cp "$SCRIPT_DIR/config.example.json" "$CONFIG_PATH"
    echo "已生成配置文件: $CONFIG_PATH（关键词等可按需修改，支持热重载）"
fi

mkdir -p "$LOG_DIR"

# 语法自检
"$PYTHON_BIN" -m py_compile "$SCRIPT_DIR/sms_code_copy.py"

# 停止已在运行的旧实例
UID_N="$(id -u)"
launchctl bootout "gui/$UID_N/$LABEL" >/dev/null 2>&1 \
    || launchctl unload "$PLIST_PATH" >/dev/null 2>&1 \
    || true

# 生成 LaunchAgent plist（用 plistlib 保证 XML 合法）
"$PYTHON_BIN" - "$PY_REAL" "$SCRIPT_DIR/sms_code_copy.py" "$CONFIG_PATH" "$SCRIPT_DIR" "$LOG_DIR" "$PLIST_PATH" <<'PY'
import plistlib
import sys

python_real, script, config, workdir, log_dir, out_path = sys.argv[1:7]
plist = {
    "Label": "com.cangwei.sms-code-copy",
    "ProgramArguments": [python_real, script, "--config", config],
    "WorkingDirectory": workdir,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Background",
    "StandardOutPath": f"{log_dir}/launchd.log",
    "StandardErrorPath": f"{log_dir}/launchd.err.log",
}
with open(out_path, "wb") as fh:
    plistlib.dump(plist, fh)
PY

# 加载（新版 macOS 用 bootstrap，失败时回退 load）
launchctl bootstrap "gui/$UID_N" "$PLIST_PATH" 2>/dev/null \
    || launchctl load "$PLIST_PATH"

echo ""
echo "✅ 安装完成，服务已启动（开机自启已开启）"
echo "   plist 位置: $PLIST_PATH"
echo ""
echo "常用命令："
echo "   查看状态: launchctl list | grep $LABEL"
echo "   停止运行: launchctl bootout gui/$UID_N/$LABEL"
echo "   重新启动: launchctl bootstrap gui/$UID_N $PLIST_PATH"
echo "   卸载自启: $SCRIPT_DIR/uninstall.sh"
echo ""
echo "⚠️  重要：读取短信需要「完全磁盘访问权限」"
echo "   系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 勾选以下二选一："
echo "   1. 你运行 install.sh 所用的终端 App（终端 / iTerm2 等）"
echo "   2. 本服务使用的 Python 解释器: $PY_REAL"
echo "   授权后如仍未生效，执行: launchctl bootout gui/$UID_N/$LABEL && launchctl bootstrap gui/$UID_N $PLIST_PATH"
