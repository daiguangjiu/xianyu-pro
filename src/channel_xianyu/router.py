"""Xianyu 路由：草稿查询 + 批量准备草稿（CSV/JSON 导出） + 标记发布完成。

另含「上架到店铺 → 带入闲鱼发布页」的预填通道（见 `publish.py`）：
    POST /xianyu/handoff/{item_id}        生成预填包 + 票据 + 跳转 URL
    GET  /xianyu/handoff/resolve          闲参谋携票据取包（一次性票据）
    GET  /xianyu/publish-package/{item_id} 仅取预填包（调试 / 人工照抄）
"""
from __future__ import annotations

from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.channel_xianyu.listing import save_listing, to_brief
from src.channel_xianyu.prepare import (
    XianyuListingDraft,
    export_csv,
    export_json,
    prepare_pending_listings,
)
from src.channel_xianyu.publish import (
    build_publish_package,
    issue_handoff,
    verify_ticket,
)
from src.deps import get_db
from src.models import ChannelListing, SupplierItem
from src.schemas import (
    ChannelListingOut,
    XianyuListingOut,
    XianyuPrepareOut,
    XianyuSkuOut,
)

router = APIRouter()


def _draft_to_out(draft: XianyuListingDraft) -> XianyuListingOut:
    return XianyuListingOut(
        item_id=draft.item_id,
        title=draft.title,
        suggested_price=draft.suggested_price,
        total_available_stock=draft.total_available_stock,
        main_image=draft.main_image,
        image_count=draft.image_count,
        sku_count=len(draft.skus),
        tags=list(draft.tags),
        description_hint=draft.description_hint,
        category_id=draft.category_id,
        supplier_id=draft.supplier_id,
        prepared_at=draft.prepared_at,
    )


