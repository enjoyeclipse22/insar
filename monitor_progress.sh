#!/bin/bash
# 监控重庆InSAR处理进度

echo "=== 重庆InSAR处理进度监控 ==="
echo ""

echo "[1] Python进程状态:"
ps aux | grep python3 | grep -E "run_insar|download" | grep -v grep

echo ""
echo "[2] 数据目录内容:"
ls -lh /root/insar/data_chongqing_large/ 2>/dev/null || echo "目录不存在"

echo ""
echo "[3] 已生成图像:"
ls -lh /root/insar/results/*Chongqing*.jpg 2>/dev/null | wc -l
echo "张图像"

echo ""
echo "[4] 最近日志:"
tail -20 /root/insar/data_chongqing_large/*.log 2>/dev/null || echo "无日志文件"
