"""启动入口（可在任意目录运行）：python "D:\\Warehouse Map\\run.py" """
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

from app.main import main  # noqa: E402

if __name__ == "__main__":
    main()
