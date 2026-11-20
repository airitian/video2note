"""测试环境：在导入任何 core 模块之前，把数据目录指到临时目录、
预置一个管理员令牌。

core.config 在 import 时就根据 V2N_DATA_DIR / V2N_ADMIN_TOKEN 定死
路径与令牌常量，所以这些环境变量必须在 conftest 顶层（pytest 最先
加载的文件）设置，不能等到测试函数里。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["V2N_DATA_DIR"] = tempfile.mkdtemp(prefix="v2n-test-")
os.environ["V2N_ADMIN_TOKEN"] = "test-admin-token"
os.environ.pop("V2N_ACCESS_TOKEN", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ADMIN = {"X-Access-Token": "test-admin-token"}
