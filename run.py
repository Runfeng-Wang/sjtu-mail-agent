"""启动：python run.py ，然后浏览器打开 http://127.0.0.1:8765"""
import os
import webbrowser

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    if not os.environ.get("NO_BROWSER"):
        webbrowser.open(f"http://127.0.0.1:{port}")
    # 只监听本机，邮件数据不会暴露到局域网
    uvicorn.run("app.server:app", host="127.0.0.1", port=port)
