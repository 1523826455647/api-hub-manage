"""
中转站渠道整合管理工具
支持 NewAPI / Sub2API 类型的中转站账号管理
"""
import json
import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from routes import router

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时确保数据文件存在
    accounts_file = DATA_DIR / "accounts.json"
    if not accounts_file.exists():
        accounts_file.write_text("[]", encoding="utf-8")
    yield


app = FastAPI(title="中转站渠道整合管理", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(router, prefix="/api")

templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


if __name__ == "__main__":
    import uvicorn
    # reload=False：避免 Windows 上双进程 + 热重载导致的启动慢/请求卡住
    # 开发时如需热重载可改为 reload=True
    uvicorn.run("main:app", host="0.0.0.0", port=8899, reload=False)
