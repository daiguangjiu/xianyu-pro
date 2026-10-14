"""货源模块。

1688 商品同步已于 2026-10-13 整体下线（假数据 2026-10-04 已清，
无定时任务、无入口）。现在的货源来自：渔枢系统上架回传（raw_json.origin
= hub_product，见 app/services/sourcing_publish.py）与自营录入
（raw_json.origin = web_form，见 src/goods/service.py）。
"""
