#!/bin/bash
# YouTube Digest 启动脚本
# 用法：bash start.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 释放端口（如果已被占用）
lsof -ti:8000 | xargs kill -9 2>/dev/null

# 首次运行：从模板创建 config.yaml
if [ ! -f "config.yaml" ]; then
  cp config.example.yaml config.yaml
  echo "📝 已创建 config.yaml，请在浏览器设置中填写你的 API Key 和 Obsidian 路径"
fi

# 如果 venv 不存在则自动创建
if [ ! -d "venv" ]; then
  echo "首次运行，安装依赖（约需 1-2 分钟）..."
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt -q
  echo "✅ 依赖安装完成"
else
  source venv/bin/activate
fi

echo ""
echo "🎬 YouTube Digest 启动中..."
echo "   打开浏览器访问: http://localhost:8000"
echo "   Ctrl+C 停止"
echo ""
python3 server.py
