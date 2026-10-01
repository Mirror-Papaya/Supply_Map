"""配置工具入口：python run_setup.py check | bitable <链接> | chats"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.setup_tool import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(0 if main(sys.argv) else 1)