@router.get("/drafts", response_model=list[ChannelListingOut], summary="列出待发布草稿")
async def list_drafts(
    status: str = "draft",
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    rows = (
        db.query(ChannelListing)
        .filter(ChannelListing.status == status)
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [ChannelListingOut.model_validate(r) for r in rows]


@router.post(
    "/prepare-pending",
    response_model=XianyuPrepareOut,
    summary="拉所有 draft → 生成闲鱼铺货 → 写 CSV/JSON",
)
async def prepare_pending(
    item_ids: list[str] | None = Query(default=None, description="限定 item_id 列表"),
    write_files: bool = Query(default=True, description="是否生成 CSV/JSON 落盘"),
    export_dir: str = Query(default="/tmp/xiaoyu_xianyu"),
    db: Session = Depends(get_db),
):
    """拉所有 status=draft 的 ChannelListing → 生成闲鱼草稿 → 落 CSV/JSON。

    本接口“始终只在本地生成”：外部依赖为 0。
    """
    drafts = prepare_pending_listings(db, item_ids=item_ids)

    csv_path = None
    json_path = None
    if write_files and drafts:
        csv_path = str(export_csv(drafts, f"{export_dir}/drafts.csv"))
        json_path = str(export_json(drafts, f"{export_dir}/drafts.json"))

    return XianyuPrepareOut(
        total=len(drafts),
        drafts=[_draft_to_out(d) for d in drafts],
        csv_path=csv_path,
        json_path=json_path,
    )


@router.get(
    "/drafts/{listing_id}/export",
    response_model=XianyuListingOut,
    summary="为单个草稿生成闲鱼上架预览（JSON）",
)
async def export_single_listing(listing_id: int, db: Session = Depends(get_db)):
    """拉单个 ChannelListing → 生成闲鱼端可上架字段。

    与上面的批量接口的区别：仅返回单条 JSON（不做 CSV 落盘）。
    """
    listing = db.get(ChannelListing, listing_id)
    if not listing:
        return {"detail": "not found"}
    drafts = prepare_pending_listings(db, item_ids=[listing.item_id])
    if not drafts:
        return {"detail": "no draft (already published / oversold / removed)"}
    return _draft_to_out(drafts[0])


@router.post("/{listing_id}/mark-published", summary="人工标记草稿为已发布")
async def mark_published(listing_id: int, db: Session = Depends(get_db)):
    """闲鱼客户端手动发布后回调这里，把 status 置为 published。"""
    row = db.get(ChannelListing, listing_id)
    if not row:
        return {"detail": "not found"}
    row.status = "published"
    db.commit()
    return {"ok": True}


# =====================================================================
# 「上架到店铺」→ 带入闲鱼商品发布页（预填包 + 一次性票据）
# =====================================================================
def _get_item_or_404(db: Session, item_id: str) -> SupplierItem:
    item = db.query(SupplierItem).filter(SupplierItem.item_id == item_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail=f"商品不存在：{item_id}")
    return item


@router.get(
    "/publish-package/{item_id}",
    summary="生成闲鱼发布页预填包（标题/描述/价格/库存/图片/规格）",
)
async def publish_package(
    item_id: str,
    channel: str = Query("xianyu", description="下游渠道"),
    db: Session = Depends(get_db),
):
    """只返回预填字段，不签发票据（供人工照抄 / 调试 / 闲参谋直连拉取）。"""
    item = _get_item_or_404(db, item_id)
    return build_publish_package(item, db, channel=channel).to_dict()


@router.post(
    "/handoff/{item_id}",
    summary="上架到店铺：生成预填包 + 一次性票据 + 闲参谋跳转 URL",
)
async def create_handoff(
    item_id: str,
    user: str = Query("", description="操作人（分销商账号 / SSO 用户名）"),
    db: Session = Depends(get_db),
):
    """「带入闲鱼发布页」的入口。

    返回：
    - `publish_url`：闲参谋商品发布页（配置 `XIANMOU_PUBLISH_URL` 时），
      闲参谋凭 `ticket` 调 `GET /xianyu/handoff/resolve` 取预填包后填入表单；
    - `package`：预填字段全量（未接入闲参谋时前端可复制 / 下载 JSON 人工填入）；
    - `ticket` / `expires_at`：一次性短期票据（默认 10 分钟）。

    已被渔枢系统下架的商品（unlisted_at 有值且 is_listed=False）**拒绝带入发布**：
    渔货栈页面按钮已置灰，这里再兜一道，避免绕过页面直接调接口。

    顺手把动作**落库**成一条铺货记录（`channel_listings`，source=handoff、
    handoff_at=此刻）：以前这一步不写库，「我的上架」里看不到任何痕迹，
    返回体里的 `listing` 就是给页面显示「铺货记录 #12（待发布）」用的。
    已上架（published）的记录只补时间戳，状态不动。
    """
    item = _get_item_or_404(db, item_id)
    if getattr(item, "unlisted_at", None) and not item.is_listed:
        raise HTTPException(
            status_code=409,
            detail="该商品已由渔枢系统从渔货栈下架，不能再上架到店铺",
        )
    data = issue_handoff(item, db, user=user).to_dict()
    package = data.get("package") or {}
    row = save_listing(
        db,
        item,
        # 对外接口没有渔货栈登录态，拿调用方传进来的操作人当归属；没传就落成平台级
        # （owner 为空）—— 2026-10-13 起铺货状态按账号隔离，不能挂在别人名下
        owner=str(user or "").strip(),
        source="handoff",
        price=package.get("price"),
        title=package.get("title"),
        commit=True,
    )
    data["listing"] = to_brief(row)
    return data


@router.get(
    "/listing/{item_id}",
    summary="查某款货源在当前渠道的铺货记录（没有返回 {}）",
)
async def get_listing(
    item_id: str,
    owner: str = Query("", description="归属账号（形如 u1）；留空 = 平台级记录"),
    db: Session = Depends(get_db),
):
    """只读查询：这款货在这个账号名下有没有铺过（2026-10-13 起按账号隔离）。"""
    row = (
        db.query(ChannelListing)
        .filter(
            ChannelListing.item_id == item_id,
            ChannelListing.channel == HANDOFF_CHANNEL,
            ChannelListing.owner == str(owner or "").strip(),
        )
        .first()
    )
    return to_brief(row)


@router.get(
    "/handoff/resolve",
    summary="闲参谋携票据换取预填包（票据一次性、短期有效）",
)
async def resolve_handoff(ticket: str = Query(..., description="票据"), db: Session = Depends(get_db)):
    """校验票据 → 返回预填包。

    生产环境应叠加 Redis nonce 消费（`publish.TicketStore`）以保证绝对一次性。
    """
    payload = verify_ticket(ticket)
    if payload is None:
        raise HTTPException(status_code=401, detail="票据无效或已过期")
    item = _get_item_or_404(db, str(payload.get("i") or ""))
    data = build_publish_package(item, db).to_dict()
    data["operator"] = payload.get("u") or ""
    return data