"""检测入库出库管理子系统（WMS · 检测仓）的页面路由。

**这是独立子系统的门面，不是渔枢系统的一批新页面：**

  · 模板、静态、布局都是检测仓自己的（``wms/templates``），不继承平台的 base；
  · 菜单读 ``wms.nav``，权限看 ``wms.permissions``，跟平台的角色勾选表没关系；
  · ``APIRouter(dependencies=[Depends(_enforce)])`` 是统一守卫：每条路径要什么权限
    由菜单表说了算（GET → 看，POST → 做），**新加页面只要登记菜单就自带保护**；
  · 写操作统一 ``await request.form()``，service 抛 ``ValueError`` 转成提示条；
  · 扫码工作台的三个接口返回 JSON，页面用 fetch 局部刷新（检测员不打断节奏）。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__ as WMS_VERSION
from .. import audit as wms_audit
from .. import auth as wms_auth
from .. import bridge as wms_bridge
from .. import changelog as wms_changelog
from .. import cloud_callback
from .. import grading
from .. import importer
from .. import inspection
from .. import intake
from .. import label_preview
from .. import labeling
from .. import ledger
from .. import nav as wms_nav
from .. import outbound as outbound_service
from .. import parts as parts_service
from .. import permissions as wms_permissions
from .. import repairs as repairs_service
from .. import reports
from .. import seed
from .. import state
from .. import print_settings
from .. import stocktake as stocktake_service
from .. import warehouses as warehouse_service
from ..config import config as wms_config
from ..db import get_db
from ..models import (
    BATCH_SOURCE_LABELS,
    CATEGORY_CHOICES,
    CATEGORY_CONFIDENCE_TONE,
    CATEGORY_LABELS,
    CATEGORY_MAPPING,
    CATEGORY_TREE,
    GRADE_CHOICES,
    GRADE_LABELS,
    LABEL_TYPES,
    SELLABLE_GRADES,
    LABEL_TYPE_LABELS,
    LEDGER_LABELS,
    OUTBOUND_CHOICES,
    OUTBOUND_LABELS,
    OUTBOUND_STATUS_CHOICES,
    OUTBOUND_STATUS_LABELS,
    PART_CHOICES,
    PART_LABELS,
    REPAIR_CHANNEL_LABELS,
    REPAIR_STATUS_CHOICES,
    REPAIR_STATUS_LABELS,
    STATUS_CHOICES,
    STATUS_GRADED,
    STATUS_LABELS,
    STATUS_PENDING,
    STATUS_SELLABLE,
    STATUS_SOLD,
    TERMINAL_STATUSES,
    WH_SALE,
    STOCKTAKE_SCOPE_LABELS,
    STOCKTAKE_STATUS_CHOICES,
    STOCKTAKE_STATUS_LABELS,
    TEST_RESULT_CHOICES,
    TEST_RESULT_LABELS,
    WmsBatch,
    WmsGradeRule,
    WmsImportLog,
    WmsItem,
    WmsItemField,
    WmsLabelPrint,
    WmsPrinter,
    WmsPriceRule,
    WmsRole,
    WmsUserRole,
    WmsTestRecord,
    WmsWarehouse,
    category_label,
    category_leaf,
    category_options,
    category_path,
    ensure_warehouse_names,
    grade_label,
    grade_tone,
    leaf_codes,
    ledger_label,
    location_label,
    part_label,
    status_label,
    status_tone,
    warehouse_label,
)
from ..permissions import PERM_GROUPS
from ..timeutil import fmt as _fmt
from ..timeutil import now as _now

Templates_Dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(Templates_Dir))

PAGE_SIZE = 50
# 一次性提示：短命 cookie（读一次即删），中文不进地址栏
FLASH_COOKIE = "wms_flash"


def _money(value) -> str:
    try:
        return f"{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _dtype(value, pattern: str = "%Y-%m-%d %H:%M") -> str:
    return _fmt(value, pattern)


# 模板全局：等级 / 状态 / 仓库 / 流水的展示文案与配色，页面里直接调
templates.env.globals.update(
    {
        "money": _money,
        "brand": wms_config.brand,
        "system_name": wms_config.system_name,
        "asset_v": wms_config.asset_v,
        "wms_grade_label": grade_label,
        "wms_grade_tone": grade_tone,
        "wms_status_label": status_label,
        "wms_status_tone": status_tone,
        "wms_warehouse_label": warehouse_label,
        "wms_location_label": location_label,
        "wms_category_label": category_label,
        "wms_ledger_label": ledger_label,
        "wms_part_label": part_label,
        "wms_audit_action": wms_audit.action_label,
        "wms_perm_label": wms_permissions.perm_label,
        "wms_conditions_text": grading.conditions_text,
        "wms_version": WMS_VERSION,
    }
)
templates.env.filters["dt"] = _dtype


# ================================================================= 权限守卫
class NeedLogin(Exception):
    """没登录（或登录态失效）。"""


class AccessDenied(Exception):
    """登录了，但没有这一项权限。"""

    def __init__(self, perm: str = "", path: str = ""):
        super().__init__(perm or "no-perm")
        self.perm = perm
        self.path = path


async def _enforce(request: Request, db: Session = Depends(get_db)):
    """统一守卫：所有 ``/wms`` 路由进来先过这一关。

    · 要什么权限由 ``wms.nav`` 的菜单表决定（GET → view，写 → manage / 专属码）；
    · ``/wms/api/*`` 是检测工作台的局部刷新接口，返回 403 JSON 让页面弹提示；
    · 平台管理员天然全权限，其它账号必须在「人员与授权」里分配过角色。
    """
    path = request.url.path
    if not path.startswith("/wms"):
        return
    # 依赖在同一个请求里是单例：这里挂到 state 上，render 算徽标就不用再开会话
    request.state.wms_db = db
    # 仓库 / 区 / 货架的名字缓存（改名后立刻 / 最迟 5 秒全站生效；写不进去也照常渲染）
    ensure_warehouse_names(db)
    op = wms_auth.operator_of(request, db)
    request.state.wms_op = op
    if op is None:
        if path.startswith("/wms/api/"):
            raise HTTPException(401, "登录已失效，请重新登录")
        raise NeedLogin()
    needed = wms_nav.needed_perm(path, request.method)
    if needed and not op.has(needed):
        if path.startswith("/wms/api/"):
            raise HTTPException(
                403, f"没有「{wms_permissions.perm_label(needed)}」的权限"
            )
        raise AccessDenied(needed, path)


def install(app):
    """把守卫的两个异常处理器挂到宿主应用上（渔枢系统的 app 或独立部署的 wms app）。

    子系统自己管自己的异常映射，宿主不需要知道细节 —— 只需 deregister 一行：
    ``install(app)``。
    """

    @app.exception_handler(NeedLogin)
    def _need_login(request: Request, _exc):  # noqa: ANN001
        nxt = request.url.path
        if request.url.query:
            nxt += f"?{request.url.query}"
        return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)

    @app.exception_handler(AccessDenied)
    def _denied(request: Request, exc):  # noqa: ANN001
        return RedirectResponse(
            f"/wms/denied?perm={quote(getattr(exc, 'perm', '') or '')}"
            f"&path={quote(getattr(exc, 'path', '') or '')}",
            status_code=303,
        )

    return app


router = APIRouter(dependencies=[Depends(_enforce)])

# 云打印回调走**另一条不带守卫的路由**：芯烨云平台的服务器不会带我们的登录 Cookie，
# 挂在上面那个 router 上会被 _enforce 打回 401，平台就当这个回调地址无效。
# 合并部署时还得在 ``app/web/auth.py`` 的 PUBLIC_PATHS 里放行同一个路径（那里是
# 精确匹配，路径得一字不差）—— 这是两道门，少开一道都不行。
public_router = APIRouter()


# ---------------------------------------------------------------- 渲染与提示
def _flashes(request: Request) -> list[dict]:
    raw = request.cookies.get(FLASH_COOKIE) or ""
    if not raw:
        return []
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    if isinstance(data, list):
        return data
    return [data] if data.get("msg") else []


def _push_flash(resp: RedirectResponse, msg: str, kind: str = "") -> RedirectResponse:
    payload = json.dumps([{"msg": msg, "kind": kind}], ensure_ascii=False).encode("utf-8")
    resp.set_cookie(
        FLASH_COOKIE,
        base64.urlsafe_b64encode(payload).decode("ascii"),
        max_age=30,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


def render(template: str, request: Request, **ctx) -> HTMLResponse:
    op: wms_auth.Operator | None = getattr(request.state, "wms_op", None)
    perms = op.perms if op else set()
    ctx.setdefault("request", request)
    ctx.setdefault("op", op)
    ctx.setdefault("flashes", _flashes(request))
    ctx.setdefault("nav_sections", wms_nav.visible_sections(perms))
    ctx.setdefault("nav_home", "/wms")
    ctx.setdefault("crumbs", wms_nav.crumbs(request.url.path))
    # 子系统切换（渔枢系统 / 渔货栈 / 渔集）：复用平台的端定义与账号授权，
    # 桥断了就是空列表，顶栏退回「回平台」链接（见 wms/templates/base.html）
    ctx.setdefault("subsystems", wms_bridge.subsystem_options(request))
    # 徽标：待检货品 / 报修超期。算不出来就留空（锦上添花，不该让页面挂）
    db = getattr(request.state, "wms_db", None)
    try:
        ctx.setdefault("badges", wms_nav.badge_counts(db) if db is not None else {})
    except Exception:  # noqa: BLE001
        ctx.setdefault("badges", {})
    resp = templates.TemplateResponse(request=request, name=template, context=ctx)
    if request.cookies.get(FLASH_COOKIE):
        # 提示只展示一次：渲染过就删掉，刷新页面不再重复弹
        resp.delete_cookie(FLASH_COOKIE, path="/")
    return resp


def _redirect(url: str, msg: str = "", kind: str = "") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if msg:
        _push_flash(resp, msg, kind)
    return resp


def _user(request: Request) -> str:
    """操作留痕用的账号名（守卫已经把操作员挂在 ``request.state.wms_op`` 上了）。"""
    op = getattr(request.state, "wms_op", None)
    return (op.username if op else "") or ""


def _form_int(value) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.lstrip("-").isdigit() else None


def _form_money(value) -> float:
    try:
        return round(float(str(value or "").strip() or 0), 2)
    except ValueError:
        return 0.0


def _apply_enabled(form, row) -> None:
    """表单里给了 ``enabled`` 才改，没给就**保持原值**。

    2026-10-09 修 bug：以前写的是 ``row.enabled = str(form.get("enabled") or "")
    not in ("", "0")`` —— 页面表单里压根没有 enabled 这个控件，于是「保存价格系数」
    顺手把这条系数停用。线上 S / A / B / C 四条就是这么被停掉的（10-08 12:00 前后各保存一次），
    停用后 ``price_rule_of`` 取不到规则 → 系数退回 1.0 → 检测台 B 档显示
    「系数 1 × 基准价 ¥87.00 · 毛利 100.0%」。新建的行交给模型默认值（True）。
    """
    if "enabled" not in form:
        return
    row.enabled = str(form.get("enabled") or "") not in ("", "0", "false", "False")


def _ok(data: dict) -> JSONResponse:
    return JSONResponse(data)


def _fail(message: str, status: int = 400, **extra) -> JSONResponse:
    payload = {"ok": False, "message": message}
    payload.update(extra)
    return JSONResponse(payload, status_code=status)


def _warehouses(db: Session) -> list[WmsWarehouse]:
    """**仓库**下拉：只列第 1 级（仓库）且启用的。

    区 / 货架 / 层 / 位是「库位」（货品的 ``location_code``），不是仓 —— 2026-10-08 起
    它们也能在「仓库与货架」页上维护，所以这里必须限定层级，否则出库单的「发货仓」
    会冒出一堆货架名。库位下拉走 ``warehouse_service.location_options()``。
    """
    return list(
        db.scalars(
            select(WmsWarehouse)
            .where(WmsWarehouse.enabled.is_(True), WmsWarehouse.level == 1)
            .order_by(WmsWarehouse.sort.asc(), WmsWarehouse.id.asc())
        ).all()
    )


def _item_or_404(db: Session, item_id: int) -> WmsItem:
    row = db.get(WmsItem, item_id)
    if not row:
        raise HTTPException(404, "货品不存在")
    return row


# ---------------------------------------------------------------- 工作台
@router.get("/wms", response_class=HTMLResponse)
def wms_home(request: Request, db: Session = Depends(get_db)):
    data = reports.dashboard(db)
    return render(
        "wms_overview.html",
        request,
        heading="货品检测入库出库",
        sub="扫码检测 → 分级定价 → 入库上架 → 出库履约 → 报修 / 拆机件闭环",
        data=data,
        grades=[[g, GRADE_LABELS[g]] for g in GRADE_CHOICES],
        recent_ledgers=list(
            db.scalars(
                select(WmsItem).order_by(WmsItem.id.desc()).limit(8)
            ).all()
        ),
    )


# ---------------------------------------------------------------- 批次与建档
@router.get("/wms/batches", response_class=HTMLResponse)
def wms_batches(request: Request, q: str = "", status: str = "",
                db: Session = Depends(get_db)):
    # 「已禁用」不是批次状态（draft/open/closed 照旧），是一个可恢复的开关：
    # 默认列表只给启用中的，选「已禁用」才把停用的那些翻出来（恢复 / 彻底删除在这儿做）
    only_disabled = status == "disabled"
    rows = intake.list_batches(db, q=q, status="" if only_disabled else status,
                               only_disabled=only_disabled)
    # 供应商台账以渔枢「供应商管理」为准（只读桥）：选不出来就提示去找渔枢管理员
    suppliers = wms_bridge.hub_suppliers(db)
    return render(
        "wms_batches.html",
        request,
        heading="批次与建档",
        # 2026-10-08：建批次不再选品类（吃进来的时候还不知道是什么货）——品类按导入清单每行定
        sub="批次单 → 领用号段 → 导入清单建档，品类按每行定（一机一码）",
        batches=rows,
        total=len(rows),
        sources=list(BATCH_SOURCE_LABELS.items()),
        suppliers=suppliers,
        statuses=[["draft", "草稿"], ["open", "进行中"], ["closed", "已完成"],
                  ["disabled", "已停用"]],
        filters={"q": q, "status": status},
    )


@router.post("/wms/batches")
async def wms_batch_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        row = intake.create_batch(db, form, operator=_user(request))
    except ValueError as exc:
        return _redirect("/wms/batches", str(exc), "warn")
    return _redirect(f"/wms/batches/{row.id}", f"批次 {row.batch_no} 已创建，号段 {row.serial_start:04d}~{row.serial_end:04d}")


@router.post("/wms/batches/{batch_id}/disable")
async def wms_batch_disable(request: Request, batch_id: int, db: Session = Depends(get_db)):
    """停用 / 恢复批次（2026-10-08）：批次建错、或者吃完了不想再出现在选择里，就停用。

    **不动任何货品和流水** —— 停用只是让它不再出现在「建档 / 导入 / 检测」的批次选择中，
    历史货品、条码、库存流水一条不少，随时能恢复。真要抹掉痕迹只能删空批次。
    """
    batch = db.get(WmsBatch, batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    going_off = bool(batch.enabled)
    batch.enabled = not going_off
    batch.updated_at = _now()
    wms_audit.log(db, "batch", batch.id, "disable" if going_off else "enable",
                  after="停用" if going_off else "恢复", operator=_user(request),
                  note="不再出现在建档 / 导入 / 检测的批次选择里" if going_off else "")
    db.commit()
    word = "已停用" if going_off else "已恢复"
    tip = "（货品与流水都还在，随时能恢复）" if going_off else ""
    return _redirect(f"/wms/batches/{batch.id}", f"批次 {batch.batch_no} {word}{tip}")


@router.post("/wms/batches/{batch_id}/delete")
async def wms_batch_delete(request: Request, batch_id: int, db: Session = Depends(get_db)):
    """删除批次：**只能删一台货品都没有的空批次**。

    有货品的批次不让删 —— 删了货就跟着没了，账对不上；那种情况用「停用」。
    """
    batch = db.get(WmsBatch, batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    used = db.scalar(select(func.count(WmsItem.id)).where(WmsItem.batch_id == batch.id)) or 0
    if used:
        return _redirect(
            f"/wms/batches/{batch.id}",
            f"这个批次里还有 {used} 台货品，删不得（删了货和流水就都没了）—— "
            f"不想再用它，用「停用」：不再出现在选择里，历史一条不少",
            "warn",
        )
    no = batch.batch_no
    # 连带清掉这一批的导入记录（批次都没了，记录留着只会误导人）
    for log in db.scalars(select(WmsImportLog).where(WmsImportLog.batch_id == batch.id)).all():
        db.delete(log)
    db.delete(batch)
    wms_audit.log(db, "batch", batch_id, "delete", before=no, after="",
                  operator=_user(request), note="空批次，无货品")
    db.commit()
    return _redirect("/wms/batches", f"批次 {no} 已删除")


@router.get("/wms/batches/{batch_id}", response_class=HTMLResponse)
def wms_batch_detail(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(WmsBatch, batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    stats = intake.batch_stats(db, batch.id)
    items = intake.list_items(db, batch_id=batch.id, limit=200)
    return render(
        "wms_batch_detail.html",
        request,
        heading=f"批次 {batch.batch_no}",
        sub=("类目混杂 · 品类按导入清单每行定" if batch.mixed else category_label(batch.category))
        + f" · {BATCH_SOURCE_LABELS.get(batch.source_type, batch.source_type)}"
        + (f" · {batch.supplier_name}" if batch.supplier_name else ""),
        crumb_path="/wms/batches",
        crumb_leaf=batch.batch_no,
        batch=batch,
        stats=stats,
        items=items,
        warehouses=_warehouses(db),
        imports=importer.logs_of_batch(db, batch.id),
        field_labels=importer.field_map(db),
    )


# 规格维度模板（2026-10-12 加，多规格建档用）：口径照渔枢「新建商品」的规格模板 ——
# 类目不同，规格维度就不同：数码看「型号 / 成色 / 容量」，服饰看「尺码 / 颜色」，
# 重货看「重量」。都是**推荐项**：用户自己挑、自己填候选值，规格名也能改。
_SPEC_TEMPLATES: dict[str, list[dict]] = {
    "_default": [{"name": "规格", "values": []},
                 {"name": "成色", "values": ["全新", "几乎全新", "明显使用痕迹"]}],
    "家用电器": [{"name": "型号", "values": []},
                 {"name": "成色", "values": ["全新", "几乎全新", "明显使用痕迹"]}],
    "商用电器": [{"name": "型号", "values": []}, {"name": "功率", "values": []}],
    "工业品": [{"name": "型号", "values": []}, {"name": "规格", "values": []}],
    "电脑、办公": [{"name": "型号", "values": []},
                  {"name": "成色", "values": ["全新", "几乎全新", "明显使用痕迹"]},
                  {"name": "容量", "values": []}],
    "手机通讯": [{"name": "型号", "values": []},
                  {"name": "成色", "values": ["全新", "几乎全新", "明显使用痕迹"]},
                  {"name": "容量", "values": ["128G", "256G", "512G"]}],
    "数码": [{"name": "型号", "values": []}, {"name": "颜色", "values": []}],
    "五金/工具": [{"name": "型号", "values": []}, {"name": "规格", "values": []}],
    "床上用品": [{"name": "尺码", "values": ["1.5m", "1.8m", "2.0m"]},
                  {"name": "颜色", "values": []}],
    "汽车用品": [{"name": "型号", "values": []}, {"name": "颜色", "values": []}],
    "宠物生活": [{"name": "规格", "values": []}, {"name": "重量", "values": []}],
    "母婴": [{"name": "型号", "values": []}, {"name": "颜色", "values": []}],
    "钟表眼镜": [{"name": "型号", "values": []}, {"name": "颜色", "values": []}],
    "元器件": [{"name": "型号", "values": []}, {"name": "封装", "values": []}],
}


def _spec_hints() -> dict[str, str]:
    """类目（一级码 / 三级叶码）→ 一级类目名：前端「应用类目规格模板」按它挑模板。

    建档页的分类只有一个格子，人可能填三级名（破壁机）、三级全路径
    （家用电器 › 厨房小电 › 破壁机）或三级码（HA.KC.DFB）—— 前两种靠文字就能
    匹配模板名，第三种只有码，所以这里把码翻译成一级名再交给前端。
    """
    out: dict[str, str] = {}
    for code, node in CATEGORY_TREE.items():
        label = str((node or {}).get("label") or "")
        if label:
            out[str(code).upper()] = label
    for code in leaf_codes():
        out[str(code).upper()] = out.get(str(code).split(".")[0].upper(), "")
    return out


_SPEC_HINTS: dict[str, str] = _spec_hints()


@router.post("/wms/batches/{batch_id}/items")
async def wms_batch_register(request: Request, batch_id: int, db: Session = Depends(get_db)):
    """建档（手工单条 / 按型号批量）：一机一码，逐台发条码。

    字段口径照供应商清单（2026-10-08）：货号 / 产品 / 品牌 / 一级·二级·三级分类 /
    下单价格（采购成本）/ 基准价 / 序列号 + 检修后自己贴的码 + **商品字段（extra）**。
    品类由分类三列拼出来认，认不出直接拦（不静默落到「其他」—— 品类错了条码里的品类码跟着错）。
    """
    batch = db.get(WmsBatch, batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    form_url = f"/wms/batches/{batch_id}/register"
    if not batch.enabled:
        return _redirect(f"/wms/batches/{batch_id}", _batch_disabled_msg(batch), "warn")
    form = await request.form()
    mode = str(form.get("mode") or "single").strip()
    operator = _user(request)
    raw_cat, category = _category_from_form(form)
    if raw_cat and not category:
        return _redirect(
            form_url,
            f"分类「{raw_cat}」认不出来 —— 写中文（如 破壁机 / 电饭煲）或三级码（如 HA.KC.DFB）",
            "warn",
        )
    extra = _extra_from_form(form, _extra_fields(db))
    # 价格与库存的四个格子（2026-10-12）就是页面顶部那块「批量设置」，输入框名与服务端
    # 字段同名：多规格时当行内留空的默认值，不加规格时就是这一条货自己的值。
    common = {
        "sku_code": str(form.get("sku_code") or "").strip(),
        "product_name": str(form.get("product_name") or "").strip(),
        "brand": str(form.get("brand") or "").strip(),
        "model": str(form.get("model") or "").strip(),
        "spec": str(form.get("spec") or "").strip(),
        "category": category,
        "own_barcode": str(form.get("own_barcode") or "").strip(),
        "purchase_cost": _form_money(form.get("purchase_cost")),
        "selling_price": _form_money(form.get("selling_price")),
        "suggest_price": _form_money(form.get("suggest_price")),
        "base_price": _form_money(form.get("base_price")),
        "qty": form.get("qty"),
        "extra": extra,
    }
    if not common["sku_code"]:
        return _redirect(form_url, "货号必填 —— 扫码检测认货靠它", "warn")
    specs: list[dict] = []
    try:
        # 放 try 里：清单坏了也是「一句人话 + 回表单」，不能变成 500
        specs = _specs_from_form(form)
        if specs:
            # 多规格（2026-10-12）：一个规格 = 一条货 = 一个条码
            created = intake.register(
                db, batch_id, _rows_from_specs(specs, common), operator=operator)
        elif mode == "bulk":
            serials = [
                line.strip()
                for line in str(form.get("serials") or "").splitlines()
                if line.strip()
            ]
            if not serials:
                raise ValueError("请粘贴序列号清单（一行一个）")
            created = intake.register_bulk(
                db, batch_id,
                brand=common["brand"],
                model=common["model"],
                spec=common["spec"],
                product_name=common["product_name"],
                serials=serials,
                purchase_cost=common["purchase_cost"],
                selling_price=common["selling_price"],
                suggest_price=common["suggest_price"],
                base_price=common["base_price"],
                operator=operator,
                category=category,
                sku_code=common["sku_code"],
                own_barcode=common["own_barcode"],
                extra=extra,
            )
        else:
            # 数量（2026-10-10）：一条货 = N 台（同一个条码），不再拆成 N 个码。
            # 库存与价格取批量设置那块（2026-10-12 起 ③ 价格与标识并进 ② 了）
            qty = intake.norm_qty(form.get("qty"), hi=200)
            rows = [
                {**common, "serial_no": str(form.get("serial_no") or "").strip(),
                 "qty": qty}
            ]
            created = intake.register(db, batch_id, rows, operator=operator)
    except ValueError as exc:
        return _redirect(form_url, str(exc), "warn")
    first = created[0].display_code if created else ""
    total = sum(int(row.qty or 0) or 1 for row in created)
    if specs:
        msg = (f"已按 {len(specs)} 个规格建档 {len(created)} 条 / 共 {total} 台"
               f"（{first} 起 {len(created)} 个号）")
    else:
        msg = (f"已建档 {len(created)} 条 / 共 {total} 台（{first}"
               + (f" 起 {len(created)} 个号）" if len(created) > 1 else "）"))
    return _redirect(f"/wms/batches/{batch_id}", msg)


@router.get("/wms/batches/{batch_id}/register", response_class=HTMLResponse)
def wms_batch_register_form(request: Request, batch_id: int, db: Session = Depends(get_db)):
    """手动建档**独立页**（2026-10-08 用户要求）：字段与 Excel 清单一致，单条也能建。

    以前这张表单挤在批次详情页里，字段也少（没有商品字段）—— 现在搬到独立页，
    和「编辑货品」共用同一张模板，两边字段口径完全一样。
    """
    batch = db.get(WmsBatch, batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    if not batch.enabled:
        return _redirect(f"/wms/batches/{batch_id}", _batch_disabled_msg(batch), "warn")
    return render(
        "wms_item_form.html",
        request,
        heading="手动建档",
        sub=f"批次 {batch.batch_no} · 字段与 Excel 清单一致（一机一码）",
        crumb_path="/wms/batches",
        crumb_leaf=batch.batch_no,
        mode="create",
        batch=batch,
        item=None,
        form_action=f"/wms/batches/{batch.id}/items",
        values=_blank_values(),
        extra_fields=_extra_fields(db, only_enabled=True),
        extra_values={},
        category_choices=category_options(),
        spec_templates=_SPEC_TEMPLATES,
        spec_hints=_SPEC_HINTS,
    )


@router.get("/wms/batches/{batch_id}/next-sku")
def wms_batch_next_sku(batch_id: int, db: Session = Depends(get_db)) -> dict:
    """「系统生成货号」按钮（2026-10-12 用户要求）：发一个 ``YC + 年月 + 流水`` 的货号。

    非扫码场景用 —— 货上没贴清单号，人也不知道该编什么号，就让系统发。
    只发号不落库（建档提交时才建货品），所以点几次不会留下一堆空货。
    路径挂在 /wms/batches 前缀下，权限跟着建档页走（wms.batch.view）。
    """
    if not db.get(WmsBatch, batch_id):
        raise HTTPException(404, "批次不存在")
    return {"sku_code": intake.next_sku_code(db)}


# ---------------------------------------------------------------- Excel 导入建档
# 批次建好的时候只知道「从谁那吃进来的一批货」，品牌型号规格全在供应商那张表里 ——
# 所以建档走 Excel：上传 → 认表头 → 预览（改映射 / 把多余的列沉淀成商品字段）→ 执行。
def _batch_disabled_msg(batch) -> str:
    """停用的批次不再收新货（2026-10-08）：建档 / 导入都得先在批次页「恢复」。"""
    return "" if (batch and batch.enabled) else "这个批次已停用 —— 回批次页点「恢复」才能继续操作"


# ------------------------------------------------- 「建档 / 编辑货品」共用这套解析
# 2026-10-08 用户要求：建档独立成页、手动单条也能建、**字段与 Excel 清单一致**，
# 而且导入进来的货要能改能删（编辑页和新建页共用）。所以两边共用同一张模板
# （wms_item_form.html）和下面这套表单解析，字段口径不会再各说各话。
def _attach_qty(db: Session, lines: list) -> list:
    """给明细行（出库 / 盘点）挂上「这台货到底有几台」（2026-10-10）。

    明细表只存 item_id 与当时拍下来的快照，数量在货品身上 —— 页面要按台数显示就得带出来。
    挂在实例上的 ``qty`` 只是临时属性，**不落库**。
    """
    from ..models import WmsItem

    ids = [int(getattr(x, "item_id", 0) or 0) for x in lines]
    ids = [i for i in ids if i]
    qty_by_item: dict[int, int] = {}
    if ids:
        for row in db.execute(select(WmsItem.id, WmsItem.qty).where(WmsItem.id.in_(ids))):
            qty_by_item[int(row[0])] = int(row[1] or 0) or 1
    for x in lines:
        x.qty = qty_by_item.get(int(getattr(x, "item_id", 0) or 0), 1)
    return lines


def _extra_fields(db: Session, only_enabled: bool = False) -> list:
    """商品字段清单（建档页只给启用中的；编辑页全给，停用的标出来，老值才不会凭空消失）。"""
    return importer.list_fields(db, only_enabled=only_enabled)


def _extra_from_form(form, fields) -> dict:
    """表单里的 ``extra__<key>`` → ``{key: 值}``（表单里没出现的字段一律不动）。"""
    out: dict = {}
    for f in fields:
        value = form.get(f"extra__{f.key}")
        if value is not None:
            out[str(f.key)] = str(value).strip()
    return out


def _category_from_form(form) -> tuple[str, str]:
    """(填的原文, 三级品类码)：三列分类拼串，或直接写品类；认不出来返回空码。"""
    parts = [str(form.get(f"category_l{i}") or "").strip() for i in (1, 2, 3)]
    raw = str(form.get("category") or "").strip() or " ".join(p for p in parts if p)
    return raw, (importer.match_category(raw) if raw else "")


def _specs_from_form(form) -> list[dict]:
    """多规格建档（2026-10-12）：规格矩阵由前端序列化进 ``specs_json``。

    没填规格（空串）就返回空表 —— 这时走原来「一机一码 / 序列号清单」那条路，
    老用法与老测试一点不变。
    """
    raw = str(form.get("specs_json") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("规格清单读不出来 —— 刷新页面重填一次")
    if not isinstance(data, list):
        raise ValueError("规格清单格式不对")
    out: list[dict] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        out.append({
            "spec": str(row.get("spec") or "").strip(),
            "qty": str(row.get("qty") or "").strip(),
            "model": str(row.get("model") or "").strip(),
            "sku_code": str(row.get("sku_code") or "").strip(),
            "purchase_cost": str(row.get("purchase_cost") or "").strip(),
            "selling_price": str(row.get("selling_price") or "").strip(),
            "suggest_price": str(row.get("suggest_price") or "").strip(),
            "base_price": str(row.get("base_price") or "").strip(),
        })
    return [r for r in out if r["spec"]]


def _rows_from_specs(specs: list[dict], common: dict) -> list[dict]:
    """规格矩阵 → 建档行：**一个规格 = 一条货 = 一个条码**，台数记在这一条上。

    字段照渔枢「规格 · 价格 · 库存」：成本价 / 渔货价 / 建议售价 / 市场价 / 库存。
    行里留空的字段回落公共值（就是页面顶部那块批量设置，输入框名与服务端字段同名）；
    多规格时货号自动派生 ``货号-01 / -02``（跟渔枢 SKU 编码派生同一套口径），
    逐行填了的照填的走。
    """
    base_sku = str(common.get("sku_code") or "").strip()
    many = len(specs) > 1
    rows: list[dict] = []
    for idx, spec in enumerate(specs, 1):
        row = dict(common)
        row["spec"] = spec["spec"] or str(common.get("spec") or "")
        row["qty"] = intake.norm_qty(
            spec["qty"] or common.get("qty"), hi=200
        )
        row["model"] = spec["model"] or str(common.get("model") or "")
        row["sku_code"] = spec["sku_code"] or (
            f"{base_sku}-{idx:02d}" if many else base_sku
        )
        for key in ("purchase_cost", "selling_price", "suggest_price", "base_price"):
            row[key] = _form_money(spec[key]) or float(common.get(key) or 0.0)
        row["serial_no"] = ""
        rows.append(row)
    return rows


def _blank_values() -> dict:
    """空表单的默认值（建档页用；跟编辑页共用模板）。

    价格四个格子留空串（不是 "0"）—— 页面上就能看见 placeholder 的提示语
    （「留空 = 检测时按系数算」这种），不用先删掉一个 0。
    """
    return {
        "sku_code": "", "product_name": "", "brand": "", "model": "", "spec": "",
        "serial_no": "", "own_barcode": "", "category_raw": "",
        "purchase_cost": "", "selling_price": "", "suggest_price": "", "base_price": "",
        "qty": 1,
    }


def _values_of(item) -> dict:
    """货品 → 表单值（编辑页回填）。品类栏回填中文名（人看得懂，保存时照样认得出来）。"""
    return {
        "sku_code": str(item.sku_code or ""),
        "product_name": str(item.product_name or ""),
        "brand": str(item.brand or ""),
        "model": str(item.model or ""),
        "spec": str(item.spec or ""),
        "serial_no": str(item.serial_no or ""),
        "own_barcode": str(item.own_barcode or ""),
        "category_raw": category_label(item.category) if item.category else "",
        "purchase_cost": f"{float(item.purchase_cost or 0):g}",
        "selling_price": f"{float(item.selling_price or 0):g}",
        "suggest_price": f"{float(item.suggest_price or 0):g}",
        "base_price": f"{float(item.base_price or 0):g}",
        "qty": int(item.qty or 0) or 1,
    }


@router.get("/wms/batches/{batch_id}/import", response_class=HTMLResponse)
def wms_batch_import(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(WmsBatch, batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    if not batch.enabled:
        return _redirect(f"/wms/batches/{batch_id}", _batch_disabled_msg(batch), "warn")
    return render(
        "wms_batch_import.html",
        request,
        heading=f"导入建档 · 批次 {batch.batch_no}",
        sub="上传供应商的货品清单（xlsx / csv）→ 认表头 → 逐行建档，货号后面扫码就认货",
        crumb_path="/wms/batches",
        crumb_leaf=batch.batch_no,
        batch=batch,
        step="upload",
        standard_fields=importer.STANDARD_FIELDS,
        fields=importer.list_fields(db, only_enabled=True),
        logs=importer.logs_of_batch(db, batch.id),
    )


@router.get("/wms/batches/{batch_id}/import/template")
def wms_import_template(batch_id: int, db: Session = Depends(get_db)):
    """导入模板：标准表头 + 三行示例（照着填就不会认错列）。"""
    batch = db.get(WmsBatch, batch_id)
    data = importer.build_template(batch)
    # 文件名只放 ASCII（批次号是数字与横杠）：中文名进 Content-Disposition 会被 latin-1 编码卡住
    name = f"wms-import-template-{batch.batch_no if batch else 'wms'}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/wms/batches/{batch_id}/import/preview")
async def wms_import_preview(request: Request, batch_id: int, db: Session = Depends(get_db)):
    """解析上传的表 → 映射预览（认错的列在这里改，多出来的列在这里登记成商品字段）。"""
    batch = db.get(WmsBatch, batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    if not batch.enabled:
        return _redirect(f"/wms/batches/{batch_id}", _batch_disabled_msg(batch), "warn")
    form = await request.form()
    upload = form.get("file")
    name = getattr(upload, "filename", "") or ""
    data = await upload.read() if hasattr(upload, "read") else b""
    if not data:
        return _redirect(f"/wms/batches/{batch_id}/import", "没有收到文件", "warn")
    try:
        token = importer.save_upload(data, name)
        table = importer.read_table(importer.upload_path(token))
    except ValueError as exc:
        return _redirect(f"/wms/batches/{batch_id}/import", str(exc), "warn")

    headers = table["headers"]
    rows = table["rows"]
    if len(rows) > importer.IMPORT_MAX_ROWS:
        return _redirect(
            f"/wms/batches/{batch_id}/import",
            f"这张表 {len(rows)} 行，超过单次 {importer.IMPORT_MAX_ROWS} 行上限（拆开导）",
            "warn",
        )
    mapping = importer.guess_mapping(headers)
    # 预检（2026-10-08）：按当前认出来的列先跑一遍校验 —— 有毛病就摆在这儿，
    # 别让人点完「确认导入」才发现整批一台都没进来（以前失败只在提示条里闪一次）
    _, preview_errors, _, preview_unknown = importer.validate_rows(
        db, batch, headers=headers, rows=rows, mapping=mapping,
        line_base=int(table.get("header_row") or 1) + 1,
    )
    # 每列带三个示例值：认列的依据是「看内容」，不能只给个表头让人猜
    samples: list[list[str]] = []
    for idx in range(len(headers)):
        vals = [str(r[idx]).strip() for r in rows[:5]
                if idx < len(r) and str(r[idx] or "").strip()]
        samples.append(vals[:3])
    used = {f.key for f in importer.list_fields(db)}
    for f in importer.STANDARD_FIELDS:
        used.add(f["key"])
    slugs = [importer.slugify(h, set(used)) for h in headers]
    return render(
        "wms_batch_import.html",
        request,
        heading=f"确认映射 · 批次 {batch.batch_no}",
        sub=f"共 {len(rows)} 行 · 认对的列直接用，多出来的列可以沉淀成商品字段",
        crumb_path="/wms/batches",
        crumb_leaf=batch.batch_no,
        batch=batch,
        step="preview",
        token=token,
        file_name=name,
        header_row=int(table.get("header_row") or 1),
        preview_errors=preview_errors,
        preview_unknown=preview_unknown,  # 认不出来的分类（让人当场指定兜底类目）
        unknown_rows=sum(len(u.get("lines") or []) for u in preview_unknown),
        categories=category_options(),
        headers=headers,
        slugs=slugs,
        samples=samples,
        mapping=mapping,
        row_count=len(rows),
        preview_rows=rows[:8],
        standard_fields=importer.STANDARD_FIELDS,
        log_count=len(importer.logs_of_batch(db, batch.id)),
    )


@router.post("/wms/batches/{batch_id}/import/commit")
async def wms_import_commit(request: Request, batch_id: int, db: Session = Depends(get_db)):
    """执行导入：逐行建档（发条码 + 入库流水），多出来的列沉淀成商品字段。"""
    batch = db.get(WmsBatch, batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    if not batch.enabled:
        return _redirect(f"/wms/batches/{batch_id}", _batch_disabled_msg(batch), "warn")
    form = await request.form()
    token = str(form.get("token") or "")
    path = importer.upload_path(token)
    if path is None:
        return _redirect(f"/wms/batches/{batch_id}/import", "文件已过期，请重新上传", "warn")
    try:
        table = importer.read_table(path)
    except ValueError as exc:
        return _redirect(f"/wms/batches/{batch_id}/import", str(exc), "warn")

    headers = table["headers"]
    mapping: dict[int, str] = {}
    extra_specs: list[dict] = []
    for idx in range(len(headers)):
        value = str(form.get(f"map_{idx}") or "").strip()
        if not value:
            continue
        if value == "__extra__":
            key = str(form.get(f"key_{idx}") or "").strip() or importer.slugify(headers[idx], set())
            field_type = str(form.get(f"type_{idx}") or "text").strip()
            mapping[idx] = f"extra:{key}"
            extra_specs.append(
                {"col": idx, "key": key, "label": str(headers[idx] or key), "type": field_type}
            )
        else:
            mapping[idx] = value
    if not any(v == "sku_code" for v in mapping.values()):
        return _redirect(f"/wms/batches/{batch_id}/import",
                         "必须给一列指定为「货号」—— 后面扫码检测靠它认货", "warn")
    # 默认「有毛病的行跳过、其余照导」（2026-10-08）：几十行脏数据不该挡住几百行好货，
    # 导完在提示条和「导入记录」里报清楚成功多少台、跳过多少行。
    # 勾了「严格模式」才退回老规矩：任一行不合法就整批不导。
    strict = str(form.get("strict_mode") or "").strip().lower() in ("1", "on", "true", "yes")
    skip_bad = not strict
    # 分类认不出来的行按这个类目建档（人在预览页当场指定的兜底）
    cat_fallback = str(form.get("cat_fallback") or "").strip().upper()
    try:
        result = importer.apply_import(
            db, batch.id,
            headers=headers, rows=table["rows"], mapping=mapping,
            extra_specs=extra_specs, operator=_user(request),
            file_name=str(form.get("file_name") or ""),
            line_base=int(table.get("header_row") or 1) + 1,
            skip_bad_rows=skip_bad,
            cat_fallback=cat_fallback,
        )
    except ValueError as exc:
        return _redirect(f"/wms/batches/{batch_id}/import", str(exc), "warn")

    # 导完清掉落盘的临时表（留着没用，还占空间）
    try:
        path.unlink()
    except OSError:  # noqa: BLE001
        pass
    if not result["ok"]:
        head = "；".join(result["errors"][:3])
        more = f"（还有 {len(result['errors']) - 3} 条）" if len(result["errors"]) > 3 else ""
        return _redirect(
            f"/wms/batches/{batch_id}/import",
            f"整批没导入（{len(result['errors'])} 处毛病，原因都记在下面的「导入记录」里）："
            f"{head}{more}",
            "warn",
        )
    msg = f"导入完成：成功 {result['ok']} 台"
    if result.get("skipped"):
        msg += f"，失败 {result['skipped']} 行（已跳过，没进商品表；原因按行号记在「导入记录」里）"
    msg += f"（批次 {batch.batch_no}）"
    if result["fields"]:
        msg += f"，并沉淀了 {len(result['fields'])} 个商品字段：" + "、".join(result["fields"])
    return _redirect(f"/wms/batches/{batch_id}", msg)


# ---------------------------------------------------------------- 商品字段
# Excel 里认不出来的列，登记在这里就成了商品表自己的字段（改名 / 改类型 / 停用 / 排序）
@router.get("/wms/fields", response_class=HTMLResponse)
def wms_fields(request: Request, db: Session = Depends(get_db)):
    rows = importer.list_fields(db)
    used: dict[str, int] = {}
    for (extra,) in db.execute(select(WmsItem.extra)).all():
        for k in dict(extra or {}):
            used[k] = used.get(k, 0) + 1
    return render(
        "wms_fields.html",
        request,
        heading="商品字段",
        sub="导入表格里多出来的列沉淀在这里 —— 改名 / 改类型 / 停用 / 排序，值跟着货品走",
        fields=rows,
        used=used,
        standard_fields=importer.STANDARD_FIELDS,
        types=[["text", "文本"], ["number", "数字"], ["date", "日期"]],
    )


@router.post("/wms/fields")
async def wms_field_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    field_id = _form_int(form.get("field_id"))
    row = db.get(WmsItemField, field_id) if field_id else WmsItemField()
    if not field_id:
        key = importer.slugify(str(form.get("key") or form.get("label") or ""), set())
        if not key:
            return _redirect("/wms/fields", "字段名不能为空", "warn")
        if db.scalar(select(WmsItemField).where(WmsItemField.key == key)):
            return _redirect("/wms/fields", f"字段标识 {key} 已存在", "warn")
        row.key = key
        # 手工加的接在最后（字段以导入表头为准，手工补的排后面），来源写「手工添加」
        row.sort = int(db.scalar(select(func.max(WmsItemField.sort))) or 0) + 10
        row.source = "手工添加"
        row.enabled = True
        row.created_at = _now()
        db.add(row)
    row.label = str(form.get("label") or row.label or row.key).strip()
    row.field_type = str(form.get("field_type") or "text").strip()
    # 启用：编辑表单带勾选框，按勾选定；手工新增默认启用（上面已置 True）
    if field_id:
        row.enabled = str(form.get("enabled") or "") not in ("", "0")
    raw_sort = str(form.get("sort") or "").strip()
    if raw_sort:
        row.sort = _form_int(raw_sort) or row.sort
    row.note = str(form.get("note") or "").strip()
    row.updated_at = _now()
    db.commit()
    return _redirect("/wms/fields", f"字段「{row.label}」已保存")


@router.post("/wms/fields/{field_id}/toggle")
def wms_field_toggle(field_id: int, db: Session = Depends(get_db)):
    row = db.get(WmsItemField, field_id)
    if not row:
        raise HTTPException(404, "字段不存在")
    row.enabled = not row.enabled
    row.updated_at = _now()
    db.commit()
    return _redirect("/wms/fields", f"字段「{row.label}」已{'启用' if row.enabled else '停用'}",
                     "" if row.enabled else "warn")


@router.post("/wms/fields/{field_id}/move")
def wms_field_move(field_id: int, dir: str = "", db: Session = Depends(get_db)):
    """商品字段排序（2026-10-08）：上移 / 下移一位 —— 比手填排序数字省事，也不会撞号。"""
    try:
        importer.move_field(db, field_id, str(dir or "up"))
    except ValueError as exc:
        return _redirect("/wms/fields", str(exc), "warn")
    db.commit()
    return _redirect("/wms/fields", "顺序已调整")


@router.post("/wms/fields/{field_id}/delete")
def wms_field_delete(field_id: int, db: Session = Depends(get_db)):
    row = db.get(WmsItemField, field_id)
    if not row:
        raise HTTPException(404, "字段不存在")
    label = row.label
    db.delete(row)
    db.commit()
    return _redirect("/wms/fields", f"字段「{label}」已删除（已存的值不受影响）", "warn")


# ---------------------------------------------------------------- 库存台账
@router.get("/wms/items", response_class=HTMLResponse)
def wms_items(request: Request, q: str = "", status: str = "", grade: str = "",
              warehouse: str = "", category: str = "", page: int = 1,
              db: Session = Depends(get_db)):
    # 分页（2026-10-11 用户：「库存台账只有 200 条记录，我们有 867 个商品」）：
    # 以前是 limit=PAGE_SIZE*4 一刀切 200 条、没有下一页，翻不到后面的货；
    # 现在按 PAGE_SIZE（50）一页真分页，标题里的「共 N 台」也改成真 count（不再等于当前页行数）。
    total_count = intake.count_items(
        db, q=q, status=status, grade=grade, warehouse=warehouse, category=category,
    )
    pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(int(page or 1), pages))
    rows = intake.list_items(
        db, q=q, status=status, grade=grade, warehouse=warehouse,
        category=category, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
    )
    data = ledger.summary(db)
    return render(
        "wms_items.html",
        request,
        heading="库存台账",
        sub="任意货品 3 秒内查到「在哪、什么状态、什么价」",
        items=rows,
        total=total_count,
        page=page, pages=pages, page_size=PAGE_SIZE,
        row_base=(page - 1) * PAGE_SIZE,
        summary=data,
        statuses=[[s, STATUS_LABELS[s]] for s in STATUS_CHOICES],
        grades=[[g, GRADE_LABELS[g]] for g in GRADE_CHOICES],
        categories=category_options(),
        warehouses=_warehouses(db),
        filters={"q": q, "status": status, "grade": grade,
                 "warehouse": warehouse, "category": category},
    )


@router.get("/wms/items/{item_id}", response_class=HTMLResponse)
def wms_item_detail(request: Request, item_id: int, db: Session = Depends(get_db)):
    item = _item_or_404(db, item_id)
    records = inspection.records_of(db, item.id)
    detail: list[dict] = []
    for record in records:
        detail.append({"record": record, "lines": inspection.items_of(db, record.id)})
    return render(
        "wms_item_detail.html",
        request,
        heading=item.display_code,
        sub=f"{item.title} · {grade_label(item.grade)} · {status_label(item.status)}",
        crumb_path="/wms/items",
        crumb_leaf=item.display_code,
        item=item,
        records=detail,
        field_labels=importer.field_map(db),
        ledgers=ledger.item_ledgers(db, item.id),
        logs=wms_audit.of_entity(db, "item", item.id),
        part_rows=parts_service.of_item(db, item.id),
        part_choices=[[p, PART_LABELS[p]] for p in PART_CHOICES],
        warehouses=_warehouses(db),
        # 库位只能从「仓库与货架」维护的表里选（手打错了货就挂到一个不存在的库位上）
        locations=warehouse_service.location_options(db),
        # 短码第 1 位「第几个仓」的对照表：库位字段旁边那个「?」要说清 1~7 各是哪个仓
        wh_roots=warehouse_service.root_index(db),
        next_statuses=[[s, STATUS_LABELS[s], state.action_label(item.status, s)]
                       for s in state.allowed_targets(item.status)],
        ledger_labels=LEDGER_LABELS,
        # 渔货价按「市场价 × 该档系数」先算一个数填进改价格里（2026-10-10）：
        # 现场看着改，不用自己拿计算器乘。已经出过价的就显示现有渔货价。
        # 建档 / 编辑时人工填过建议售价的（2026-10-12）以人填的为准 —— 与封顶口径一致
        quote=grading.price_of(
            db, item.grade, float(item.base_price or 0.0), float(item.purchase_cost or 0.0),
            manual_suggest=float(item.suggest_price or 0.0) or None,
        ),
        default_price=float(item.selling_price or 0.0)
        or float(grading.price_of(db, item.grade, float(item.base_price or 0.0),
                                  float(item.purchase_cost or 0.0),
                                  manual_suggest=float(item.suggest_price or 0.0) or None,
                                  ).get("price") or 0.0),
    )


@router.get("/wms/items/{item_id}/edit", response_class=HTMLResponse)
def wms_item_edit_form(request: Request, item_id: int, db: Session = Depends(get_db)):
    """编辑货品（2026-10-08）：跟「手动建档」共用同一张模板，字段一模一样。

    能改：货号 / 产品名 / 品牌 / 分类·品类 / 型号 / 规格 / 序列号 / 成本 / 基准价 /
    检修后条码 / 商品字段。**不能改条码**（终身凭据），也不改等级与状态（那是检测与作业的事）。
    """
    item = _item_or_404(db, item_id)
    batch = db.get(WmsBatch, item.batch_id) if item.batch_id else None
    return render(
        "wms_item_form.html",
        request,
        heading=f"编辑货品 {item.display_code}",
        sub=f"{item.title} · 条码 {item.barcode} 终身不变",
        crumb_path="/wms/items",
        crumb_leaf=item.display_code,
        mode="edit",
        batch=batch,
        item=item,
        form_action=f"/wms/items/{item.id}/edit",
        values=_values_of(item),
        extra_fields=_extra_fields(db, only_enabled=False),
        extra_values=dict(item.extra or {}),
        category_choices=category_options(),
    )


@router.post("/wms/items/{item_id}/edit")
async def wms_item_edit(request: Request, item_id: int, db: Session = Depends(get_db)):
    """保存货品的商品字段（建档 / 导入进来的都能改）。条码不动，改了什么写审计。"""
    item = _item_or_404(db, item_id)
    back = f"/wms/items/{item.id}/edit"
    form = await request.form()
    raw_cat, category = _category_from_form(form)
    if raw_cat and not category:
        return _redirect(
            back,
            f"分类「{raw_cat}」认不出来 —— 写中文（如 破壁机 / 电饭煲）或三级码（如 HA.KC.DFB）",
            "warn",
        )
    fields = {
        "sku_code": str(form.get("sku_code") or "").strip(),
        "product_name": str(form.get("product_name") or "").strip(),
        "brand": str(form.get("brand") or "").strip(),
        # 型号 / 规格（2026-10-12 晚 用户要求把那块表单删了）：页面不再有这两格，
        # 而 update_item 的口径是「没给就当空」—— 这里回填库里的值，
        # 别把 Excel 导入进来的型号 / 规格一保存就清掉（多规格走规格矩阵那条路）。
        "model": str(form.get("model") or item.model or "").strip(),
        "spec": str(form.get("spec") or item.spec or "").strip(),
        "serial_no": str(form.get("serial_no") or "").strip(),
        # 品类是条码里写着的依据，清空表单不等于清掉品类 —— 空就保持原值
        "category": category or str(item.category or ""),
        # 价格（2026-10-12）：成本价 / 渔货价 / 建议售价 / 市场价（基准价）
        "purchase_cost": _form_money(form.get("purchase_cost")),
        "selling_price": _form_money(form.get("selling_price")),
        "suggest_price": _form_money(form.get("suggest_price")),
        "base_price": _form_money(form.get("base_price")),
        # 数量（2026-10-10）：改数量会写差额流水，余额跟着变
        "qty": intake.norm_qty(form.get("qty"), hi=200),
    }
    if not fields["sku_code"]:
        return _redirect(back, "货号必填 —— 扫码检测认货靠它", "warn")
    operator = _user(request)
    extra_fields = _extra_fields(db, only_enabled=False)
    try:
        changed = intake.update_item(
            db, item, fields=fields,
            extra=_extra_from_form(form, extra_fields),
            operator=operator,
            labels={f.key: (f.label or f.key) for f in extra_fields},
        )
        # 检修后条码（2026-10-12 晚）：建档 / 编辑页的那一格撤了，改在「货品详情」页贴
        # （/wms/items/{id}/own-barcode 有登记 + 按规则生成）。表单**不带**这一项时别动
        # 库里的值 —— 以前「表单没有」会被当成「清空」，一保存就把已贴的码抹掉。
        if "own_barcode" in form:
            own = str(form.get("own_barcode") or "").strip()
            if own != str(item.own_barcode or "").strip():
                intake.set_own_barcode(db, item, own, operator=operator)
                changed.append("检修后条码已更新")
    except ValueError as exc:
        return _redirect(back, str(exc), "warn")
    if not changed:
        return _redirect(f"/wms/items/{item.id}", "没有改动")
    head = "；".join(changed[:3])
    more = f"（还有 {len(changed) - 3} 处）" if len(changed) > 3 else ""
    return _redirect(f"/wms/items/{item.id}", f"已保存：{head}{more}")


@router.post("/wms/items/{item_id}/delete")
async def wms_item_delete(request: Request, item_id: int, db: Session = Depends(get_db)):
    """删货品（错录清理）：检测过 / 上过出库单 / 报修过 / 进过盘点单的会拦下来（改走核销、报废）。"""
    item = _item_or_404(db, item_id)
    batch_id = item.batch_id
    try:
        snap = intake.delete_item(db, item, operator=_user(request))
    except ValueError as exc:
        return _redirect(f"/wms/items/{item_id}", str(exc), "warn")
    return _redirect(
        f"/wms/batches/{batch_id}" if batch_id else "/wms/items",
        f"已删除货品 {snap['sku_code'] or snap['barcode']}（{snap['display_code']}）"
        f"—— 它的库存流水一并清掉，余额已按剩余流水重算",
    )


@router.post("/wms/items/{item_id}/reset-inbound")
async def wms_item_reset_inbound(request: Request, item_id: int, db: Session = Depends(get_db)):
    """把货品退回「刚入库、还没检测」的原始数据状态（误检 / 练手 / 录错的纠偏口子）。

    等级与渔货价清空、回待检仓；检测记录一条不删（历史留痕，复检接着算下一轮）。
    """
    item = _item_or_404(db, item_id)
    form = await request.form()
    code = item.display_code or item.barcode
    try:
        inspection.reset_to_inbound(
            db, item,
            operator=_user(request),
            reason=str(form.get("reason") or "").strip(),
        )
    except ValueError as exc:
        return _redirect(f"/wms/items/{item_id}", str(exc), "warn")
    return _redirect(
        f"/wms/items/{item_id}",
        f"{code} 已退回入库原始状态：待检、未定级（渔货价清空，检测记录仍留痕）",
    )


@router.post("/wms/items/{item_id}/move")
async def wms_item_move(request: Request, item_id: int, db: Session = Depends(get_db)):
    """移库 / 上架 / 锁货 / 释放 / 转拆机件 —— 全部写流水，不改数量。"""
    item = _item_or_404(db, item_id)
    form = await request.form()
    action = str(form.get("action") or "").strip()
    operator = _user(request)
    recall_rows: list[tuple[str, int]] = []      # 召回要同步回平台的库存（提交之后再推）
    try:
        if action == "move":
            warehouse = str(form.get("warehouse_code") or "").upper()
            # 库位必须是「仓库与货架」维护表里启用的（页面是下拉，这里是后端兜底；
            # 货品原来挂着的那个值原样放行，别让历史手填值把移库卡住）
            location = warehouse_service.check_location(
                db, form.get("location_code"), current=item.location_code
            )
            if not warehouse:
                raise ValueError("请选择目标仓")
            # 报修（C）/ 报废（D）的货不许进可售仓（2026-10-13 用户拍板）
            if warehouse == WH_SALE:
                ok, why = state.can_enter_sale(item.grade)
                if not ok:
                    raise ValueError(why)
            item.location_code = location
            ledger.apply(db, item, biz_type="MOVE", biz_id="", to_warehouse=warehouse,
                         operator=operator, remark=str(form.get("remark") or "移库"))
        elif action == "status":
            target = str(form.get("to_status") or "").upper()
            warehouse, default_status = state.route_of(item.grade)
            # 同上：报修 / 报废的货不能置成「可售」，也不能顺带搬进可售仓
            if target == STATUS_SELLABLE or str(
                form.get("warehouse_code") or warehouse
            ).upper() == WH_SALE:
                ok, why = state.can_enter_sale(item.grade)
                if not ok:
                    raise ValueError(why)
            was_sold = str(item.status or "").upper() == STATUS_SOLD
            qty = int(item.qty or 0) or 1
            ledger.apply(db, item, biz_type="UP", biz_id="",
                         to_warehouse=str(form.get("warehouse_code") or warehouse).upper() or None,
                         to_status=target, operator=operator,
                         remark=str(form.get("remark") or state.action_label(item.status, target)))
            # 买家退货召回：货又回到手上了 —— 渔枢商品库存与渔货栈平台库存加回去（2026-10-10）
            if was_sold and target != STATUS_SOLD:
                recall_rows.append((str(item.barcode or ""), qty))
        elif action == "parts":
            parts_service.to_parts(db, item, operator=operator,
                                   remark=str(form.get("remark") or "判废转拆机件"))
        else:
            raise ValueError("未知动作")
    except ValueError as exc:
        return _redirect(f"/wms/items/{item_id}", str(exc), "warn")
    db.commit()
    # 平台的库存同步放在渔集自己的事务提交之后：别握着渔集的写事务去写平台库
    # （同一个 SQLite 文件时会把库锁死；Postgres 上也更干净），同步失败不影响这次流转
    note = ""
    if recall_rows:
        note = wms_bridge.hub_sync_note(wms_bridge.push_stock(recall_rows))
    return _redirect(f"/wms/items/{item_id}",
                     "已更新（库存流水已记录）" + (f"；{note}" if note else ""))


@router.post("/wms/items/{item_id}/price")
async def wms_item_price(request: Request, item_id: int, db: Session = Depends(get_db)):
    """改价：只许往上调 —— 低于「低于基准价此比例需审批」那条线的价**不让存**。"""
    item = _item_or_404(db, item_id)
    form = await request.form()
    price = _form_money(form.get("selling_price"))
    base = _form_money(form.get("base_price")) or float(item.base_price or 0.0)
    cost = _form_money(form.get("purchase_cost")) or float(item.purchase_cost or 0.0)
    check = grading.price_of(db, item.grade, base, cost, manual_price=price or None,
                             manual_suggest=float(item.suggest_price or 0.0) or None)
    if check.get("manual_below_floor"):
        return _redirect(
            f"/wms/items/{item_id}",
            f"改价只能往上调：¥{float(price or 0):.2f} 低于审批线 "
            f"¥{float(check.get('floor') or 0):.2f}（{grade_label(item.grade)} 档「低于基准价 "
            f"{float(check.get('approve_below') or 0) * 100:.0f}% 需审批」）—— "
            f"要更低就先找主管审批",
            "warn",
        )
    item.base_price = base
    item.purchase_cost = cost
    item.selling_price = float(check.get("price") or 0.0)
    # 改价框现在总是带着数（渔货价自动带出来的），跟「按系数算出来的」一样就不算人工改价
    auto_price = round(base * float(check.get("factor") or 1.0), 2)
    manual = price is not None and price > 0 and abs(price - auto_price) > 0.005
    wms_audit.log(db, "item", item.id, "price",
                  after=f"¥{item.selling_price:.2f}", operator=_user(request),
                  note=("人工定价" if manual else "按等级系数自动算")
                  + ("；低于底价需主管审批" if check.get("need_approval") else ""))
    db.commit()
    msg = f"渔货价已更新为 ¥{item.selling_price:.2f}"
    # 渔货价不能高于建议售价（2026-10-10）：压下来了就得说清楚，别让人以为填的值存进去了
    if check.get("clamped_by_suggest"):
        msg += (f"（你填的 ¥{float(price or 0):.2f} 高于建议售价 "
                 f"¥{float(check.get('suggest_price') or 0):.2f}，已压到建议售价 —— "
                 f"要调高就去「分级规则 → 价格系数」把建议售价系数抬上去）")
    if check.get("need_approval"):
        msg += f"（低于底价 ¥{check.get('floor'):.2f}，需主管审批）"
    if check.get("low_margin"):
        msg += "（毛利率低于下限，已记录）"
    return _redirect(f"/wms/items/{item_id}", msg, "warn" if check.get("need_approval") else "")


@router.post("/wms/items/{item_id}/own-barcode")
async def wms_item_own_barcode(request: Request, item_id: int, db: Session = Depends(get_db)):
    """检修后条码（2026-10-08）：检修规整完**自己重新贴的码**。

    现场多半是拿现成的码贴上去再扫进来（手填 / 扫码录入）；没有码可贴时按规则生成一条
    （WMS + 年月 + 4 位流水）。一码一台，贴过的不许再贴到第二台；留空 = 清除（贴错了）。
    """
    item = _item_or_404(db, item_id)
    form = await request.form()
    action = str(form.get("action") or "save").strip()
    code = str(form.get("own_barcode") or "").strip()
    if action == "gen":
        code = intake.next_own_barcode(db)
    try:
        intake.set_own_barcode(db, item, code, operator=_user(request))
    except ValueError as exc:
        return _redirect(f"/wms/items/{item_id}", str(exc), "warn")
    if code:
        return _redirect(f"/wms/items/{item_id}", f"检修后条码已登记：{code}")
    return _redirect(f"/wms/items/{item_id}", "已清除检修后条码")


# ---------------------------------------------------------------- 扫码检测工作台
@router.get("/wms/inspect", response_class=HTMLResponse)
def wms_inspect(request: Request, code: str = "", db: Session = Depends(get_db)):
    item = intake.find_item(db, code) if code else None
    return render(
        "wms_inspect.html",
        request,
        heading="扫码检测工作台",
        sub="扫 W 占位码 → 自动载入档案 → 单键热键录入 → Enter 提交自动跳下一台",
        item=item,
        code=code,
        pending=inspection.pending_pool(db, limit=30),
        recheck=inspection.recheck_pool(db, limit=30),
        results=[[r, TEST_RESULT_LABELS[r]] for r in TEST_RESULT_CHOICES],
        grades=[[g, GRADE_LABELS[g]] for g in GRADE_CHOICES if g != "W"],
        # 货架位置也是下拉：库位从「仓库与货架」页维护，现场只许选
        locations=warehouse_service.location_options(db),
        # 短码第 1 位「第几个仓」的对照表：库位字段旁边那个「?」要说清 1~7 各是哪个仓
        wh_roots=warehouse_service.root_index(db),
        # 标签预览的那套样式（检测台里直接把预览画出来，不用跳去预览页）
        label_preview_css=label_preview.preview_css(),
    )


@router.get("/wms/api/item.json")
def wms_api_item(code: str = "", db: Session = Depends(get_db)):
    """扫码即载入：返回货品档案 + 检测项模板（按品类动态渲染）。"""
    item = intake.find_item(db, code)
    if not item:
        return _fail(
            f"扫的「{code or '（空）'}」对不上任何货品（系统条码 / 货号 / 序列号都查过）"
            f"—— 先去「批次与建档」建档", 404,
        )
    # 同一个货号可能有好几台（表格里数量 > 1）：把同号的另外几台一起给检测员挑
    twins = intake.find_items_by_sku(db, item.sku_code) if item.sku_code else []
    twins = [t for t in twins if t.id != item.id]
    # 「不影响使用」的降级项（面板 / 显示、非关键配件）打上 minor 标记：检测台按它
    # 给检测员一句「判 × 也只是降价值，照常卖（B 档）」，免得看到红叉就以为要报废
    template = [
        {"key": key, "label": label,
         "minor": key in inspection.MINOR_ITEM_KEYS,
         "cosmetic": key in inspection.COSMETIC_KEYS,
         "hint": inspection.ITEM_HINTS.get(key, "")}
        for key, label in inspection.full_template(item.category)
    ]
    batch = db.get(WmsBatch, item.batch_id) if item.batch_id else None
    # 历史检测记录：扫到检测过的货时页面要拿它问一句「是不是要重新提交」
    records = _records_of(db, item.id)
    # 沉淀字段（extra）按「商品字段」页维护的顺序 + **显示名**给出来（2026-10-08）：
    # 检测台是给人看的，屏幕上该出现「下单时间」这种名字，顺序也该跟管理页一致 ——
    # 以前直接拿 dict 的 key 铺出来，改过名的字段显示的是内部标识，顺序也乱。
    extra = dict(item.extra or {})
    extra_rows: list[dict] = []
    listed: set[str] = set()
    for f in sorted(importer.list_fields(db, only_enabled=False),
                    key=lambda x: (int(x.sort or 0), int(x.id or 0))):
        value = str(extra.get(f.key) or "").strip()
        if not value:
            continue
        listed.add(f.key)
        extra_rows.append({"key": f.key, "label": f.label or f.key, "value": value,
                           "type": f.field_type or "text"})
    for key, value in extra.items():  # 没登记进「商品字段」的历史值也不能丢
        text = str(value or "").strip()
        if text and key not in listed:
            extra_rows.append({"key": key, "label": key, "value": text, "type": "text"})
    return _ok(
        {
            "ok": True,
            "item": {
                "id": item.id,
                "barcode": item.barcode,
                "display_code": item.display_code,
                "category": item.category,
                "category_label": category_label(item.category),
                "brand": item.brand,
                "product_name": item.product_name,
                "model": item.model,
                "spec": item.spec,
                "title": item.title,
                "serial_no": item.serial_no,
                "sku_code": item.sku_code,
                # 数量（2026-10-10）：这一条货有几台（同一个条码，整台流转）
                "qty": int(item.qty or 0) or 1,
                "own_barcode": item.own_barcode,
                "extra": dict(item.extra or {}),
                # 「② 商品入库信息」要的就是这一份入库档案：建档时录的都在这里
                "extra_rows": extra_rows,
                "batch_no": batch.batch_no if batch else "",
                "supplier_name": (batch.supplier_name if batch else "") or "",
                # 时间口径跟页面其它地方一致：库里 UTC，出去就转成展示时区（别让 JS 直接啃 ISO）
                "inbound_at": _fmt(item.created_at),
                "location_code": item.location_code,
                # 库位的展示名（表里维护的全路径），检测台那格显示它
                "location_label": location_label(item.location_code) or "",
                "twins": [
                    {"id": t.id, "barcode": t.barcode, "display_code": t.display_code,
                     "grade": t.grade, "status": t.status,
                     "qty": int(t.qty or 0) or 1, "sku_code": str(t.sku_code or "")}
                    for t in twins
                ],
                "grade": item.grade,
                "status": item.status,
                "status_label": status_label(item.status),
                "warehouse": warehouse_label(item.warehouse_code),
                "base_price": item.base_price,
                "purchase_cost": item.purchase_cost,
            },
            "template": template,
            "safety_keys": list(inspection.SAFETY_ITEM_KEYS),
            "records": records,
            # 已经被检测过的货再扫一次：页面要先问一句「是不是要重新提交、覆盖上一轮」
            "tested": bool(records),
            "last_record": (records[-1] if records else None),
        }
    )


def _records_of(db: Session, item_id) -> list[dict]:
    """这台货的历史检测记录（最近一次排最后）。

    ``tested_at`` 是 datetime，直接塞进 JSONResponse 会 TypeError —— 扫「已经检测过」
    的货（复检）时 100% 触发，所以时间一律先格式化成文本。
    """
    return [
        {"round": r.round, "final": r.final_grade, "tester": r.tester,
         "reasons": r.reasons, "at": _fmt(r.tested_at)}
        for r in inspection.records_of(db, item_id)
    ]


@router.post("/wms/api/inspect/preview")
async def wms_api_preview(request: Request, db: Session = Depends(get_db)):
    """实时建议档位：每勾一项就刷新（不落库）。"""
    payload = await request.json()
    item = db.get(WmsItem, _form_int(payload.get("item_id")))
    if not item:
        return _fail("货品不存在", 404)
    out = inspection.preview(db, item, payload)
    return _ok({"ok": True, **out})


@router.post("/wms/api/inspect/submit")
async def wms_api_submit(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    item = db.get(WmsItem, _form_int(payload.get("item_id")))
    if not item:
        return _fail("货品不存在", 404)
    # 库位先校验再检测：脏库位不该把检测记录也写进去（页面是下拉，这里是后端兜底；
    # 货品原来挂着的那个值原样放行，复检老货不用先去补库位）
    try:
        location = warehouse_service.check_location(
            db, payload.get("location_code"), current=item.location_code
        )
    except ValueError as exc:
        return _fail(str(exc))
    try:
        record = inspection.submit(
            db, item,
            tester=_user(request) or str(payload.get("tester") or ""),
            results=payload.get("results") or {},
            fail_descs=payload.get("fail_descs") or {},
            battery_health=payload.get("battery_health"),
            flaw_desc=str(payload.get("flaw_desc") or ""),
            photos=list(payload.get("photos") or []),
            final_grade=str(payload.get("final_grade") or "").upper(),
            override_reason=str(payload.get("override_reason") or ""),
            uncertain=bool(payload.get("uncertain")),
            inhouse_fixable=bool(payload.get("inhouse_fixable")),
            must_official=bool(payload.get("must_official")),
            missing_parts=bool(payload.get("missing_parts")),
            # 全新（2026-10-10）：原封未拆 / 拆封未使用 —— N 档的两个入口
            sealed=bool(payload.get("sealed")),
            opened_unused=bool(payload.get("opened_unused")),
            note=str(payload.get("note") or ""),
            # 检测台的渔货价：留空 = 用系统按分级规则算的默认价（2026-10-08）
            manual_price=_form_money(payload.get("manual_price")) or None,
        )
    except ValueError as exc:
        return _fail(str(exc))
    # 检完顺手把货架位置写上：现场「检完上架」是一个动作，不该让人再跑一趟台账页。
    # 位置不是库存变动（流水只记仓与状态），所以只留审计痕。
    if location and location != str(item.location_code or ""):
        item.location_code = location
        wms_audit.log(db, "item", item.id, "location", after=location,
                      operator=_user(request), note="检测台登记货架位置")
        db.commit()
    # 检修规整完自己重新贴的码：检测台一起扫进来登记（一码一台，冲突只提示，不让检测白做）
    own = str(payload.get("own_barcode") or "").strip()
    own_note = ""
    if own and own != str(item.own_barcode or ""):
        try:
            intake.set_own_barcode(db, item, own, operator=_user(request))
        except ValueError as exc:
            own_note = f"（检修后条码没登记：{exc}）"
    # 检测台打过标签的：提交后顺手把贴标复核做了（2026-10-10）。
    # 现场就是「打完就贴」，不该让人再拿着货跑去标签页扫一遍 —— 标签页直接显示已打印 + 已贴标。
    # 但纸上的档与最终档对不上时绝不自动放行：那张标签是错的，必须补打。
    label_note = ""
    printed = labeling.last_print_of(db, item.id)
    if printed and str(item.status or "").upper() == STATUS_GRADED:
        if str(printed.grade or "").upper() != str(item.grade or "").upper():
            label_note = (f" · 标签上印的是 {printed.grade} 档，跟最终档 {item.grade} 对不上"
                          f" —— 去「标签打印与贴标复核」补打一张再贴")
        else:
            res = labeling.verify(db, item.barcode, item.grade, operator=_user(request))
            # B / C / D 也过复核，但下一步是维修 / 报修 / 拆机件 —— 话要说准，别都写「已入可售仓」
            label_note = (f" · 标签已打印，{res.get('message') or '贴标复核通过'}" if res.get("ok")
                          else f" · 贴标复核没过：{res.get('message')}")
    return _ok(
        {
            "ok": True,
            "location": item.location_code,
            "own_barcode": item.own_barcode,
            "message": ("存疑已进复检池" if record.uncertain
                        else f"已定级 {record.final_grade}（第 {record.round} 次检测）")
                       + own_note + label_note,
            "label": {"printed": bool(printed), "status": printed.status if printed else "",
                      "verify": label_note},
            "suggest": record.suggest_grade,
            "final": record.final_grade,
            "reasons": record.reasons,
            "safety": record.safety_hit,
            "grade_label": grade_label(record.final_grade or record.suggest_grade),
            "item_url": f"/wms/items/{item.id}",
        }
    )


# ---------------------------------------------------------------- 检测台的即时打标签
def _label_preview_of(db: Session, item: WmsItem, grade: str = "") -> dict:
    """这台货的标签打出来长什么样（不落库、不打印，纯预览）。

    拿一个没进 session 的 WmsLabelPrint 喂给渲染层 —— 预览不该在队列里留痕。
    """
    data = labeling.label_data(item)
    ghost = WmsLabelPrint(
        item_id=item.id,
        barcode=item.barcode,
        display_code=data["display_code"],
        # 预览按数量算份数（3 台就预览「共 3 张」），跟真打时一个口径
        copies=labeling.label_copies_of(item),
        label_type=labeling.LABEL_FORMAL,
        grade=str(grade or data["grade"]).upper(),
        action_text=data["action"],
        dialect=labeling.TSPL,
        command=labeling.build_command(data, labeling.TSPL),
        transport=labeling.TRANSPORT_CLOUD,
        batch_no=str(item.batch_no or ""),
    )
    return label_preview.render(print_settings.render_for(db, ghost, None), zoom=2.0)


@router.get("/wms/api/label/preview")
def wms_api_label_preview(item_id: int = 0, grade: str = "", db: Session = Depends(get_db)):
    """检测台的标签预览：还没打，先看一眼纸上长什么样。"""
    item = db.get(WmsItem, _form_int(item_id))
    if not item:
        return _fail("货品不存在", 404)
    return _ok({"ok": True, **_label_preview_of(db, item, grade)})


@router.post("/wms/api/label/print")
async def wms_api_label_print(request: Request, db: Session = Depends(get_db)):
    """检测台即时打标签（2026-10-10）：入队 → 下发云打印机 → 立刻回查。

    现场是「检完就打、打完就贴」，所以这里打过的：
      · 打印页那条记录会显示已打印（云端回查到就置 printed）；
      · 提交检测时自动把贴标复核做了 —— 不用再跑去标签页扫一遍。
    还没提交检测时等级取**引擎建议档**，提交后若最终档与标签对不上会提示补打。
    """
    payload = await request.json()
    item = db.get(WmsItem, _form_int(payload.get("item_id")))
    if not item:
        return _fail("货品不存在", 404)
    # 检修后条码顺手登记：跟提交时同一套校验（一码一台，冲突只提示）
    own = str(payload.get("own_barcode") or "").strip()
    own_note = ""
    if own and own != str(item.own_barcode or ""):
        try:
            intake.set_own_barcode(db, item, own, operator=_user(request))
        except ValueError as exc:
            own_note = f"（检修后条码没登记：{exc}）"
    grade = str(payload.get("grade") or "").strip().upper() or str(item.grade or "W").upper()
    # 打过一张的再点就是补打 —— 补打必须留原因（防补打变常态）
    label_type = labeling.LABEL_REPRINT if labeling.last_print_of(db, item.id) else labeling.LABEL_FORMAL
    try:
        rows = labeling.enqueue(
            db, [item],
            operator=_user(request),
            label_type=label_type,
            reason="检测台补打（同一台再打一张）" if label_type == labeling.LABEL_REPRINT else "",
            dialect=labeling.TSPL,
            transport=labeling.TRANSPORT_CLOUD,
            # 还没提交检测时按引擎建议档出（2026-10-13 用户三催：纸上的等级
            # 必须跟引擎建议 / 实际评级一致，不能永远印「W 待测」）
            grade=grade,
        )
    except ValueError as exc:
        return _fail(str(exc))
    row = rows[0] if rows else None
    if row is None:
        return _fail("没排进打印队列")
    # 印在纸上的等级：没提交检测时按建议档印，提交后会拿最终档跟它比对
    row.grade = grade
    row.command = labeling.build_command(labeling.label_data(item), row.dialect)
    db.commit()
    ok, msg = print_settings.send(db, row)
    if ok:
        print_settings.refresh(db, row)      # 回查一次：打出来了就置「已打印」
    return _ok(
        {
            "ok": ok,
            "message": msg + own_note,
            "status": row.status,
            "status_label": labeling.PRINT_STATUS_LABELS.get(row.status, row.status),
            "print_id": row.id,
            "grade": row.grade,
            "barcode": row.barcode,
            **_label_preview_of(db, item, row.grade),
        }
    )


@router.get("/wms/api/label/check-code")
def wms_api_label_check_code(
    item_id: int = 0, code: str = "", db: Session = Depends(get_db)
):
    """扫「检修后条码」时的防错校验（2026-10-10）。

    现场动线：打标签 → 贴到机器上 → 扫枪再扫一次标签上的码 → 自动填进「检修后条码」。
    扫进来的码必须跟**刚打出来那张**对得上，三种结果：
      · 一样 → 直接填，不打扰；
      · 是库里另一台货的码 → 贴错标了，**只给「重新扫」**（一码一台，填了也存不进去）；
      · 库里没有 → 可能是他自己的内部码，弹出来让他确认（确认才填）。
    """
    item = db.get(WmsItem, _form_int(item_id))
    if not item:
        return _fail("货品不存在", 404)
    scanned = str(code or "").strip()
    if not scanned:
        return _fail("没扫到码")
    printed = labeling.last_print_of(db, item.id)
    label_code = str((printed.barcode if printed else "") or item.barcode or "")
    if scanned == label_code:
        return _ok(
            {
                "ok": True,
                "same": True,
                "code": scanned,
                "label_barcode": label_code,
                "other_item": None,
                "message": f"就是这个码（跟刚打出来的那张一致：{label_code}）",
            }
        )
    other = intake.find_item(db, scanned)
    if other and other.id != item.id:
        return _ok(
            {
                "ok": True,
                "same": False,
                "code": scanned,
                "label_barcode": label_code,
                "other_item": {
                    "id": other.id,
                    "barcode": other.barcode,
                    "display_code": other.display_code,
                    "grade": other.grade,
                    "status_label": status_label(other.status),
                },
                "message": f"这是另一台货（{other.display_code}）的码 —— 贴错标了，别用它",
            }
        )
    return _ok(
        {
            "ok": True,
            "same": False,
            "code": scanned,
            "label_barcode": label_code,
            "other_item": None,
            "message": f"扫进来的「{scanned}」跟标签上印的「{label_code}」不一样"
            f" —— 是这台自己的内部码吗？",
        }
    )


@router.post("/wms/api/upload")
async def wms_api_upload(request: Request):
    """拍照留档：检测照片存 media/wms/，返回可直接进 photos 的地址。"""
    from pathlib import Path
    from uuid import uuid4

    from ..config import config as _cfg

    form = await request.form()
    upload = form.get("file")
    name = getattr(upload, "filename", "") or ""
    data = await upload.read() if hasattr(upload, "read") else b""
    if not name or not data:
        return _fail("没有收到图片", 400)
    suffix = Path(name).suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return _fail("只支持 png / jpg / webp / gif", 400)
    folder = Path(_cfg.media_path) / "wms"
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4().hex[:16]}{suffix}"
    try:
        (folder / filename).write_bytes(data)
    except OSError as exc:  # noqa: BLE001 写盘失败不能让页面 500
        return _fail(f"保存失败：{exc}", 500)
    return _ok({"ok": True, "url": f"/media/wms/{filename}"})


# ---------------------------------------------------------------- 标签打印
@router.get("/wms/labels", response_class=HTMLResponse)
def wms_labels(request: Request, batch_no: str = "", db: Session = Depends(get_db)):
    rows = labeling.queue(db, limit=100, only_queued=False)
    if batch_no:
        rows = [r for r in rows if r.batch_no == batch_no]
    graded = intake.list_items(db, status="GRADED", limit=200)
    # 「已贴标」不在打印记录上 —— 贴没贴看货品：贴标复核通过（已入可售仓）才算贴上了。
    # 检测台打过标签并提交检测的，这里直接显示已打印 + 已贴标（2026-10-10）
    labeled: dict[int, dict] = {}
    for p in rows:
        it = db.get(WmsItem, p.item_id) if p.item_id else None
        labeled[p.id] = {
            "status": str(it.status or ""),
            "grade": str(it.grade or ""),
            "own_barcode": str(it.own_barcode or ""),
        } if it else None
    return render(
        "wms_labels.html",
        request,
        heading="标签打印与贴标复核",
        sub="打印走队列（幂等 + 可追溯），贴标后扫码复核：等级不符直接阻断入库",
        prints=rows,
        graded=graded,
        labeled=labeled,
        status_labels=labeling.PRINT_STATUS_LABELS,
        rate=labeling.reprint_rate(db, batch_no=batch_no),
        label_types=[[t, LABEL_TYPE_LABELS[t]] for t in LABEL_TYPES],
        dialects=[["TSPL", "TSPL（芯烨/佳博/汉印/TSC）"], ["ZPL", "ZPL（斑马系）"]],
        transports=[["CLOUD", "云打印 · 芯烨条码云（XP-420B，云端部署走这条）"],
                    ["NET9100", "链路A · 网口 RAW 9100"],
                    ["WEBSERIAL", "链路B · Web Serial（仅 Chromium）"],
                    ["CUPS", "链路D · CUPS（只打 A4 检测单）"]],
        batch_no=batch_no,
    )


@router.post("/wms/labels/enqueue")
async def wms_label_enqueue(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(v) for v in form.getlist("item_ids") if str(v).strip().isdigit()]
    if not ids:
        return _redirect("/wms/labels", "请先勾选要打印的货品", "warn")
    items = [db.get(WmsItem, i) for i in ids]
    items = [i for i in items if i]
    try:
        created = labeling.enqueue(
            db, items,
            operator=_user(request),
            label_type=str(form.get("label_type") or "formal"),
            reason=str(form.get("reason") or ""),
            printer=str(form.get("printer") or ""),
            dialect=str(form.get("dialect") or "TSPL"),
            transport=str(form.get("transport") or "NET9100"),
        )
    except ValueError as exc:
        return _redirect("/wms/labels", str(exc), "warn")
    # 开了「入队即下发」就直接推给云打印机（失败也留痕，队列里还能手动重发）
    cfg = print_settings.config(db, mask=False)
    pushed, last_err = 0, ""
    if cfg.get("auto_push") == "1" and print_settings.ready(cfg):
        for row in created:
            ok, msg = print_settings.send(db, row)
            if ok:
                pushed += 1
            else:
                last_err = msg
    if pushed:
        return _redirect(
            "/wms/labels",
            f"已排入队列 {len(created)} 张，其中 {pushed} 张已下发到云打印机",
        )
    if last_err:
        return _redirect("/wms/labels",
                         f"已排入队列 {len(created)} 张（下发失败：{last_err}）", "warn")
    return _redirect("/wms/labels", f"已排入打印队列 {len(created)} 张")


@router.post("/wms/labels/emergency-stop")
def wms_label_emergency_stop(request: Request, db: Session = Depends(get_db)):
    """强制停止打印（2026-10-12 加）：一次动作把还在路上的标签全掐掉。

    打印失控时用 —— 连着吐纸、份数打错、误点了「全部下发」：
    清云端队列 + 关掉「入队即下发」+ 本地待打印全部作废，三件一起做，
    少一件就停不干净（2026-10-12 那次 20 台货打出几百张，现场只能拔电源）。
    """
    _ok, msg = print_settings.emergency_stop(db)
    try:
        wms_audit.log(db, "label_print", 0, "emergency_stop",
                      operator=_user(request), after=msg)
    except Exception:
        pass
    return _redirect("/wms/labels", msg)


@router.post("/wms/labels/{print_id}/printed")
def wms_label_printed(print_id: int, db: Session = Depends(get_db)):
    try:
        labeling.mark_printed(db, print_id)
    except ValueError as exc:
        return _redirect("/wms/labels", str(exc), "warn")
    return _redirect("/wms/labels", "已标记打印完成")


@router.get("/wms/labels/{print_id}/command", response_class=PlainTextResponse)
def wms_label_command(print_id: int, db: Session = Depends(get_db)):
    """打印指令预览：云打印给的是 XML（照芯烨云的 <PAGE>/<LABEL> 格式），
    本地链路给的是 TSPL / ZPL（复制出来可用 `nc <打印机IP> 9100` 直接试打）。"""
    row = db.get(WmsLabelPrint, print_id)
    if not row:
        raise HTTPException(404, "打印任务不存在")
    if str(row.transport or "").upper() == labeling.TRANSPORT_CLOUD:
        return PlainTextResponse(print_settings.render_for(db, row, None))
    # 本地链路（TSPL / ZPL）：库里那份 command 是入队时的快照，**等级可能已经变了** ——
    # 按货品当前事实重算一份再给（顺手写回库，两边一致），别打出过时的等级（2026-10-13）
    item = db.get(WmsItem, row.item_id) if row.item_id else None
    if item is not None:
        row.command = labeling.build_command(labeling.label_data(item), row.dialect)
        row.grade = str(item.grade or "W").upper()
        db.commit()
    return PlainTextResponse(row.command or "")


@router.get("/wms/labels/{print_id}/preview", response_class=HTMLResponse)
def wms_label_preview_page(
    print_id: int, request: Request, db: Session = Depends(get_db)
):
    """标签预览图（2026-10-08）：这张标打出来长什么样，先看一眼再打。

    改模板、换纸张、调版式都靠它 —— 不用「打一张废纸再回来改」。
    """
    row = db.get(WmsLabelPrint, print_id)
    if not row:
        raise HTTPException(404, "打印任务不存在")
    item = db.get(WmsItem, row.item_id) if row.item_id else None
    xml = print_settings.render_for(db, row, None)
    printer = print_settings.default_printer(db)
    if row.printer_sn and (printer is None or printer.sn != row.printer_sn):
        printer = next(
            (p for p in print_settings.printers(db) if p.sn == row.printer_sn), printer
        )
    return render(
        "wms_label_preview.html",
        request,
        heading="标签预览",
        sub="按毫米坐标画出来的样子 —— 预览与真打出来的一一对应（条码是真 Code128）",
        row=row,
        item=item,
        xml=xml,
        preview=label_preview.render(xml, zoom=2.4),
        preview_css=label_preview.preview_css(),
        printer=printer,
        is_cloud=str(row.transport or "").upper() == labeling.TRANSPORT_CLOUD,
        grade_text=grade_label(row.grade or "W"),
        status_text=labeling.PRINT_STATUS_LABELS.get(
            str(row.status or ""), str(row.status or "")
        ),
    )


@router.post("/wms/labels/{print_id}/push")
def wms_label_push(print_id: int, db: Session = Depends(get_db)):
    """把这一张推给云打印机（没开自动下发时手动点）。"""
    row = db.get(WmsLabelPrint, print_id)
    if not row:
        return _redirect("/wms/labels", "打印任务不存在", "warn")
    ok, msg = print_settings.send(db, row)
    return _redirect("/wms/labels", msg, "" if ok else "warn")


@router.post("/wms/labels/{print_id}/sync")
def wms_label_sync(print_id: int, db: Session = Depends(get_db)):
    """回查云端：这张到底打出来没有。"""
    row = db.get(WmsLabelPrint, print_id)
    if not row:
        return _redirect("/wms/labels", "打印任务不存在", "warn")
    ok, msg = print_settings.refresh(db, row)
    return _redirect("/wms/labels", msg, "" if ok else "warn")


@router.post("/wms/labels/push-all")
def wms_labels_push_all(db: Session = Depends(get_db)):
    """把队列里还没下发的一起推给云打印机（批量重发）。"""
    rows = [r for r in labeling.queue(db, limit=200) if not r.order_no]
    if not rows:
        return _redirect("/wms/labels", "队列里没有待下发的任务", "warn")
    ok_n, last_err = 0, ""
    for row in rows:
        ok, msg = print_settings.send(db, row)
        if ok:
            ok_n += 1
        else:
            last_err = msg
    if ok_n:
        tail = f"（{len(rows) - ok_n} 张失败：{last_err}）" if ok_n < len(rows) else ""
        return _redirect("/wms/labels", f"已下发 {ok_n} 张到云打印机{tail}",
                         "" if ok_n == len(rows) else "warn")
    return _redirect("/wms/labels", f"一张都没发出去：{last_err}", "warn")


@router.post("/wms/labels/verify")
async def wms_label_verify(request: Request, db: Session = Depends(get_db)):
    """贴标复核：等级不符 100% 阻断入库。"""
    form = await request.form()
    result = labeling.verify(
        db,
        str(form.get("barcode") or ""),
        str(form.get("grade") or ""),
        operator=_user(request),
    )
    kind = "" if result.get("ok") else "warn"
    return _redirect("/wms/labels", result.get("message") or "", kind)


# ---------------------------------------------------------------- 打印设置（芯烨条码云）
# 云端部署时服务碰不到现场那台 XP-420B 的网口，所以打印走芯烨条码云平台中转：
# 这里管账号（开发者ID / UserKEY）、打印机档案（机身编号 + 纸张）与标签模板（XML），
# 全部在页面上改，改完立刻生效 —— 换机器、改版式都不用动代码重启。
@router.get("/wms/print-settings", response_class=HTMLResponse)
def wms_print_settings_page(request: Request, db: Session = Depends(get_db)):
    cfg = print_settings.config(db)
    default = print_settings.default_printer(db)
    sample = print_settings.sample_xml(db)
    # 回调地址：优先用「公网域名」设置项，没填就退回当前访问的地址（IP 直连时用）
    base = str(cfg.get("public_base_url") or "").strip() or str(request.base_url).rstrip("/")
    return render(
        "wms_print_settings.html",
        request,
        heading="打印设置",
        sub="芯烨条码云（XP-420B）：账号、打印机、标签模板都在这儿改，改完立刻生效",
        cfg=cfg,
        callback_url=cloud_callback.callback_url(base),
        events=cloud_callback.recent(db),
        state_label=cloud_callback.state_label,
        printers=print_settings.printers(db),
        default_sn=(default.sn if default else ""),
        template_vars=print_settings.TEMPLATE_VARS,
        # 模板体检：中文行用了英数字体（f=1~6）会印成拉丁乱码，页面上直接飘黄条
        template_warnings=print_settings.template_warnings(cfg.get("template") or ""),
        sample=sample,
        test_result=print_settings.last_test_of(db),
        # 标签预览图：把 XML 按毫米坐标画出来（改完模板一眼就知道会不会打歪）
        preview=label_preview.render(sample),
        preview_css=label_preview.preview_css(),
        ready=print_settings.ready(print_settings.config(db, mask=False)),
    )


@router.post("/wms/print-settings")
async def wms_print_settings_save(request: Request, db: Session = Depends(get_db)):
    """存账号与三个开关。

    坑（2026-10-09 修）：页面上「保存模板」「保存回调设置」两个表单以前也提交到这里，
    它们没有开关控件 → ``form.get("enabled")`` 为空 → 开关被当成 0 写回库，
    **用户勾好的「启用云打印」一保存模板就悄悄失效**。现在开关只在带了
    ``switches`` 标记的表单里改（该标记只有「账号与行为」这个表单有），
    模板 / 回调各自走自己的路由 —— 双保险。
    """
    form = await request.form()
    values = {
        "cloud_user": str(form.get("cloud_user") or ""),
        "cloud_key": str(form.get("cloud_key") or ""),
        "base_url": str(form.get("base_url") or ""),
        "timeout": str(form.get("timeout") or ""),
        "copies": str(form.get("copies") or ""),
    }
    if form.get("switches"):  # 勾了才改这个表单里的开关；没勾 = 关
        for key in ("enabled", "auto_push", "mode"):
            values[key] = "1" if form.get(key) else "0"
    print_settings.save_config(db, values)
    return _redirect("/wms/print-settings", "账号与行为已保存")


@router.post("/wms/print-settings/template")
async def wms_print_template_save(request: Request, db: Session = Depends(get_db)):
    """只存标签模板 —— 别碰开关（见上一条路由的注释）。"""
    form = await request.form()
    print_settings.save_config(db, {"template": str(form.get("template") or "")})
    return _redirect("/wms/print-settings", "标签模板已保存")


@router.post("/wms/print-settings/callback")
async def wms_print_callback_save(request: Request, db: Session = Depends(get_db)):
    """只存回调的公网域名与令牌 —— 同样不碰开关。"""
    form = await request.form()
    print_settings.save_config(
        db,
        {
            "public_base_url": str(form.get("public_base_url") or ""),
            "callback_token": str(form.get("callback_token") or ""),
        },
    )
    return _redirect("/wms/print-settings", "回调设置已保存")


@router.post("/wms/print-settings/test")
def wms_print_self_test(db: Session = Depends(get_db)):
    """一键自检：账号 → 云接口 → 机器在线 → 打一张测试页，结果存在页面上。"""
    result = print_settings.self_test(db)
    head = "自检通过" if result["ok"] else "自检没通过 —— 看下面的分步结果"
    return _redirect("/wms/print-settings", head, "" if result["ok"] else "warn")


@router.post("/wms/print-settings/printers")
async def wms_printer_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    gap_raw = str(form.get("gap_mm") or "").strip()
    try:
        _row, msg = print_settings.save_printer(
            db,
            printer_id=_form_int(form.get("printer_id")) or 0,
            sn=str(form.get("sn") or ""),
            name=str(form.get("name") or ""),
            kind=str(form.get("kind") or "label"),
            width_mm=_form_int(form.get("width_mm")) or 60,
            height_mm=_form_int(form.get("height_mm")) or 40,
            gap_mm=int(gap_raw) if gap_raw.isdigit() else 2,
            copies=_form_int(form.get("copies")) or 1,
            enabled=bool(form.get("enabled")),
            note=str(form.get("note") or ""),
            verify=bool(form.get("verify")),
            cfg=print_settings.config(db, mask=False),
        )
    except ValueError as exc:
        return _redirect("/wms/print-settings", str(exc), "warn")
    return _redirect("/wms/print-settings", msg)


@router.post("/wms/print-settings/printers/{printer_id}/default")
def wms_printer_default(printer_id: int, db: Session = Depends(get_db)):
    try:
        msg = print_settings.set_default(db, printer_id)
    except ValueError as exc:
        return _redirect("/wms/print-settings", str(exc), "warn")
    return _redirect("/wms/print-settings", msg)


@router.post("/wms/print-settings/printers/{printer_id}/toggle")
def wms_printer_toggle(printer_id: int, db: Session = Depends(get_db)):
    try:
        msg = print_settings.toggle(db, printer_id)
    except ValueError as exc:
        return _redirect("/wms/print-settings", str(exc), "warn")
    return _redirect("/wms/print-settings", msg)


@router.post("/wms/print-settings/printers/{printer_id}/delete")
def wms_printer_delete(printer_id: int, db: Session = Depends(get_db)):
    try:
        msg = print_settings.delete_printer(db, printer_id)
    except ValueError as exc:
        return _redirect("/wms/print-settings", str(exc), "warn")
    return _redirect("/wms/print-settings", msg)


@router.post("/wms/print-settings/printers/{printer_id}/test")
def wms_printer_test(printer_id: int, db: Session = Depends(get_db)):
    """测试打印：确认「账号 + 模板 + 这台机器」这条链路是通的。"""
    row = db.get(WmsPrinter, printer_id)
    if not row:
        return _redirect("/wms/print-settings", "打印机不存在", "warn")
    ok, msg = print_settings.test_print(db, row)
    return _redirect("/wms/print-settings", msg, "" if ok else "warn")


@router.post("/wms/print-settings/printers/{printer_id}/status")
def wms_printer_status(printer_id: int, db: Session = Depends(get_db)):
    """查这台机器在不在（顺带把型号与固件版本带回来）。"""
    row = db.get(WmsPrinter, printer_id)
    if not row:
        return _redirect("/wms/print-settings", "打印机不存在", "warn")
    ok, msg = print_settings.printer_status(db, row)
    return _redirect("/wms/print-settings", msg, "" if ok else "warn")


@router.post("/wms/print-settings/printers/{printer_id}/clear")
def wms_printer_clear(printer_id: int, db: Session = Depends(get_db)):
    """清空这台机器在云端的待打印队列（打错一堆时用）。"""
    row = db.get(WmsPrinter, printer_id)
    if not row:
        return _redirect("/wms/print-settings", "打印机不存在", "warn")
    ok, msg = print_settings.clear_queue(db, row)
    return _redirect("/wms/print-settings", msg, "" if ok else "warn")


# ---------------------------------------------------------------- 云打印回调（公网入站）
# 芯烨条码云平台的服务器推过来时**不会带我们的登录 Cookie**，所以这两条挂在
# public_router 上（没有 _enforce 守卫）；合并部署时 app/web/auth.py 的 PUBLIC_PATHS
# 里也放行了这个路径 —— 少开任何一道门，平台都会把回调地址判成无效。
async def _callback_payload(request: Request) -> dict[str, Any]:
    """回调报文可能是 JSON 也可能是表单，两种都接（字段还可能裹在 data 里）。"""
    raw = await request.body()
    if not raw:
        return {}
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001 不是 JSON 就当表单试
        pass
    try:
        return dict(parse_qsl(text, keep_blank_values=True))
    except Exception:  # noqa: BLE001 两种都不是：原样存下来，回头照报文补解析
        return {"raw": text[:2000]}


def _callback_allowed(request: Request, db: Session, payload: dict[str, Any]) -> bool:
    """设了回调令牌就得对上（没设 = 谁来都收 —— 快速落地时的默认值）。"""
    token = str(print_settings.config(db, mask=False).get("callback_token") or "").strip()
    if not token:
        return True
    given = request.query_params.get("token") or str(payload.get("token") or "")
    return given.strip() == token


@public_router.post("/wms/api/cloudprint/callback")
async def wms_cloudprint_callback(request: Request, db: Session = Depends(get_db)):
    """云打印回调入口：收下就落库，**无条件回 ``{"data": "ok"}``**。

    平台只认这个返回值（回别的、或超过 3 秒都算地址无效），所以这里连令牌没对上
    也回 ok —— 认不出的推送照样留痕，页面上能看见「平台确实推过来了」，
    比让平台把地址判死、后面一条都收不到强。
    """
    payload = await _callback_payload(request)
    if _callback_allowed(request, db, payload):
        cloud_callback.receive(
            db, payload, source_ip=(request.client.host if request.client else "")
        )
    return JSONResponse({"data": "ok"})


@public_router.get("/wms/api/cloudprint/callback")
def wms_cloudprint_callback_probe():
    """平台后台「测试回调地址」多半发的是 GET：也回 ok（不落库）。"""
    return JSONResponse({"data": "ok"})


# ---------------------------------------------------------------- 出库
@router.get("/wms/outbound", response_class=HTMLResponse)
def wms_outbound(request: Request, q: str = "", type: str = "", status: str = "",
                 db: Session = Depends(get_db)):
    rows = outbound_service.list_orders(db, q=q, type=type, status=status)
    return render(
        "wms_outbound.html",
        request,
        heading="出库管理",
        sub="销售 / 报修 / 调拨 / 拆机件四类出库，拣货扫码校验错货错等级当场拦",
        orders=rows,
        total=len(rows),
        types=[[t, OUTBOUND_LABELS[t]] for t in OUTBOUND_CHOICES],
        statuses=[[s, OUTBOUND_STATUS_LABELS[s]] for s in OUTBOUND_STATUS_CHOICES],
        warehouses=_warehouses(db),
        grades=[[g, GRADE_LABELS[g]] for g in GRADE_CHOICES if g in SELLABLE_GRADES],
        filters={"q": q, "type": type, "status": status},
    )


@router.post("/wms/outbound")
async def wms_outbound_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        row = outbound_service.create(db, form, operator=_user(request))
    except ValueError as exc:
        return _redirect("/wms/outbound", str(exc), "warn")
    return _redirect(f"/wms/outbound/{row.id}", f"出库单 {row.order_no} 已创建")


def _outbound_trace(db: Session, order, *, force: bool = False) -> dict:
    """出库单的物流轨迹：走三端共用的「物流轨迹服务」（带本地存档）。

    渔集是独立子系统（按 wms/__init__.py 的规矩不 import app.* 业务模块），
    这里跟 bridge.py 一样**函数内延迟 import**；没接平台的部署返回空结构，
    页面照常显示，只是没有轨迹。
    """
    empty = {
        "ok": False, "msg": "", "status": "", "nodes": [], "mail_no": "",
        "cp_name": "", "from_cache": False, "queried_at": None,
        "is_final": False, "stale": False,
    }
    mail_no = str(getattr(order, "waybill_no", "") or "").strip()
    if not mail_no:
        return empty
    try:
        from app.services import logistics_trace
    except Exception:  # noqa: BLE001
        return empty
    try:
        return logistics_trace.lookup(
            db,
            mail_no,
            cp_name=str(getattr(order, "express_company", "") or "").strip(),
            phone=str(getattr(order, "receiver_phone", "") or "").strip(),
            force=force,
        )
    except Exception as exc:  # noqa: BLE001
        return dict(empty, msg=f"物流轨迹查询失败：{exc}")


@router.get("/wms/outbound/{order_id}", response_class=HTMLResponse)
def wms_outbound_detail(
    request: Request, order_id: int, force: str = "", db: Session = Depends(get_db)
):
    from ..models import WmsOutboundOrder

    order = db.get(WmsOutboundOrder, order_id)
    if not order:
        raise HTTPException(404, "出库单不存在")
    return render(
        "wms_outbound_detail.html",
        request,
        heading=order.order_no,
        trace=_outbound_trace(db, order, force=force == "1"),
        sub=f"{OUTBOUND_LABELS.get(order.type, order.type)} · "
            f"{OUTBOUND_STATUS_LABELS.get(order.status, order.status)}",
        crumb_path="/wms/outbound",
        crumb_leaf=order.order_no,
        order=order,
        # 数量带出来（一台货可以带 N 台，明细表只存快照）
        lines=_attach_qty(db, outbound_service.lines_of(db, order.id)),
        candidates=intake.list_items(
            db, status="SELLABLE", warehouse=order.warehouse_code, limit=100
        ),
        warehouses=_warehouses(db),
    )


@router.post("/wms/outbound/{order_id}/pick")
async def wms_outbound_pick(request: Request, order_id: int, db: Session = Depends(get_db)):
    from ..models import WmsOutboundOrder

    order = db.get(WmsOutboundOrder, order_id)
    if not order:
        raise HTTPException(404, "出库单不存在")
    form = await request.form()
    action = str(form.get("action") or "pick").strip()
    operator = _user(request)
    barcode = str(form.get("barcode") or "").strip()
    try:
        if action == "lock":
            if not barcode:
                raise ValueError("请先扫条码")
            item = intake.find_item(db, barcode)
            if not item:
                raise ValueError(f"未知条码 {barcode}")
            outbound_service.lock(db, order, item, operator=operator)
            msg = f"{item.display_code} 已锁定"
        elif action == "pick":
            if not barcode:
                raise ValueError("请先扫条码")
            line = outbound_service.pick(
                db, order, barcode,
                require_grade=str(form.get("require_grade") or ""),
                operator=operator,
            )
            msg = f"拣货校验通过：{line.barcode}"
        else:
            raise ValueError("未知动作")
    except ValueError as exc:
        return _redirect(f"/wms/outbound/{order_id}", str(exc), "warn")
    return _redirect(f"/wms/outbound/{order_id}", msg)


@router.post("/wms/outbound/{order_id}/ship")
async def wms_outbound_ship(request: Request, order_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        order = outbound_service.ship(
            db, order_id,
            operator=_user(request),
            waybill_no=str(form.get("waybill_no") or ""),
            express_company=str(form.get("express_company") or ""),
        )
    except ValueError as exc:
        return _redirect(f"/wms/outbound/{order_id}", str(exc), "warn")
    # 卖掉的货同时把渔枢商品库存与渔货栈平台库存减掉了（2026-10-10），同步结果说清楚
    note = wms_bridge.hub_sync_note(getattr(order, "hub_sync", None))
    return _redirect(f"/wms/outbound/{order_id}",
                     "出库核销完成，成本已结转" + (f"；{note}" if note else ""))


@router.post("/wms/outbound/{order_id}/cancel")
def wms_outbound_cancel(order_id: int, db: Session = Depends(get_db)):
    try:
        outbound_service.cancel(db, order_id, operator="")
    except ValueError as exc:
        return _redirect(f"/wms/outbound/{order_id}", str(exc), "warn")
    return _redirect(f"/wms/outbound/{order_id}", "出库单已取消，锁定的货已释放", "warn")


# ---------------------------------------------------------------- 报修
@router.get("/wms/repairs", response_class=HTMLResponse)
def wms_repairs(request: Request, q: str = "", status: str = "", timeout: str = "",
                db: Session = Depends(get_db)):
    rows = repairs_service.list_orders(
        db, q=q, status=status, only_timeout=(timeout == "1")
    )
    return render(
        "wms_repairs.html",
        request,
        heading="报修工单",
        sub="C1 线上（京东 / 400）/ C2 线下（授权网点）双通道，超时未回自动预警",
        orders=rows,
        total=len(rows),
        stats=repairs_service.stats(db),
        channels=list(REPAIR_CHANNEL_LABELS.items()),
        statuses=[[s, REPAIR_STATUS_LABELS[s]] for s in REPAIR_STATUS_CHOICES],
        candidates=intake.list_items(db, grade="C", limit=100),
        filters={"q": q, "status": status, "timeout": timeout},
    )


@router.post("/wms/repairs")
async def wms_repair_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    item = db.get(WmsItem, _form_int(form.get("item_id")))
    if not item:
        return _redirect("/wms/repairs", "请选择要报修的货品", "warn")
    try:
        row = repairs_service.create(db, item, form, operator=_user(request))
    except ValueError as exc:
        return _redirect("/wms/repairs", str(exc), "warn")
    return _redirect(f"/wms/repairs/{row.id}", f"报修单 {row.order_no} 已创建")


@router.get("/wms/repairs/{repair_id}", response_class=HTMLResponse)
def wms_repair_detail(request: Request, repair_id: int, db: Session = Depends(get_db)):
    from ..models import WmsRepairOrder

    row = db.get(WmsRepairOrder, repair_id)
    if not row:
        raise HTTPException(404, "报修工单不存在")
    return render(
        "wms_repair_detail.html",
        request,
        heading=row.order_no,
        sub=f"{REPAIR_CHANNEL_LABELS.get(row.channel, row.channel)} · {row.vendor} · "
            f"{REPAIR_STATUS_LABELS.get(row.status, row.status)}",
        crumb_path="/wms/repairs",
        crumb_leaf=row.order_no,
        order=row,
        item=db.get(WmsItem, row.item_id) if row.item_id else None,
        logs=wms_audit.of_entity(db, "repair", row.id),
    )


@router.post("/wms/repairs/{repair_id}/actions")
async def wms_repair_action(request: Request, repair_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    action = str(form.get("action") or "").strip()
    operator = _user(request)
    try:
        if action == "send":
            repairs_service.send(db, repair_id, waybill_no=str(form.get("waybill_no") or ""),
                                 operator=operator)
            msg = "已登记送修，开始时效倒计时"
        elif action == "return":
            repairs_service.return_back(db, repair_id, operator=operator)
            msg = "已登记修回：货品进复检池，必须重新检测定级"
        elif action == "cancel":
            repairs_service.cancel(db, repair_id, reason=str(form.get("reason") or ""),
                                   operator=operator)
            msg = "已撤单：货回仓后需重新检测定级（可能降档）"
        elif action == "close":
            repairs_service.close(db, repair_id, operator=operator,
                                  note=str(form.get("note") or ""))
            msg = "工单已结案"
        else:
            raise ValueError("未知动作")
    except ValueError as exc:
        return _redirect(f"/wms/repairs/{repair_id}", str(exc), "warn")
    return _redirect(f"/wms/repairs/{repair_id}", msg)


# ---------------------------------------------------------------- 拆机件与残值
@router.get("/wms/parts", response_class=HTMLResponse)
def wms_parts(request: Request, db: Session = Depends(get_db)):
    items = parts_service.pool(db)
    return render(
        "wms_parts.html",
        request,
        heading="拆机件与残值",
        sub="D 档强制逐件估值，无残值明细不允许关闭工单",
        items=items,
        rate=parts_service.recovery_rate(db),
        part_choices=[[p, PART_LABELS[p]] for p in PART_CHOICES],
        candidates=intake.list_items(db, grade="D", limit=100),
        part_rows={it.id: parts_service.of_item(db, it.id) for it in items},
    )


@router.post("/wms/parts/add")
async def wms_parts_add(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    item = db.get(WmsItem, _form_int(form.get("item_id")))
    if not item:
        return _redirect("/wms/parts", "请选择货品", "warn")
    try:
        parts_service.add(
            db, item,
            part_type=str(form.get("part_type") or "other"),
            part_name=str(form.get("part_name") or ""),
            est_value=_form_money(form.get("est_value")),
            operator=_user(request),
            note=str(form.get("note") or ""),
        )
    except ValueError as exc:
        return _redirect("/wms/parts", str(exc), "warn")
    return _redirect("/wms/parts", f"已登记残值明细，{item.display_code} 理论残值 ¥{parts_service.estimate_total(db, item.id):.2f}")


@router.post("/wms/parts/{part_id}/sell")
async def wms_parts_sell(request: Request, part_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        parts_service.sell(db, part_id, _form_money(form.get("actual_sold_price")),
                           operator=_user(request))
    except ValueError as exc:
        return _redirect("/wms/parts", str(exc), "warn")
    return _redirect("/wms/parts", "已记录实际售价，残值回收率已回写")


# ---------------------------------------------------------------- 盘点
@router.get("/wms/stocktake", response_class=HTMLResponse)
def wms_stocktake(request: Request, status: str = "", db: Session = Depends(get_db)):
    rows = stocktake_service.list_takes(db, status=status)
    return render(
        "wms_stocktake.html",
        request,
        heading="盘点与差异处理",
        sub="扫一个记一个，未扫到的不自动算盘亏（防误判）；差异需审批后才生成调整流水",
        takes=rows,
        scopes=list(STOCKTAKE_SCOPE_LABELS.items()),
        statuses=[[s, STOCKTAKE_STATUS_LABELS[s]] for s in STOCKTAKE_STATUS_CHOICES],
        warehouses=_warehouses(db),
        grades=[[g, GRADE_LABELS[g]] for g in GRADE_CHOICES],
        batches=intake.list_batches(db),
        filters={"status": status},
    )


@router.post("/wms/stocktake")
async def wms_stocktake_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        row = stocktake_service.create(db, form, operator=_user(request))
    except ValueError as exc:
        return _redirect("/wms/stocktake", str(exc), "warn")
    return _redirect(f"/wms/stocktake/{row.id}", f"盘点单 {row.take_no} 已开（{row.plan_qty} 件待盘）")


@router.get("/wms/stocktake/{take_id}", response_class=HTMLResponse)
def wms_stocktake_detail(request: Request, take_id: int, db: Session = Depends(get_db)):
    from ..models import WmsStocktake

    take = db.get(WmsStocktake, take_id)
    if not take:
        raise HTTPException(404, "盘点单不存在")
    return render(
        "wms_stocktake_detail.html",
        request,
        heading=take.take_no,
        sub=f"{STOCKTAKE_SCOPE_LABELS.get(take.scope, take.scope)} · "
            f"{STOCKTAKE_STATUS_LABELS.get(take.status, take.status)}",
        crumb_path="/wms/stocktake",
        crumb_leaf=take.take_no,
        take=take,
        lines=_attach_qty(db, stocktake_service.lines_of(db, take.id)),
    )


@router.post("/wms/stocktake/{take_id}/scan")
async def wms_stocktake_scan(request: Request, take_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        stocktake_service.scan(db, take_id, str(form.get("barcode") or ""),
                               operator=_user(request))
    except ValueError as exc:
        return _redirect(f"/wms/stocktake/{take_id}", str(exc), "warn")
    return _redirect(f"/wms/stocktake/{take_id}", "已记录")


@router.post("/wms/stocktake/{take_id}/finish")
def wms_stocktake_finish(take_id: int, db: Session = Depends(get_db)):
    try:
        take = stocktake_service.finish(db, take_id, operator="")
    except ValueError as exc:
        return _redirect(f"/wms/stocktake/{take_id}", str(exc), "warn")
    msg = f"盘点结束：账面 {take.plan_qty} / 实盘 {take.actual_qty} / 差异 {take.diff_qty}"
    if take.need_upgrade:
        msg += "（差异超阈值，已升级主管核查）"
    return _redirect(f"/wms/stocktake/{take_id}", msg, "warn" if take.need_upgrade else "")


@router.post("/wms/stocktake/{take_id}/approve")
async def wms_stocktake_approve(request: Request, take_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        stocktake_service.approve(db, take_id, approver=_user(request),
                                  note=str(form.get("note") or ""))
    except ValueError as exc:
        return _redirect(f"/wms/stocktake/{take_id}", str(exc), "warn")
    return _redirect(f"/wms/stocktake/{take_id}", "已审批，盘亏已生成调整流水")


# ---------------------------------------------------------------- 分级规则
@router.get("/wms/rules", response_class=HTMLResponse)
def wms_rules(request: Request, edit: int = 0, db: Session = Depends(get_db)):
    rules = list(
        db.scalars(
            select(WmsGradeRule).order_by(WmsGradeRule.priority.asc(), WmsGradeRule.id.asc())
        ).all()
    )
    # 同档位重影（2026-10-10）：同品类 + 同档位 + 同红线标记 + **条件也一模一样** 才算重影 ——
    # 判级只认先匹配的那条，真正被挡住的就是这种；条件不同的多条只是分层生效
    # （比如两条 B：不影响使用的判 B、故障厂内可修也判 B），不算毛病，别把正常规则也标红。
    twins: dict[str, int] = {}
    for r in rules:
        key = (_rule_twin_key(r.category, r.grade, r.safety_flag)
               + "|" + grading.conditions_key(r.conditions or {}))
        twins[key] = twins.get(key, 0) + 1
    for r in rules:
        r.twin_count = twins[(_rule_twin_key(r.category, r.grade, r.safety_flag)
                              + "|" + grading.conditions_key(r.conditions or {}))]
    # 「编辑」用 ?edit=<id>（GET，不改状态）：把这条规则原样填进下面的表单原地改，
    # 以前只有「新增」，现场想删个条件只能新建一条 —— 于是同档位越堆越多（2026-10-10）
    editing = db.get(WmsGradeRule, edit) if edit else None
    # 价格系数按**分级规则的优先级**排（2026-10-11）：以前按 grade 字母排是 A/B/C/D/S，
    # S 反而排最后 —— 而 S 恰恰是优先级最高、最先匹配的那一档，看的人会以为越靠后越差。
    prices = list(db.scalars(select(WmsPriceRule)).all())
    grade_order = _grade_priority_order(rules, GRADE_CHOICES)
    prices.sort(
        key=lambda p: (grade_order.get(str(p.grade or "").upper(), 99), str(p.grade or ""))
    )
    # 品类对标管理数据：三级对三级（收货三级 ↔ 渔枢系统三级）+ 已挂规则数 + 在库货品数，
    # 让「该给哪个品类补规则 / 哪些还没对标」一眼可见（依据来自《类目对标看板》）
    rule_count: dict[str, int] = {}
    for r in rules:
        key = str(r.category or "*").upper()
        rule_count[key] = rule_count.get(key, 0) + 1
    # 「在库货品」的口径（2026-10-08 在页面上写清楚，免得看不懂）：**现在还在库里的台数**
    # —— 已售 / 危废这类终态不算；括号里是其中还没检测（待检）的台数。
    item_count: dict[str, int] = {}
    pending_count: dict[str, int] = {}
    for cat, status, n in db.execute(
        select(WmsItem.category, WmsItem.status, func.count(WmsItem.id))
        .where(WmsItem.status.notin_(TERMINAL_STATUSES))
        .group_by(WmsItem.category, WmsItem.status)
    ).all():
        key = str(cat or "").upper()
        item_count[key] = item_count.get(key, 0) + int(n or 0)
        if str(status or "").upper() == STATUS_PENDING:
            pending_count[key] = pending_count.get(key, 0) + int(n or 0)
    category_rows = []
    for code in leaf_codes():
        leaf = category_leaf(code) or {}
        conf = str(leaf.get("conf") or "")
        category_rows.append(
            {
                "code": code,
                "path": category_path(code),
                "map": str(leaf.get("map") or ""),
                "tickets": int(leaf.get("tickets") or 0),
                "confidence": conf,
                "confidence_tone": CATEGORY_CONFIDENCE_TONE.get(conf, "muted"),
                "rules": rule_count.get(code, 0),
                # 别叫 items：Jinja 里 dict.items 是方法，{{ c.items }} 会渲染成方法对象
                "stock": item_count.get(code, 0),
                "pending": pending_count.get(code, 0),
            }
        )
    return render(
        "wms_rules.html",
        request,
        heading="分级规则与价格系数",
        sub="分级标准入库为配置项，改标准不改代码（生效期保证历史数据不被追溯重判）",
        rules=rules,
        prices=prices,
        grades=[[g, GRADE_LABELS[g]] for g in GRADE_CHOICES if g != "W"],
        # 价格系数那一档的下拉与表格同一个顺序（按规则优先级），别两套顺序打架
        price_grades=[
            [g, GRADE_LABELS.get(g, g)]
            for g in sorted(grade_order, key=lambda k: grade_order[k])
            if g != "W"
        ],
        categories=category_options(),
        category_tree=CATEGORY_TREE,
        category_rows=category_rows,
        generic_rules=rule_count.get("*", 0),
        # 条件的人话字典：字段中文名 / 运算符 / 可照抄的示例（页面用它拼条件，不用手写 JSON）
        cond_fields=grading.CONDITION_FIELDS,
        cond_ops=grading.CONDITION_OPS,
        cond_examples=grading.CONDITION_EXAMPLES,
        # 规则体检（2026-10-11）：循环所有规则把「每条用了哪些条件项」摆成对照表，
        # 并挑出「条件项不统一 / 条件一样档位不同 / 兜底档排太前」（现场要求逐档统一条件项）
        rule_check=grading.rule_checklist(rules),
        # 原地编辑（?edit=<id>）：表单预填这条规则的现值；没在编辑就是新增
        editing=editing,
        editing_json=(
            json.dumps(editing.conditions or {}, ensure_ascii=False) if editing else ""
        ),
        # 被历史检测记录引用过的规则不能删（记录里存了 rule_id，删了就查不到依据）：
        # 这里先把「判过几台货」算出来，表里直接写清楚
        rule_used={r.id: _rule_used(db, r.id) for r in rules},
    )


def _grade_priority_order(rules, grades) -> dict[str, int]:
    """等级 → 排序位次：按「分级规则」表里**第一次出现**的顺序（规则已按优先级升序排好）。

    价格系数表与它的下拉都按这个顺序排（2026-10-11）：按字母排是 A/B/C/D/S，S 排最后，
    而 S 恰恰是优先级最高、最先匹配的那一档。规则里没出现过的等级排在后面（按字母）。
    """
    out: dict[str, int] = {}
    for r in rules:
        g = str(r.grade or "").upper()
        if g and g not in out:
            out[g] = len(out)
    for g in grades or []:
        g = str(g or "").upper()
        if g and g not in out:
            out[g] = len(out)
    return out


def _rule_twin_key(category, grade, safety) -> str:
    """同档位重影的判定键：同品类 + 同档位 + 同「是否安全红线」。

    安全红线单独算一路（它永远第一匹配，跟普通规则本来就不是一回事）：
    线上「安全红线判 D」和「缺件严重判 D」是两条，合并会打乱匹配顺序，不能当重复。
    """
    return f"{str(category or '*').upper()}|{str(grade or '').upper()}|{1 if safety else 0}"


def _rule_used(db: Session, rule_id: int) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(WmsTestRecord).where(WmsTestRecord.rule_id == rule_id)
        )
        or 0
    )


@router.post("/wms/rules/grade")
async def wms_grade_rule_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    rule_id = _form_int(form.get("rule_id"))
    raw = str(form.get("conditions") or "").strip()
    try:
        conditions = json.loads(raw) if raw else {"all_of": []}
        if not isinstance(conditions, dict):
            raise ValueError
    except ValueError:
        return _redirect("/wms/rules", "conditions 必须是 JSON 对象，如 {\"all_of\":[{\"field\":\"flaw_count\",\"op\":\"==\",\"value\":0}]}", "warn")
    row = db.get(WmsGradeRule, rule_id) if rule_id else WmsGradeRule()
    if rule_id and not row:
        return _redirect("/wms/rules", "这条规则不在了（可能已经被删）", "warn")
    category = str(form.get("category") or "*").upper()
    grade = str(form.get("grade") or "A").upper()
    safety = str(form.get("safety_flag") or "") not in ("", "0")
    # 同品类 + 同档位 + 同红线标记只留一条（2026-10-10）：
    # 以前想改一条规则只能在「新增」里改，一保存就多出一模一样的档位，
    # 判级又只认优先级高的那条 —— 现场看着就是「改了没反应，删了又还在」。
    twin = db.scalars(
        select(WmsGradeRule).where(
            WmsGradeRule.category == category,
            WmsGradeRule.grade == grade,
            WmsGradeRule.safety_flag == safety,
        ).order_by(WmsGradeRule.priority.asc())
    ).all()
    others = [t for t in twin if t.id != (row.id or 0)]
    # 新增：同档位一律别再堆第二条；编辑：只跟「启用中」的同档规则冲突
    # （停用的本来就不参与判级，不该挡着人家改自己的规则 —— 2026-10-10 演练时踩到）
    blockers = others if not rule_id else [t for t in others if t.enabled]
    if blockers:
        t = blockers[0]
        if rule_id:
            tail = (f"「{t.title or grade}」（优先级 {t.priority}）还启用着 —— "
                    f"两条同时生效只会认优先级高的那条，先把那条停用或删掉再来改这条")
        else:
            tail = (f"「{t.title or grade}」· 优先级 {t.priority} · "
                    f"{'启用中' if t.enabled else '已停用'} —— 要改它就点那一行的「编辑」；"
                    f"确实要两条同档规则，先把那条停用或删掉")
        where = "通用（所有品类）" if category == "*" else category_label(category)
        return _redirect(
            "/wms/rules",
            f"{where}的 {grade_label(grade)} 档已经有一条了：{tail}",
            "warn",
        )
    row.category = category
    row.priority = _form_int(form.get("priority")) or 100
    row.grade = grade
    row.safety_flag = safety
    row.title = str(form.get("title") or "").strip()
    row.reason = str(form.get("reason") or "").strip()
    # 表单没给 enabled 就保持原值（2026-10-09：以前缺省当停用，新增的规则一保存就是停用的）
    _apply_enabled(form, row)
    row.conditions = conditions
    if not rule_id:
        db.add(row)
    db.flush()
    seed.ensure_defaults(db)
    db.commit()
    # 「判断条件是否对」（2026-10-11）:条件与**别的档位**的内置口径一模一样时提醒一句 ——
    # 现场就是在「D 报废」的表单里照抄了 N / S 的条件，保存完看着条条都对、档位却是 D。
    # 只提醒不拦：确实有人要「满足这些条件的判 D」这种写法。
    warn = grading.mismatch_note(row.conditions or {}, row.grade, bool(row.safety_flag))
    verb = "已更新" if rule_id else "已新增"
    stale = [t for t in others if not t.enabled] if rule_id else []
    tail = f"（同档还留着 {len(stale)} 条已停用的，看着碍事就删掉）" if stale else ""
    if warn:
        return _redirect(
            "/wms/rules",
            f"分级规则「{row.title or row.grade}」{verb}（立即生效）。但有件事要你确认：{warn}",
            "warn",
        )
    return _redirect("/wms/rules", f"分级规则「{row.title or row.grade}」{verb}（立即生效）{tail}")


@router.post("/wms/rules/grade/{rule_id}/delete")
def wms_grade_rule_delete(rule_id: int, db: Session = Depends(get_db)):
    """删掉一条分级规则（2026-10-10）。

    判级是留痕的：检测记录里存着当时命中的 ``rule_id`` 与理由，规则一删那条记录就查不到依据了
    —— 所以**判过货的规则只能停用，不能删**（停用后引擎照样不会用它）。
    """
    row = db.get(WmsGradeRule, rule_id)
    if not row:
        raise HTTPException(404, "规则不存在")
    used = _rule_used(db, rule_id)
    if used:
        return _redirect(
            "/wms/rules",
            f"「{row.title or row.grade}」已经判过 {used} 台货，历史记录还要指着它查依据 —— 不能删；"
            f"点那行的「停用」就够（停用后引擎不再用它判级）",
            "warn",
        )
    name = row.title or row.grade
    db.delete(row)
    db.commit()
    return _redirect("/wms/rules", f"分级规则「{name}」已删除")


@router.post("/wms/rules/grade/{rule_id}/toggle")
def wms_grade_rule_toggle(rule_id: int, db: Session = Depends(get_db)):
    row = db.get(WmsGradeRule, rule_id)
    if not row:
        raise HTTPException(404, "规则不存在")
    row.enabled = not row.enabled
    db.commit()
    return _redirect("/wms/rules", f"规则已{'启用' if row.enabled else '停用'}",
                     "" if row.enabled else "warn")


@router.post("/wms/rules/price")
async def wms_price_rule_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    grade = str(form.get("grade") or "").upper()
    row = db.scalar(select(WmsPriceRule).where(WmsPriceRule.grade == grade))
    if not row:
        row = WmsPriceRule(grade=grade)
        db.add(row)
    row.factor = _form_money(form.get("factor")) or 1.0
    # 建议售价系数（2026-10-10）：建议售价 = 基准价 × 它，渔枢 / 渔货栈建档直接用；
    # 留空按 1.0（建议售价 = 基准价）。渔货价算得比它还高会被压到建议售价（见 grading.price_of）
    row.suggest_factor = _form_money(form.get("suggest_factor")) or 1.0
    row.min_margin = _form_money(form.get("min_margin"))
    row.need_approval_below = _form_money(form.get("need_approval_below"))
    row.note = str(form.get("note") or "").strip()
    _apply_enabled(form, row)
    db.commit()
    state = "" if row.enabled else "warn"
    tail = "" if row.enabled else "（这条是停用状态，检测台还是不认这个系数）"
    over = "（渔货价系数比建议售价系数还高，出价会被压到建议售价 —— 建议把建议售价系数调上去）" \
        if row.factor > row.suggest_factor else ""
    return _redirect("/wms/rules",
                     f"{grade_label(grade)} 档已保存：渔货价 ×{row.factor:g}、"
                     f"建议售价 ×{row.suggest_factor:g}{over}{tail}", state)


@router.post("/wms/rules/price/{rule_id}/toggle")
def wms_price_rule_toggle(rule_id: int, db: Session = Depends(get_db)):
    """价格系数的启用 / 停用。

    停用的档位 ``price_rule_of`` 取不到 → 出价按系数 1.0 兜底（毛利会算成 100%），
    所以这里明确提示一句，别再让人对着 100% 毛利发懵。
    """
    row = db.get(WmsPriceRule, rule_id)
    if not row:
        raise HTTPException(404, "价格系数不存在")
    row.enabled = not row.enabled
    db.commit()
    if row.enabled:
        return _redirect("/wms/rules", f"{grade_label(row.grade)} 档价格系数已启用（检测出价按系数 {row.factor}）")
    return _redirect("/wms/rules", f"{grade_label(row.grade)} 档价格系数已停用（该档出价会按 1.0 兜底）", "warn")


# ---------------------------------------------------------------- 报表
@router.get("/wms/reports", response_class=HTMLResponse)
def wms_reports(request: Request, db: Session = Depends(get_db)):
    return render(
        "wms_reports.html",
        request,
        heading="报表中心",
        sub="两个最该盯的数：残值回收率（D 档有没有被当废品扔）与报修超时单数（货悬在别人手里）",
        data=reports.overview(db),
        grade_labels=GRADE_LABELS,
    )


# ---------------------------------------------------------------- 仓库与货架（库位）
# 五级：仓库 → 区 → 货架 → 层 → 位。默认 7 个仓由 wms/seed 播进来，之后在这里增改删；
# 其它页面（移库 / 出库 / 盘点 / 台账筛选 / 检测台）的仓库与库位全部从这个表出下拉。
@router.get("/wms/warehouses", response_class=HTMLResponse)
def wms_warehouses(request: Request, q: str = "", level: int = 0,
                   db: Session = Depends(get_db)):
    return render(
        "wms_warehouses.html",
        request,
        heading="仓库与货架",
        sub="仓库 → 区 → 货架 → 层 → 位五级；在这里维护，其它页面的仓库与库位都从它出（只能下拉选）",
        rows=warehouse_service.page_rows(db, q=q, level=level),
        all_rows=warehouse_service.page_rows(db),  # 上级下拉要全部候选
        levels=warehouse_service.level_choices(),
        zones=list(GRADE_LABELS.items()),
        q=q,
        level=level,
    )


@router.post("/wms/warehouses")
async def wms_warehouse_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        row, action = warehouse_service.save(
            db,
            wh_id=_form_int(form.get("wh_id")) or 0,
            code=str(form.get("code") or ""),
            name=str(form.get("name") or ""),
            level=_form_int(form.get("level")) or 1,
            parent_code=str(form.get("parent_code") or ""),
            grade_zone=str(form.get("grade_zone") or ""),
            can_out=str(form.get("can_out") or "") not in ("", "0"),
            sort=str(form.get("sort") or "").strip() or None,
            enabled=str(form.get("enabled") or "") not in ("", "0"),
            note=str(form.get("note") or ""),
        )
    except ValueError as exc:
        return _redirect("/wms/warehouses", str(exc), "warn")
    return _redirect("/wms/warehouses", f"{action}（{row.full_path}）")


@router.post("/wms/warehouses/{wh_id}/move")
def wms_warehouse_move(wh_id: int, dir: str = "", db: Session = Depends(get_db)):
    try:
        warehouse_service.move(db, wh_id, dir or "up")
    except ValueError as exc:
        return _redirect("/wms/warehouses", str(exc), "warn")
    return _redirect("/wms/warehouses", "顺序已调整")


@router.post("/wms/warehouses/{wh_id}/toggle")
def wms_warehouse_toggle(wh_id: int, db: Session = Depends(get_db)):
    try:
        row = warehouse_service.toggle(db, wh_id)
    except ValueError as exc:
        return _redirect("/wms/warehouses", str(exc), "warn")
    if row.enabled:
        return _redirect("/wms/warehouses", f"「{row.name}」已启用")
    return _redirect(
        "/wms/warehouses",
        f"「{row.name}」已停用 —— 新作业的下拉里不再出现，历史货品与流水照旧",
        "warn",
    )


@router.post("/wms/warehouses/{wh_id}/delete")
def wms_warehouse_delete(wh_id: int, db: Session = Depends(get_db)):
    try:
        msg = warehouse_service.delete(db, wh_id)
    except ValueError as exc:
        return _redirect("/wms/warehouses", str(exc), "warn")
    return _redirect("/wms/warehouses", msg)


# ---------------------------------------------------------------- 更新日志
# 渔集的更新与渔枢系统 / 渔货栈记在平台同一份 config/changelog.json 里
# （渔集的条目标着 system="wms"），本页只挑渔集的显示 —— 登录即可看，不需要权限。
@router.get("/wms/changelog", response_class=HTMLResponse)
def wms_changelog_page(request: Request):
    rows = wms_changelog.entries()
    return render(
        "wms_changelog.html",
        request,
        heading="更新日志",
        sub=f"渔集的每一次上线（共 {len(rows)} 次）",
        entries=rows,
        version=wms_changelog.current_version(),
    )


# ---------------------------------------------------------------- 系统设计方案
# 把整套方案（业务流程 / 分级引擎 / 权限模型 / 接口设计）原样挂进系统里，
# 让使用者随时能翻到设计思路 —— 文档本身是一个自包含的单页 HTML（自带样式与交互），
# 用 iframe 装进来，跟系统主题互不污染。
_DESIGN_DOCS: dict[str, str] = {
    "design_plan.html": "货品检测入库出库管理系统 · 详细方案",
}
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@router.get("/wms/design", response_class=HTMLResponse)
def wms_design(request: Request):
    return render(
        "wms_design.html",
        request,
        heading="系统设计方案",
        sub="为什么这么设计：业务流程、分级引擎、一码到底、权限模型的设计思路全在这里",
        doc_src="/wms/static/design_plan.html",
        doc_title=_DESIGN_DOCS["design_plan.html"],
    )


@router.get("/wms/static/{name}")
def wms_static_file(name: str):
    """静态文档（白名单，不做目录穿越）；找不到就 404。"""
    if name not in _DESIGN_DOCS:
        raise HTTPException(status_code=404, detail="文件不存在")
    path = _STATIC_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文档未随系统发布")
    return FileResponse(path, media_type="text/html; charset=utf-8")


# ---------------------------------------------------------------- 账本
@router.get("/wms/ledger", response_class=HTMLResponse)
def wms_ledger(request: Request, db: Session = Depends(get_db)):
    from ..models import WmsStockLedger

    rows = list(
        db.scalars(select(WmsStockLedger).order_by(WmsStockLedger.id.desc()).limit(200)).all()
    )
    return render(
        "wms_ledger.html",
        request,
        heading="库存账本",
        sub="余额由流水算出，流水只增不改（幂等键防重复提交）",
        ledgers=rows,
        balances=ledger.balances(db),
        labels=LEDGER_LABELS,
        negative=ledger.negative_balances(db),
    )


# ---------------------------------------------------------------- 无权限提示
@router.get("/wms/denied", response_class=HTMLResponse)
def wms_denied(request: Request, perm: str = "", path: str = "", db: Session = Depends(get_db)):
    """被守卫拦下来的落点：说清楚「要哪个权限、找谁要」，而不是甩一个 403 白页。"""
    admins: list[str] = []
    for row in db.scalars(select(WmsUserRole)).all():
        role = db.get(WmsRole, int(row.role_id or 0))
        if role is not None and role.code == "admin":
            admins.append(str(row.username or row.user_id))
    return render(
        "wms_denied.html",
        request,
        perm=perm,
        # 权限码给人看是全类名，这里翻成业务话 —— 页面要写「要哪个权限、找谁要」
        perm_label=dict((c, l) for _g, ps in PERM_GROUPS for c, l in ps).get(
            perm, perm or "对应操作"
        ),
        from_path=path,
        contact=(",".join(dict.fromkeys(admins)) or "仓库主管"),
    )


# ---------------------------------------------------------------- 人员与授权
@router.get("/wms/members", response_class=HTMLResponse)
def wms_members(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
):
    """检测仓的授权表：角色 -> 权限，账号 -> 角色。"""
    role_rows = []
    for role in wms_permissions.roles(db):
        perms = sorted(wms_permissions.perms_of_role(db, role.id))
        role_rows.append(
            {
                "id": role.id,
                "code": role.code,
                "name": role.name,
                "remark": role.remark,
                "builtin": role.builtin,
                "perms": perms,
            }
        )

    members: dict[int, dict] = {}
    granted: dict[int, set[int]] = {}
    for row in db.scalars(select(WmsUserRole).order_by(WmsUserRole.id.asc())).all():
        uid = int(row.user_id or 0)
        info = members.setdefault(
            uid,
            {
                "user_id": uid,
                "username": row.username,
                "display_name": row.display_name,
                "role_names": [],
            },
        )
        role = db.get(WmsRole, int(row.role_id or 0))
        if role is not None:
            if role.name not in info["role_names"]:
                info["role_names"].append(role.name)
            granted.setdefault(uid, set()).add(role.id)

    users = wms_bridge.platform_users(db, q)
    return render(
        "wms_members.html",
        request,
        role_rows=role_rows,
        perm_groups=PERM_GROUPS,
        all_perms=list(wms_permissions.ALL_PERMS),
        members=list(members.values()),
        granted=granted,
        users=users,
        bridge_ok=bool(users) or True,
        q=q,
    )


@router.post("/wms/members/grant")
async def wms_members_grant(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    user_id = _form_int(form.get("user_id")) or 0
    if not user_id:
        return _redirect("/wms/members", "缺 user_id", "warn")
    role_ids = [_form_int(v) for v in form.getlist("role_ids") if _form_int(v)]
    try:
        wms_permissions.grant(
            db,
            user_id=user_id,
            username=str(form.get("username") or ""),
            display_name=str(form.get("display_name") or ""),
            role_ids=role_ids,
            actor=_user(request),
        )
    except ValueError as exc:
        return _redirect("/wms/members", str(exc), "warn")
    names = wms_permissions.role_names_of(db, user_id)
    return _redirect(
        "/wms/members",
        f"已更新授权：{form.get('username') or user_id} → {('、'.join(names) or '无角色（等于出仓）')}",
    )


@router.post("/wms/members/revoke")
async def wms_members_revoke(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    user_id = _form_int(form.get("user_id")) or 0
    if user_id:
        wms_permissions.revoke(db, user_id)
    return _redirect("/wms/members", "已移出检测仓（平台账号本身不受影响）")


@router.post("/wms/members/role")
async def wms_members_role(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        row = wms_permissions.save_role(
            db,
            role_id=_form_int(form.get("role_id")) or 0,
            code=str(form.get("code") or ""),
            name=str(form.get("name") or ""),
            remark=str(form.get("remark") or ""),
            perms=[str(p) for p in form.getlist("perms")],
        )
    except ValueError as exc:
        return _redirect("/wms/members", str(exc), "warn")
    return _redirect("/wms/members", f"角色已保存：{row.name}（{len(form.getlist('perms'))} 项权限）")


@router.post("/wms/members/role/delete")
async def wms_members_role_delete(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        wms_permissions.delete_role(db, _form_int(form.get("role_id")) or 0)
    except ValueError as exc:
        return _redirect("/wms/members", str(exc), "warn")
    return _redirect("/wms/members", "角色已删除")
