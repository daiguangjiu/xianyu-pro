"""测试环境隔离：必须在任何 app 模块导入前生效，因此放在包的 __init__ 里。

settings 在 app.config 导入时就固定了，若先被其它测试模块导入，
就会沿用 .env 里的正式库地址，测试会写脏正式数据。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

os.environ["DATABASE_URL"] = "sqlite:///data/test.db"
# 渔货栈（src）与渔枢系统同进程时认 HUB_DATABASE_URL：测试里也指向测试库，避免写脏本地库
os.environ["HUB_DATABASE_URL"] = os.environ["DATABASE_URL"]
os.environ["MEDIA_DIR"] = "data/test_media"
os.environ["OCR_PROVIDER"] = "none"          # 测试不依赖真实 OCR，保证确定性
os.environ["NOTIFY_MODE"] = "off"            # 测试不发飞书回执
# 默认不等截图补单号（立即回执），「等待补全订单号」的行为由专门用例开启
os.environ["NOTIFY_WAIT_SCREENSHOT_SECONDS"] = "0"
os.environ.setdefault("FEISHU_APP_ID", "")
os.environ.setdefault("FEISHU_APP_SECRET", "")
# 后台账号：测试库里固定建 admin / xianyu2026 管理员，避免受本地 .env 影响
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "xianyu2026"
os.environ["SECRET_KEY"] = "test-secret-key"
# 通知 / 推单模板一律置空：本地 .env 里可能存着极简模板（如「推单 {{ 收货人 }}」），
# 会让 test_push 对系统内置文案的断言落空 —— 测试要测的是系统内置格式。
# 做法：把 .env 里出现的所有 *_TEMPLATE 键先设成空串（config 的 dotenv 用 setdefault，
# 已存在的键不会被覆盖，删键则会被原值填回 —— 所以必须置空不能删）。
_env_file = ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key = _line.partition("=")[0].strip()
        if _key.endswith("_TEMPLATE"):
            os.environ[_key] = ""

# 每次运行从干净库开始（test.db 在 .gitignore 的 data/ 下）
for suffix in ("", "-wal", "-shm"):
    path = ROOT / "data" / f"test.db{suffix}"
    if path.exists():
        path.unlink()
