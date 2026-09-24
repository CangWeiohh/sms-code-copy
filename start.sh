#!/usr/bin/env bash
# ============================================================
# sms-code-copy 手动启动脚本（非 launchd 自启场景）
# 用法: ./start.sh
# 以后台方式运行 sms_code_copy.py，PID 写入 run.pid
#
# 注意：与 launchd 自启服务互斥。若自启服务正在运行，请先执行
#   launchctl bootout gui/$(id -u)/com.cangwei.sms-code-copy
# 或彻底卸载自启: ./uninstall.sh
# ============================================================
set -euo pipefail

LABEL="com.cangwei.sms-code-copy"

# 切换到脚本所在目录，保证相对路径（config.json / logs）正确
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "[错误] 未找到 $PYTHON，请先安装 Python3 或用 PYTHON 环境变量指定路径。"
    exit 1
fi

mkdir -p logs

# 与 launchd 自启互斥：避免两个进程同时轮询导致验证码重复复制
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "[错误] 开机自启服务正在运行，为避免重复复制请先停止："
    echo "  launchctl bootout gui/$(id -u)/$LABEL"
    echo "  或彻底卸载自启: ./uninstall.sh"
    exit 1
fi

# 已在运行则退出
if [ -f "run.pid" ] && kill -0 "$(cat run.pid)" 2>/dev/null; then
    echo "程序已在运行 (PID $(cat run.pid))。如需重启请先执行 ./stop.sh"
    exit 1
fi

# 清理残留的失效 PID 文件
rm -f run.pid

# 后台启动（config.json 缺失时脚本会自动生成默认配置）
nohup "$PYTHON" sms_code_copy.py >> logs/console.log 2>&1 &
PID=$!
echo "$PID" > run.pid

# 等待片刻确认进程稳定存活（如未授权完全磁盘访问会立即报错退出）
sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
    echo "[错误] 程序启动后立即退出，最近日志如下（完整日志: logs/console.log）：" >&2
    tail -n 15 logs/console.log >&2 || true
    rm -f run.pid
    exit 1
fi

echo "sms-code-copy 已启动 (PID $PID)"
echo "日志文件: logs/sms-code-copy.log（控制台输出: logs/console.log）"
echo "停止: ./stop.sh"
