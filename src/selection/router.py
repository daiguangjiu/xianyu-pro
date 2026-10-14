"""Selection 路由：手动触发一次选品流水线。"""
from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from src.deps import get_db
from src.schemas import SelectionRunRequest, SelectionRunResponse
from src.selection import run_selection as run_selection_engine
from src.worker.tasks import run_selection_task  # celery 任务入口

router = APIRouter()


@router.post("/run", response_model=SelectionRunResponse, summary="触发一次选品规则流水线")
async def run_selection(req: SelectionRunRequest, db: Session = Depends(get_db)):
    """同步执行一次选品（适合小批量 / 调试）。

    生产环境建议走 Celery：`run_selection_task.delay()`。
    """
    result = run_selection_engine(
        db,
        item_ids=req.item_ids,
        channel="xianyu",
        shadow_mode=req.shadow_mode,
    )
    return SelectionRunResponse(
        total=result.total,
        passed=result.passed,
        rejected=result.rejected,
        rejected_reasons=result.rejected_reasons,
    )


@router.post("/run-async", summary="异步触发选品（Celery）")
async def run_selection_async(req: SelectionRunRequest, background: BackgroundTasks):
    task = run_selection_task.delay(req.item_ids, req.shadow_mode)
    return {"task_id": task.id}