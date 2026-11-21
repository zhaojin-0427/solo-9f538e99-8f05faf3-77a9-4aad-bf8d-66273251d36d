"""FastAPI 入口：REST 接口 + 单页操作界面。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import services
from . import database
from .database import init_db
from .errors import ApiError
from .schemas import PartitionIn, PreviewIn, SliceIn, WindowIn


@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = database.get_conn()
    try:
        init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="风机振动分区更正补算清单",
    description="三日特征窗口 / 告警评分切片版本登记、更正影响面预览与确认单追溯",
    version="1.0.0",
    lifespan=lifespan,
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


# --------------------------------------------------------------------------- #
# 登记接口
# --------------------------------------------------------------------------- #
@app.post("/api/partitions", status_code=201)
def upload_partition(payload: PartitionIn):
    """上传风机某 UTC 日期的分区修订（修订号严格递增）。"""
    conn = database.get_conn()
    try:
        result = services.upload_partition(conn, payload)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/windows", status_code=201)
def register_window(payload: WindowIn):
    """登记三日特征窗口版本（窗口取 end_date 及前两日）。"""
    conn = database.get_conn()
    try:
        result = services.register_window(conn, payload)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/slices", status_code=201)
def register_slice(payload: SliceIn):
    """登记告警评分切片版本及其引用的窗口版本。"""
    conn = database.get_conn()
    try:
        result = services.register_slice(conn, payload)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 预览与确认
# --------------------------------------------------------------------------- #
@app.post("/api/previews", status_code=201)
def create_preview(payload: PreviewIn):
    """按当前已提交状态生成补算清单预览并留存版本引用。"""
    conn = database.get_conn()
    try:
        preview_id, impact = services.create_preview(conn, payload.corrections)
        conn.commit()
        return {"preview_id": preview_id, "impact": impact}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/previews/{preview_id}/confirm")
def confirm_preview(preview_id: int):
    """按所见版本确认；版本漂移则拒绝。成功返回确认号（保存失败不发号、不留单）。"""
    conn = database.get_conn()
    try:
        result = services.confirm_preview(conn, preview_id)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/previews/{preview_id}")
def get_preview(preview_id: int):
    """打开预览单；确认后仍可凭留存引用查看原清单。"""
    conn = database.get_conn()
    try:
        return services.get_order(conn, preview_id)
    finally:
        conn.close()


@app.get("/api/orders/{confirmation_no}")
def get_order(confirmation_no: str):
    """按确认号打开旧单，用留存的源修订/窗口/切片引用复算原清单。"""
    conn = database.get_conn()
    try:
        return services.find_order_by_confirmation_no(conn, confirmation_no)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 查询接口
# --------------------------------------------------------------------------- #
@app.get("/api/partitions")
def list_partitions():
    conn = database.get_conn()
    try:
        return services.list_rows(conn, "partition_rev")
    finally:
        conn.close()


@app.get("/api/windows")
def list_windows():
    conn = database.get_conn()
    try:
        return services.list_rows(conn, "feature_window")
    finally:
        conn.close()


@app.get("/api/slices")
def list_slices():
    conn = database.get_conn()
    try:
        return services.list_slices(conn)
    finally:
        conn.close()


@app.get("/api/previews")
def list_previews():
    conn = database.get_conn()
    try:
        return services.list_previews(conn)
    finally:
        conn.close()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
