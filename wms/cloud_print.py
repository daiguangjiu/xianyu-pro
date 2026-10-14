"""芯烨条码云打印机的 API 客户端（XP-420B / XP-D81 / XP-460B / XP-470E / XP-T451B / XP-T451E）。

对接的是**条码云打印机平台**（不是本地网口 / CUPS）：我们的服务在云上，现场那台
XP-420B 只要联网就能出单 —— 现场不用装驱动、不用配内网端口，这正好解决「云端部署
怎么打现场标签」的问题（2026-10-08）。

三个要点（照官方文档 https://www.yuque.com/yikezhineng/tcgo6t/xz045prd11ogpvtl）：

1. **公共参数三件套**：``user``（开发者ID）+ ``timestamp``（10 位秒）+ ``sign``；
   签名 = ``sha1(user + UserKEY + timestamp)``，小写 40 位。
2. **请求体是 JSON**，请求头必须是 ``Content-Type: application/json;charset=UTF-8``。
3. **判断成败只看 ``code``**，0 才是成功；``data`` 成功才有值。

接口清单（本模块只用到这五个）：
  · verifyPrinter         添加打印机（SN 绑到开发者ID下，**打印前必须先绑**）
  · printLabel            打印标签 / 小票（XML 内容，不需要 OSS）
  · queryOrderState       订单是否已打印
  · queryPrinterStatus    打印机在线 / 缺纸（status：0 离线 / 1 在线正常 / 2 在线异常）
  · delPrinterQueue       清空待打印队列

``httpx`` 不在依赖里时本模块不 import 它（只在发请求时才要），免得整套系统起不来。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

BASE_URL = "https://open-barcode.xpyun.net/api/openapi/sprinter"

# 路径 → 说明（页面下拉 / 提示里会用到）
ACTIONS: dict[str, str] = {
    "verifyPrinter": "添加打印机（绑定设备号）",
    "printLabel": "打印标签 / 小票",
    "queryOrderState": "查询订单打印状态",
    "queryPrinterStatus": "查询打印机状态",
    "delPrinterQueue": "清空待打印队列",
}

# 官方错误码 → 现场看得懂的中文（排错指引照文档抄的）
ERRORS: dict[int, str] = {
    -2: "参数无效：检查开发者ID、打印机编号、签名是否正确",
    429: "请求太频繁（触发流控），歇一会儿再试",
    1001: "打印机编号未授权给当前开发者：找芯烨技术客服做设备号授权",
    1002: "打印机未绑定：先在本页「添加打印机」把设备号绑到当前开发者ID下",
    1003: "打印机离线：看机身指示灯（红灯闪烁就是掉线），检查网络并重启打印机",
    1004: "订单保存失败：检查标签内容后重试",
    1005: "订单已被清除（云端订单有保存期限，太久的单查不到）",
    1007: "订单内容超长（超过 65535 字节），拆成几单再发",
    1008: "打印机编号无效：核对机身顶部标签上的编号",
    1010: "添加打印机设备失败，稍后再试或联系售后",
    1012: "用户未注册：检查开发者ID是否已注册到芯烨条码云平台",
    1013: "5 分钟内同一个幂等因子只打印一次（防重复打印）",
    1014: "幂等因子超长（最多 50 个字符）",
    1015: "打印失败：检查打印机联网并重启",
}

# 打印机状态码（queryPrinterStatus 的 data.status）
PRINTER_STATUS: dict[int, str] = {0: "离线", 1: "在线正常", 2: "在线异常（多半是缺纸）"}


class CloudPrintError(Exception):
    """云打印接口返回非 0，或根本没连上。``hint`` 是给现场看的处理建议。"""

    def __init__(self, message: str, code: int = -1, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint or ERRORS.get(int(code), "")


def sign_of(user: str, key: str, timestamp: str) -> str:
    """签名：sha1(user + UserKEY + timestamp)，小写 40 位。"""
    raw = f"{user}{key}{timestamp}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def call(
    action: str,
    *,
    user: str,
    key: str,
    base_url: str = "",
    timeout: float = 10.0,
    **payload: Any,
) -> Any:
    """调一个云接口，成功返回 ``data``；失败抛 :class:`CloudPrintError`。"""
    if action not in ACTIONS:
        raise CloudPrintError(f"没有这个接口：{action}")
    user = str(user or "").strip()
    if not user:
        raise CloudPrintError("还没填开发者ID（去「个人中心 → 我的资料」拿）")
    if not str(key or "").strip():
        raise CloudPrintError("还没填 UserKEY（开发者密钥）")
    stamp = str(int(time.time()))
    body = {
        **payload,
        "user": user,
        "timestamp": stamp,
        "sign": sign_of(user, key, stamp),
    }
    url = f"{str(base_url or '').strip().rstrip('/') or BASE_URL}/{action}"
    try:
        import httpx  # 延迟导入：依赖没装也不影响系统其它部分

        # **中文必须是真 UTF-8 字节，不能用 json= 让它转义成 \u4e2d**
        # （2026-10-09 线上真出过：httpx 的 json= 走 json.dumps 默认 ensure_ascii=True，
        # 品名发出去是一串 \uXXXX；标准 JSON 解析能还原，但平台若直接按文本取字，
        # 打印机收到的就是转义串 —— 打出来一片乱码）。这里自己 dumps 再编成 UTF-8，
        # 两种解析方式都不会错。
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        resp = httpx.post(
            url,
            content=raw_body,
            headers={"Content-Type": "application/json;charset=UTF-8"},
            timeout=timeout,
        )
        data = resp.json()
    except CloudPrintError:
        raise
    except Exception as exc:  # noqa: BLE001 网络层问题全部归成一条人话
        raise CloudPrintError(f"连不上云打印平台（{exc}）", code=-1,
                              hint=f"检查服务器能否访问 {url}") from exc
    raw_code = data.get("code")
    code = int(raw_code) if raw_code is not None else -1
    if code != 0:
        raise CloudPrintError(
            f"{data.get('msg') or '调用失败'}（code {code}）",
            code=code,
        )
    return data.get("data")


# ---------------------------------------------------------------- 五个动作
def verify_printer(sn: str, name: str = "", **auth: Any) -> bool:
    """添加打印机：把机身编号绑到开发者ID下（**打印之前必须调一次**）。"""
    data = call("verifyPrinter", sn=str(sn or "").strip(), name=str(name or "")[:64], **auth)
    return bool(data)


def print_label(
    sn: str,
    content: str,
    *,
    copies: int = 1,
    mode: int = 1,
    idempotent: str = "",
    direct: bool = True,
    **auth: Any,
) -> str:
    """打印标签：content 是 XML（照官方文档的 <PAGE>/<LABEL> 格式）。

    ``mode=1``：不检查在线也生成订单，离线时先缓存，联网后自动打 —— 现场打印机
    偶尔掉线不至于丢单。``idempotent`` 用来防重复打印（5 分钟内同值只打一次）。
    """
    payload: dict[str, Any] = {
        "sn": str(sn or "").strip(),
        "content": str(content or ""),
        "direct": bool(direct),
        "copies": max(1, int(copies or 1)),
        "mode": 1 if int(mode or 1) else 0,
    }
    idem = str(idempotent or "").strip()[:50]
    # 空串不能带：云端会把空串当成一个固定的幂等因子，
    # 于是「5 分钟内只能打一张」（code 1013）—— 测试页连点两次就打不出来（2026-10-09）
    if idem:
        payload["idempotent"] = idem
    return str(call("printLabel", **payload, **auth) or "")


def query_order_state(order_id: str, **auth: Any) -> bool:
    """订单打没打出来（true = 已打印）。"""
    return bool(call("queryOrderState", orderId=str(order_id or "").strip(), **auth))


def query_printer_status(sn: str, **auth: Any) -> dict:
    """打印机状态：0 离线 / 1 在线正常 / 2 在线异常（缺纸）。"""
    data = call("queryPrinterStatus", sn=str(sn or "").strip(), **auth) or {}
    if not isinstance(data, dict):
        data = {}
    status = int(data.get("status") or 0)
    return {
        "status": status,
        "status_text": PRINTER_STATUS.get(status, "未知"),
        "online": status == 1,
        "version": str(data.get("version") or ""),
        "model": str(data.get("printerType") or ""),
    }


def clear_queue(sn: str = "", order_id: str = "", **auth: Any) -> bool:
    """清空待打印队列（按打印机，或按订单号清一单）。"""
    data = call(
        "delPrinterQueue",
        sn=str(sn or "").strip(),
        orderId=str(order_id or "").strip(),
        **auth,
    )
    return bool(data)
