#!/usr/bin/env bash
# ============================================================
# sms-code-copy 手动停止脚本（配合 start.sh 使用）
# 用法: ./stop.sh
# 读取 run.pid 优雅停止进程（SIGTERM），最多等待 15 秒后强制结束
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f "run.pid" ]; then
    echo "未找到 run.pid，程序可能未在运行。"
    exit 0
fi

PID="$(cat run.pid)"

if ! kill -0 "$PID" 2>/dev/null; then
    echo "进程 $PID 已不存在，清理 PID 文件。"
    rm -f run.pid
    exit 0
fi

echo "正在停止程序 (PID $PID) ..."
# 发送 SIGTERM，等待优雅退出（游标状态为原子写入，随时可安全中断）
kill "$PID"
for _ in $(seq 1 30); do
    if ! kill -0 "$PID" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

if kill -0 "$PID" 2>/dev/null; then
    echo "程序未在 15 秒内退出，强制结束..."
    kill -9 "$PID" || true
fi

rm -f run.pid
echo "程序已停止。"
