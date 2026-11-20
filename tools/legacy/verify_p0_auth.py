"""P0-4 访问控制验证

必须先设置环境变量再 import core.main，让中间件挂上去。
用 TestClient 跑真实 HTTP 栈（含中间件），验证：
  1. 未带令牌访问受保护接口 -> 401
  2. header / cookie / query 三种方式都能通过
  3. state / login 免鉴权（否则无法完成登录流程）
  4. 攻击者改 base_url 的路径被挡住
  5. 未设令牌时行为不变（不破坏本地使用）
"""
import os
import subprocess
import sys

TOKEN = "test-token-abc123"
os.environ["V2N_ACCESS_TOKEN"] = TOKEN
sys.path.insert(0, ".")

from fastapi.testclient import TestClient  # noqa: E402
from core.main import app  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        fail += 1
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))


print("=" * 66)
print("P0-4  无鉴权 -> 任意读写 / base_url 改写导致 API Key 外带")
print("=" * 66)

with TestClient(app) as c:
    # 1) 受保护接口必须拒绝匿名访问
    for path in ["/api/tasks", "/api/health", "/api/settings"]:
        r = c.get(path)
        check(f"匿名 GET {path} 被拒", r.status_code == 401,
              f"HTTP {r.status_code}")

    # 2) 删除任务（P0-1 的毁灭性操作）同样被挡
    r = c.delete("/api/tasks/0d531e9ce1ed")
    check("匿名 DELETE 任务被拒", r.status_code == 401, f"HTTP {r.status_code}")

    # 3) 改 base_url 这条 Key 外带链被挡住
    r = c.post("/api/settings", json={"values": {"asr_base_url": "http://evil.tld"}})
    check("匿名改 base_url 被拒", r.status_code == 401, f"HTTP {r.status_code}")

    # 4) 三种携带令牌的方式都应放行
    r = c.get("/api/tasks", headers={"Authorization": f"Bearer {TOKEN}"})
    check("Bearer header 放行", r.status_code == 200, f"HTTP {r.status_code}")

    r = c.get("/api/tasks", headers={"X-Access-Token": TOKEN})
    check("X-Access-Token 放行", r.status_code == 200, f"HTTP {r.status_code}")

    r = c.get("/api/tasks", params={"token": TOKEN})
    check("query ?token= 放行", r.status_code == 200, f"HTTP {r.status_code}")

    r = c.get("/api/tasks", headers={"Authorization": "Bearer wrong-token"})
    check("错误令牌仍被拒", r.status_code == 401, f"HTTP {r.status_code}")

    # 5) 免鉴权入口必须可访问，否则没法进登录流程
    r = c.get("/api/auth/state")
    check("auth/state 免鉴权", r.status_code == 200 and r.json()["required"] is True,
          str(r.json()))

    # 6) 登录 -> 写入 cookie -> 后续请求自动通过
    r = c.post("/api/auth/login", json={"token": "wrong"})
    check("登录错误令牌被拒", r.status_code == 401, f"HTTP {r.status_code}")

    r = c.post("/api/auth/login", json={"token": TOKEN})
    check("登录正确令牌通过", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code == 200:
        check("下发 httpOnly cookie", "v2n_token" in r.cookies, str(list(r.cookies)))
        cookie = r.cookies.get("v2n_token")
        r = c.get("/api/tasks")
        check("cookie 自动通过校验", r.status_code == 200, f"HTTP {r.status_code}")

        # 7) 静态 media 直连（<video src> 场景）也靠 cookie 放行
        r = c.get("/api/media/0d531e9ce1ed/nope.mp4")
        check("media 不再因匿名 401 而裸奔", r.status_code in (401, 404),
              f"HTTP {r.status_code}")

        c.post("/api/auth/logout")
        r = c.get("/api/tasks")
        check("登出后重新需要令牌", r.status_code == 401, f"HTTP {r.status_code}")

print()
print("=" * 66)
print("回归：不设置令牌时行为必须与过去一致（不破坏本地使用）")
print("=" * 66)

clean = dict(os.environ)
clean.pop("V2N_ACCESS_TOKEN", None)
code = """
import sys
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
from core.main import app
with TestClient(app) as c:
    print("HEALTH", c.get("/api/health").status_code)
    print("TASKS", c.get("/api/tasks").status_code)
    print("STATE", c.get("/api/auth/state").json())
"""
r = subprocess.run([sys.executable, "-c", code], env=clean,
                   capture_output=True, text=True, cwd=".")
out = r.stdout
check("无令牌时 health 仍可访问", "HEALTH 200" in out, out.replace("\n", " | ")[:90])
check("无令牌时 tasks 仍可访问", "TASKS 200" in out)
check("无令牌时 required=False", "'required': False" in out, out.replace("\n", " | ")[:90])

print()
print("=" * 66)
print(f"结果：{ok} 通过 / {fail} 失败")
print("=" * 66)
sys.exit(1 if fail else 0)
