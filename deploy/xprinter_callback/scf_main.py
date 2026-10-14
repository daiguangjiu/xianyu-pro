# -*- coding: utf-8 -*-
"""腾讯云 SCF 云函数：把芯烨云打印回调转发到渔集服务器（API 网关触发器）。

**什么时候才需要它**：优先用 API 网关的「HTTP 后端」直连（零代码，见同目录说明）；
网关那份走不通（比如要改写报文、要加重试、要留日志）时，就用这个函数顶上。

填进芯烨后台的地址 = API 网关生成的默认域名，形如：
    https://service-xxxxxxxx-1250000000.gz.apigw.tencentcs.com/release/xprinter

环境变量：
    TARGET_URL    必填：渔集服务器上的回调地址
                  http://62.234.85.96:8000/wms/api/cloudprint/callback
    TARGET_TOKEN  选填：回调令牌（渔集「打印设置 → ④ 云打印回调」里设了就填，没设留空）

为什么要有这一层中转：芯烨只认**域名形式**的 https 地址，而渔集是 http://IP:8000
直连（国内机器上未备案域名跑不了 443），所以借云厂商自带的 https 域名做转发 ——
网关那头是域名，回源那头是 IP，两边都不用备案。
"""
import base64
import json
import os
import urllib.error
import urllib.request

TARGET_URL = os.environ.get(
    "TARGET_URL", "http://62.234.85.96:8000/wms/api/cloudprint/callback"
)
TARGET_TOKEN = os.environ.get("TARGET_TOKEN", "")


def _forward(body: bytes, content_type: str) -> tuple[int, str]:
    url = TARGET_URL
    if TARGET_TOKEN:
        url += ("&" if "?" in url else "?") + "token=" + TARGET_TOKEN
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": content_type}
    )
    # 3 秒是芯烨的硬要求：渔集那边只落库不往外发请求，正常几十毫秒就回
    with urllib.request.urlopen(req, timeout=3) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")[:1000]


def main_handler(event, context):  # noqa: ANN001 云函数入口签名由平台约定
    """API 网关触发器入口：报文原样转发，渔集回什么就回什么。"""
    raw = event.get("body") or ""
    body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode("utf-8")
    headers = event.get("headers") or {}
    ctype = (headers.get("content-type") or headers.get("Content-Type")
             or "application/json")
    try:
        status, text = _forward(body, ctype)
        print(f"[xprinter] forwarded {len(body)}B -> {status} {text[:200]}")
        return {
            "isBase64Encoded": False,
            "statusCode": status,
            "headers": {"Content-Type": "application/json"},
            "body": text or '{"data":"ok"}',
        }
    except (urllib.error.URLError, OSError) as exc:
        # 渔集没接住也要回 ok：回错码会被平台判成「地址无效」，后面一条都收不到
        print(f"[xprinter] forward failed: {exc}")
        return {
            "isBase64Encoded": False,
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": '{"data":"ok"}',
        }
