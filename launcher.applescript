-- YouTube Digest Launcher

set appDir to "/Users/yizhang/Downloads/apply/yt-digest"

-- 释放 8000 端口上的旧进程
do shell script "lsof -ti:8000 | xargs kill -9 2>/dev/null; true"

delay 0.5

-- 后台启动服务器（无 Terminal 窗口）
do shell script "cd " & quoted form of appDir & " && source venv/bin/activate && python3 server.py > /tmp/yt-digest.log 2>&1 &"

-- 等服务器启动（简单 sleep，不用轮询）
delay 2

-- 打开浏览器
open location "http://localhost:8000/"
