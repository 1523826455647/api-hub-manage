"""后端 API 路由"""
import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from services.newapi import NewAPIAdapter
from services.sub2api import Sub2APIAdapter
from services import capsolver, new_async_client

router = APIRouter()
DATA_DIR = Path(__file__).parent / "data"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
SETTINGS_FILE = DATA_DIR / "settings.json"


# === 请求模型 ===

class AccountCreate(BaseModel):
    name: str
    platform: str  # newapi | sub2api
    base_url: str
    auth_type: str  # token | login
    access_token: str | None = None
    credential_type: str = "token"  # cookie | token | bearer | user_api_key
    username: str | None = None
    password: str | None = None
    recharge_ratio: float = 1.0  # 充值比例如 1:1=1.0, 1:10=10.0
    upstream_key: str | None = None  # 用于 Sub2API 联动的 sk- 密钥


class RedeemRequest(BaseModel):
    code: str


class SettingsUpdate(BaseModel):
    capsolver_api_key: str | None = None
    hub_base_url: str | None = None
    hub_email: str | None = None
    hub_password: str | None = None
    hub_api_key: str | None = None  # 可选：Admin x-api-key，优先于邮箱密码
    ultra_low_rate: float | None = None  # 超低价阈值，低于此倍率为超低价


# === 数据持久化 ===

def _load_accounts() -> list[dict]:
    if not ACCOUNTS_FILE.exists():
        return []
    return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))


def _save_accounts(accounts: list[dict]):
    ACCOUNTS_FILE.write_text(
        json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))


def _save_settings(settings: dict):
    SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# === 缓存系统 ===
# 内存缓存 + 磁盘持久化：重启后也能秒开上次数据
_cache: dict[str, dict[str, Any]] = {}
DASHBOARD_DISK_CACHE = DATA_DIR / "dashboard_cache.json"

CACHE_TTL_DASHBOARD = 300  # 仪表盘缓存 5 分钟
CACHE_TTL_ACCOUNT = 180    # 单个账号缓存 3 分钟

# 后台刷新状态：防止并发 force 刷新互相踩踏
_refresh_lock = asyncio.Lock()
_refreshing = False


def _cache_get(key: str, ttl: int) -> Any | None:
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < ttl:
        return entry["data"]
    return None


def _cache_set(key: str, data: Any):
    _cache[key] = {"data": data, "ts": time.time()}
    # 仪表盘数据额外落盘，重启后秒开
    if key == "dashboard" and isinstance(data, dict) and data.get("accounts"):
        try:
            payload = {"ts": time.time(), "data": data}
            DASHBOARD_DISK_CACHE.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass


def _cache_invalidate(prefix: str = ""):
    if not prefix:
        _cache.clear()
    else:
        keys = [k for k in _cache if k.startswith(prefix)]
        for k in keys:
            del _cache[k]


def _load_dashboard_disk_cache() -> dict | None:
    """从磁盘加载上次仪表盘快照（进程启动时调用）"""
    if not DASHBOARD_DISK_CACHE.exists():
        return None
    try:
        payload = json.loads(DASHBOARD_DISK_CACHE.read_text(encoding="utf-8"))
        data = payload.get("data")
        if isinstance(data, dict) and data.get("accounts"):
            data["from_cache"] = True
            # 写入内存缓存（不过期，直到 force 刷新）
            _cache["dashboard"] = {"data": data, "ts": payload.get("ts", time.time())}
            return data
    except (json.JSONDecodeError, OSError, TypeError):
        pass
    return None


# 模块导入时立刻尝试加载磁盘缓存
_load_dashboard_disk_cache()

def _get_adapter(account: dict):
    """根据平台类型获取对应的适配器"""
    platform = account.get("platform", "newapi")
    base_url = account["base_url"]
    token = account.get("access_token", "")
    cred_type = account.get("credential_type", "token")

    if platform == "sub2api":
        adapter = Sub2APIAdapter(base_url, token, credential_type=cred_type)
        adapter.refresh_token = account.get("refresh_token", "")
        return adapter
    else:
        adapter = NewAPIAdapter(base_url, token, credential_type=cred_type)
        if account.get("user_id"):
            adapter.user_id = account["user_id"]
        return adapter


# 单渠道整体拉取超时上限（秒）：防止某个挂掉/极慢的中转站拖垮整个仪表盘
ACCOUNT_FETCH_TIMEOUT = 12


async def _account_summary(account: dict) -> dict[str, Any]:
    """并发拉取单个渠道的 余额/消耗/分组，返回仪表盘所需的 summary 结构。

    三个上游请求并发执行；整体带超时上限，任一渠道故障不影响其它渠道。
    """
    adapter = _get_adapter(account)
    rr = account.get("recharge_ratio", 1.0) or 1.0
    s: dict[str, Any] = {
        "id": account["id"], "name": account["name"], "platform": account["platform"],
        "base_url": account["base_url"], "recharge_ratio": rr,
        "credential_type": account.get("credential_type", "token"),
        # 渠道级上游 Key（不含分组级）；分组级在扫描时再判
        "has_upstream_key": bool(_looks_like_api_key(account.get("upstream_key", "") or "")),
    }
    try:
        balance_r, usage_r, groups_r = await asyncio.wait_for(
            asyncio.gather(
                adapter.get_balance(),
                adapter.get_usage(),
                adapter.get_groups(),
                return_exceptions=True,
            ),
            timeout=ACCOUNT_FETCH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        s["balance"] = None
        s["today_cost"] = None
        s["total_cost"] = None
        s["groups"] = []
        s["error"] = f"拉取超时（>{ACCOUNT_FETCH_TIMEOUT}s），该站点可能无法访问"
        return s

    # 余额
    if isinstance(balance_r, Exception):
        s["balance"] = None
        em = str(balance_r)
        if "User API Key" in em:
            s["balance_note"] = em
        else:
            s["error"] = em
    else:
        s["balance"] = balance_r.get("balance", 0)
        s["group"] = balance_r.get("group", "")

    # 消耗
    if isinstance(usage_r, Exception):
        s["today_cost"] = None
        s["total_cost"] = None
    else:
        s["today_cost"] = usage_r.get("today_cost", 0)
        s["total_cost"] = usage_r.get("total_cost", 0)

    # 分组倍率
    if isinstance(groups_r, Exception):
        s["groups"] = []
        if "User API Key" not in str(groups_r) and not s.get("error"):
            s["error"] = str(groups_r)
    else:
        for g in groups_r:
            g["raw_ratio"] = g.get("ratio", 1.0)
            g["ratio"] = round(g.get("ratio", 1.0) / rr, 6)
            g["effective"] = True
        s["groups"] = groups_r

    return s


# === 账号管理 ===

@router.get("/accounts")
async def list_accounts():
    """获取所有已添加的账号"""
    accounts = _load_accounts()
    safe_accounts = []
    for acc in accounts:
        safe_accounts.append({
            "id": acc["id"],
            "name": acc["name"],
            "platform": acc["platform"],
            "base_url": acc["base_url"],
            "auth_type": acc["auth_type"],
            "credential_type": acc.get("credential_type", "token"),
            "username": acc.get("username", ""),
            "has_token": bool(acc.get("access_token")),
            "has_upstream_key": bool(_looks_like_api_key(acc.get("upstream_key", "") or "")),
            "recharge_ratio": acc.get("recharge_ratio", 1.0) or 1.0,
        })
    return {"success": True, "data": safe_accounts}


@router.post("/accounts")
async def add_account(account: AccountCreate):
    """添加新账号"""
    accounts = _load_accounts()

    # 清洗 token：去掉用户可能粘贴的 "Bearer " 前缀
    raw_token = (account.access_token or "").strip()
    if raw_token.lower().startswith("bearer "):
        raw_token = raw_token[7:].strip()

    new_account: dict[str, Any] = {
        "id": str(uuid.uuid4())[:8],
        "name": account.name,
        "platform": account.platform,
        "base_url": account.base_url.rstrip("/"),
        "auth_type": account.auth_type,
        "credential_type": account.credential_type or "token",
        "username": account.username or "",
        "password": account.password or "",
        "access_token": raw_token,
        "refresh_token": "",
        "user_id": "",
        "recharge_ratio": account.recharge_ratio or 1.0,
        "upstream_key": (account.upstream_key or "").strip(),
    }

    # Token 方式：即时校验凭据有效性
    if account.auth_type == "token" and raw_token:
        adapter = _get_adapter(new_account)
        try:
            if new_account.get("credential_type") == "user_api_key":
                # User API Key 只能走 /v1/* 端点，用 get_models 验证
                await adapter.get_models()
            else:
                await adapter.get_balance()
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"凭据验证失败: {e}"
            )

    # 如果选择登录方式，尝试登录获取 token
    if account.auth_type == "login" and account.username and account.password:
        adapter = _get_adapter(new_account)
        try:
            # 自动检测并处理 Turnstile
            turnstile_token = ""
            ts_info = {"enabled": False, "site_key": ""}

            if account.platform == "newapi" and isinstance(adapter, NewAPIAdapter):
                ts_info = await adapter.check_turnstile()
            elif account.platform == "sub2api":
                # Sub2API 检测 Turnstile
                import httpx as _httpx
                try:
                    async with new_async_client(15.0) as _c:
                        _r = await _c.get(f"{new_account['base_url']}/api/v1/settings/public")
                        _d = _r.json()
                        _s = _d.get("data", _d)
                        ts_info = {
                            "enabled": bool(_s.get("turnstile_enabled", False)),
                            "site_key": _s.get("turnstile_site_key", ""),
                        }
                except Exception:
                    pass

            if ts_info["enabled"] and ts_info["site_key"]:
                settings = _load_settings()
                cs_key = settings.get("capsolver_api_key", "")
                if not cs_key:
                    raise ValueError(
                        "该站点已开启 Cloudflare Turnstile 人机验证，"
                        "请先在设置中配置 CapSolver API Key，或使用 Token/Cookie 方式添加"
                    )
                try:
                    turnstile_token = await capsolver.solve_turnstile(
                        api_key=cs_key,
                        website_url=new_account["base_url"],
                        website_key=ts_info["site_key"],
                        timeout=60,
                    )
                except capsolver.CapSolverError as e:
                    raise ValueError(f"Turnstile 自动验证失败: {e}")

            token = await adapter.login(
                account.username, account.password, turnstile_token
            )
            new_account["access_token"] = token
            if hasattr(adapter, "refresh_token") and adapter.refresh_token:
                new_account["refresh_token"] = adapter.refresh_token
            if hasattr(adapter, "user_id") and adapter.user_id:
                new_account["user_id"] = adapter.user_id
            # 登录成功后校正 credential_type，避免后续误把 session 当 token
            # 登录成功后校正 credential_type（NewAPI 返回 session cookie；Sub2API 返回 JWT）
            if account.platform == "newapi":
                new_account["credential_type"] = "cookie"
            elif account.platform == "sub2api":
                new_account["credential_type"] = "bearer"
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"登录失败: {str(e)}")

    accounts.append(new_account)
    _save_accounts(accounts)
    _cache_invalidate()  # 新增账号，清全部缓存
    return {"success": True, "data": {"id": new_account["id"]}}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str):
    """删除账号"""
    accounts = _load_accounts()
    accounts = [a for a in accounts if a["id"] != account_id]
    _save_accounts(accounts)
    _cache_invalidate()  # 清除所有缓存
    return {"success": True}


# === 数据查询（带缓存） ===

@router.get("/accounts/{account_id}/balance")
async def get_balance(account_id: str, force: bool = Query(False)):
    """查询账号余额"""
    cache_key = f"balance:{account_id}"
    if not force:
        cached = _cache_get(cache_key, CACHE_TTL_ACCOUNT)
        if cached:
            return {"success": True, "data": cached}

    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    adapter = _get_adapter(account)
    try:
        balance = await adapter.get_balance()
        _cache_set(cache_key, balance)
        return {"success": True, "data": balance}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/accounts/{account_id}/groups")
async def get_groups(account_id: str, force: bool = Query(False)):
    """查询分组信息与倍率"""
    cache_key = f"groups:{account_id}"
    if not force:
        cached = _cache_get(cache_key, CACHE_TTL_ACCOUNT)
        if cached is not None:
            return {"success": True, "data": cached}

    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    adapter = _get_adapter(account)
    try:
        groups = await adapter.get_groups()
        _cache_set(cache_key, groups)
        return {"success": True, "data": groups}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/accounts/{account_id}/models")
async def get_models(account_id: str, force: bool = Query(False)):
    """查询可用模型"""
    cache_key = f"models:{account_id}"
    if not force:
        cached = _cache_get(cache_key, CACHE_TTL_ACCOUNT)
        if cached is not None:
            return {"success": True, "data": cached}

    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    adapter = _get_adapter(account)
    try:
        models = await adapter.get_models()
        _cache_set(cache_key, models)
        return {"success": True, "data": models}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/accounts/{account_id}/usage")
async def get_usage(account_id: str, force: bool = Query(False)):
    """查询消耗统计"""
    cache_key = f"usage:{account_id}"
    if not force:
        cached = _cache_get(cache_key, CACHE_TTL_ACCOUNT)
        if cached:
            return {"success": True, "data": cached}

    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    adapter = _get_adapter(account)
    try:
        usage = await adapter.get_usage()
        _cache_set(cache_key, usage)
        return {"success": True, "data": usage}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/accounts/{account_id}/overview")
async def get_overview(account_id: str, force: bool = Query(False)):
    """获取账号完整概览（余额 + 分组 + 消耗）"""
    cache_key = f"overview:{account_id}"
    if not force:
        cached = _cache_get(cache_key, CACHE_TTL_ACCOUNT)
        if cached:
            return {"success": True, "data": cached}
    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    adapter = _get_adapter(account)
    recharge_ratio = account.get("recharge_ratio", 1.0) or 1.0
    result: dict[str, Any] = {
        "account_id": account_id,
        "name": account["name"],
        "platform": account["platform"],
        "recharge_ratio": recharge_ratio,
    }

    # 余额/分组/消耗 并发拉取，带整体超时
    try:
        balance_r, groups_r, usage_r = await asyncio.wait_for(
            asyncio.gather(
                adapter.get_balance(),
                adapter.get_groups(),
                adapter.get_usage(),
                return_exceptions=True,
            ),
            timeout=ACCOUNT_FETCH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        err = f"拉取超时（>{ACCOUNT_FETCH_TIMEOUT}s），该站点可能无法访问"
        result["balance"] = {"error": err}
        result["groups"] = {"error": err}
        result["usage"] = {"error": err}
        return {"success": True, "data": result}

    if isinstance(balance_r, Exception):
        result["balance"] = {"error": str(balance_r)}
    else:
        result["balance"] = balance_r

    if isinstance(groups_r, Exception):
        result["groups"] = {"error": str(groups_r)}
    else:
        for g in groups_r:
            g["raw_ratio"] = g.get("ratio", 1.0)
            g["ratio"] = round(g.get("ratio", 1.0) / recharge_ratio, 6)
            g["effective"] = True
        result["groups"] = groups_r

    if isinstance(usage_r, Exception):
        result["usage"] = {"error": str(usage_r)}
    else:
        result["usage"] = usage_r

    _cache_set(cache_key, result)
    return {"success": True, "data": result}


# === 操作 ===

@router.post("/accounts/{account_id}/refresh")
async def refresh_token(account_id: str):
    """重新登录刷新 token"""
    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    if account["auth_type"] != "login" or not account.get("username"):
        raise HTTPException(status_code=400, detail="该账号不支持刷新（非登录类型）")

    adapter = _get_adapter(account)
    try:
        token = await adapter.login(account["username"], account["password"])
        for acc in accounts:
            if acc["id"] == account_id:
                acc["access_token"] = token
                if hasattr(adapter, "refresh_token") and adapter.refresh_token:
                    acc["refresh_token"] = adapter.refresh_token
                if hasattr(adapter, "user_id") and adapter.user_id:
                    acc["user_id"] = adapter.user_id
                # 校正凭据类型
                if acc.get("platform") == "newapi":
                    acc["credential_type"] = "cookie"
                elif acc.get("platform") == "sub2api":
                    acc["credential_type"] = "bearer"
                break
        _save_accounts(accounts)
        _cache_invalidate(f"balance:{account_id}")
        _cache_invalidate(f"groups:{account_id}")
        _cache_invalidate(f"usage:{account_id}")
        _cache_invalidate(f"overview:{account_id}")
        _cache_invalidate("dashboard")
        return {"success": True, "message": "Token 刷新成功"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/accounts/{account_id}/redeem")
async def redeem(account_id: str, req: RedeemRequest):
    """兑换码兑换"""
    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    adapter = _get_adapter(account)
    try:
        result = await adapter.redeem_code(req.code)
        return {"success": True, "data": result}
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/check-turnstile")
async def check_turnstile(req: dict):
    """检查站点是否开启了 Turnstile 人机验证"""
    base_url = req.get("base_url", "").rstrip("/")
    platform = req.get("platform", "newapi")
    if not base_url:
        raise HTTPException(status_code=400, detail="请提供站点地址")

    if platform == "newapi":
        adapter = NewAPIAdapter(base_url)
        result = await adapter.check_turnstile()
        return {"success": True, "data": result}
    else:
        # Sub2API 也可能有 Turnstile
        import httpx
        try:
            async with new_async_client(15.0) as client:
                resp = await client.get(f"{base_url}/api/v1/settings/public")
                data = resp.json()
                settings = data.get("data", data)
                return {"success": True, "data": {
                    "enabled": bool(settings.get("turnstile_enabled", False)),
                    "site_key": settings.get("turnstile_site_key", ""),
                }}
        except Exception:
            return {"success": True, "data": {"enabled": False, "site_key": ""}}


# === 全局汇总 ===

async def _build_dashboard(force_snapshot: bool = False) -> dict[str, Any]:
    """实际拉取所有渠道并组装仪表盘数据（并发 + 单渠道超时）。"""
    global _refreshing
    accounts = _load_accounts()
    total_balance = 0.0
    today_cost = 0.0
    total_cost = 0.0
    error_count = 0

    account_summaries = list(await asyncio.gather(
        *(_account_summary(account) for account in accounts)
    ))

    for s in account_summaries:
        # 注意：余额/消耗为 0 是合法值，不能用 truthy 判断
        if s.get("balance") is not None:
            try:
                total_balance += float(s["balance"] or 0)
            except (TypeError, ValueError):
                pass
        if s.get("today_cost") is not None:
            try:
                today_cost += float(s["today_cost"] or 0)
            except (TypeError, ValueError):
                pass
        if s.get("total_cost") is not None:
            try:
                total_cost += float(s["total_cost"] or 0)
            except (TypeError, ValueError):
                pass
        if s.get("error") and "User API Key" not in str(s.get("error", "")):
            error_count += 1

    result = {
        "total_balance": round(total_balance, 4),
        "today_cost": round(today_cost, 4),
        "total_cost": round(total_cost, 4),
        "account_count": len(accounts),
        "error_count": error_count,
        "accounts": account_summaries,
        "cached_at": int(time.time()),
        "from_cache": False,
        "empty": False,
        "refreshing": False,
    }
    _cache_set("dashboard", result)
    if force_snapshot:
        _save_snapshot_to_history()
    return result


@router.get("/dashboard")
async def dashboard(force: bool = Query(False, description="强制刷新，忽略缓存")):
    """仪表盘汇总 - 打开页面秒出缓存，点强制刷新才拉上游。

    - 无 force：优先返回内存/磁盘缓存（毫秒级）
    - force=true：并发拉取所有渠道（单渠道 12s 超时上限）
    """
    global _refreshing
    cache_key = "dashboard"

    if not force:
        cached = _cache.get(cache_key, {}).get("data")
        if cached and cached.get("accounts") is not None and not cached.get("empty"):
            out = dict(cached)
            out["from_cache"] = True
            out["refreshing"] = _refreshing
            return {"success": True, "data": out}
        # 无缓存：返回空壳（前端会触发后台 force 刷新），绝不阻塞
        return {"success": True, "data": {
            "total_balance": 0, "today_cost": 0, "total_cost": 0,
            "account_count": len(_load_accounts()), "error_count": 0,
            "accounts": [], "cached_at": 0, "from_cache": False,
            "empty": True, "refreshing": _refreshing,
        }}

    # force=true：加锁避免多个 force 同时打满上游
    if _refresh_lock.locked():
        # 已有刷新在跑：立刻返回现有缓存（或空壳）+ refreshing 标记
        cached = _cache.get(cache_key, {}).get("data") or {
            "total_balance": 0, "today_cost": 0, "total_cost": 0,
            "account_count": len(_load_accounts()), "error_count": 0,
            "accounts": [], "cached_at": 0, "empty": True,
        }
        out = dict(cached)
        out["from_cache"] = True
        out["refreshing"] = True
        return {"success": True, "data": out}

    async with _refresh_lock:
        _refreshing = True
        try:
            result = await _build_dashboard(force_snapshot=True)
            return {"success": True, "data": result}
        finally:
            _refreshing = False


@router.get("/dashboard/status")
async def dashboard_status():
    """轻量状态：前端轮询是否还在后台刷新"""
    cached = _cache.get("dashboard", {}).get("data")
    return {
        "success": True,
        "data": {
            "refreshing": _refreshing,
            "has_cache": bool(cached and cached.get("accounts")),
            "cached_at": (cached or {}).get("cached_at", 0),
        },
    }


# === 设置 ===

@router.get("/settings")
async def get_settings():
    """获取系统设置"""
    settings = _load_settings()
    cs_key = settings.get("capsolver_api_key", "")
    masked = ""
    if cs_key:
        masked = cs_key[:6] + "****" + cs_key[-4:] if len(cs_key) > 10 else "****"
    hub_key = settings.get("hub_api_key", "")
    hub_key_masked = ""
    if hub_key:
        hub_key_masked = hub_key[:4] + "****" + hub_key[-4:] if len(hub_key) > 8 else "****"
    hub_ready = bool(
        settings.get("hub_base_url")
        and (settings.get("hub_api_key") or (settings.get("hub_email") and settings.get("hub_password")))
    )
    return {
        "success": True,
        "data": {
            "capsolver_api_key_masked": masked,
            "capsolver_configured": bool(cs_key),
            "hub_base_url": settings.get("hub_base_url", ""),
            "hub_email": settings.get("hub_email", ""),
            "hub_configured": hub_ready,
            "hub_password_set": bool(settings.get("hub_password")),
            "hub_api_key_set": bool(hub_key),
            "hub_api_key_masked": hub_key_masked,
            "ultra_low_rate": settings.get("ultra_low_rate", 0.6),
        },
    }


@router.post("/settings")
async def update_settings(req: SettingsUpdate):
    """更新系统设置"""
    settings = _load_settings()
    if req.capsolver_api_key is not None:
        settings["capsolver_api_key"] = req.capsolver_api_key
    if req.hub_base_url is not None:
        settings["hub_base_url"] = req.hub_base_url.rstrip("/") if req.hub_base_url else ""
    if req.hub_email is not None:
        settings["hub_email"] = req.hub_email
    if req.hub_password is not None and req.hub_password != "":
        settings["hub_password"] = req.hub_password
    if req.hub_api_key is not None and req.hub_api_key != "":
        settings["hub_api_key"] = req.hub_api_key
    if req.ultra_low_rate is not None:
        settings["ultra_low_rate"] = req.ultra_low_rate
    _save_settings(settings)
    return {"success": True, "message": "设置已保存"}


@router.get("/settings/capsolver-balance")
async def capsolver_balance():
    """查询 CapSolver 账户余额"""
    settings = _load_settings()
    cs_key = settings.get("capsolver_api_key", "")
    if not cs_key:
        raise HTTPException(status_code=400, detail="未配置 CapSolver API Key")
    try:
        balance = await capsolver.get_balance(cs_key)
        return {"success": True, "data": {"balance": balance}}
    except capsolver.CapSolverError as e:
        raise HTTPException(status_code=400, detail=str(e))


# === 倍率历史 ===

RATIO_HISTORY_FILE = DATA_DIR / "ratio_history.json"
MAX_HISTORY_SNAPSHOTS = 200


def _save_snapshot_to_history():
    """将当前 dashboard 缓存的 groups 数据保存为一份历史快照"""
    cached = _cache.get("dashboard", {}).get("data")
    if not cached:
        return
    accounts = cached.get("accounts", [])
    if not accounts:
        return
    now_ts = int(time.time())
    snap = {
        "ts": now_ts,
        "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(now_ts)),
        "accounts": [],
    }
    for acc in accounts:
        entry = {
            "id": acc.get("id"),
            "name": acc.get("name"),
            "platform": acc.get("platform"),
            "base_url": acc.get("base_url"),
            "balance": acc.get("balance"),
            "recharge_ratio": acc.get("recharge_ratio", 1.0),
            "groups": [],
        }
        for g in acc.get("groups", []) or []:
            entry["groups"].append({
                "name": g.get("name", ""),
                "ratio": g.get("ratio", None),
                "raw_ratio": g.get("raw_ratio", g.get("ratio")),
            })
        snap["accounts"].append(entry)

    history = _load_ratio_history()
    history.append(snap)
    if len(history) >= 2 and history[-2]["ts"] == history[-1]["ts"]:
        history[-2] = history[-1]
        history.pop()
    if len(history) > MAX_HISTORY_SNAPSHOTS:
        history = history[-MAX_HISTORY_SNAPSHOTS:]
    RATIO_HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_ratio_history() -> list[dict]:
    if not RATIO_HISTORY_FILE.exists():
        return []
    try:
        return json.loads(RATIO_HISTORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


@router.get("/accounts/{account_id}/ratio_history")
async def get_ratio_history(account_id: str):
    """获取某个渠道的历史倍率变化（所有分组），仅返回最近 10 次快照"""
    history = _load_ratio_history()
    # 只取最近 10 次
    recent = history[-10:]
    result: dict[str, list] = {}
    for snap in recent:
        for acc in snap.get("accounts", []):
            if acc.get("id") != account_id:
                continue
            for g in acc.get("groups", []):
                name = g.get("name", "")
                ratio = g.get("ratio")
                if ratio is None:
                    continue
                if name not in result:
                    result[name] = []
                result[name].append({"time": snap.get("time", ""), "ratio": ratio})
    return {"success": True, "data": result}


# === Sub2API Hub 配置 ===


async def _get_hub_admin():
    """根据设置构建并鉴权 Sub2APIAdmin；优先 x-api-key，其次邮箱密码。"""
    settings = _load_settings()
    base_url = settings.get("hub_base_url", "")
    if not base_url:
        raise HTTPException(status_code=400, detail="请先配置 Hub 地址")
    from services.sub2api_admin import Sub2APIAdmin
    hub_key = (settings.get("hub_api_key") or "").strip()
    email = settings.get("hub_email", "")
    password = settings.get("hub_password", "")
    if hub_key:
        return Sub2APIAdmin(base_url, api_key=hub_key)
    if email and password:
        admin = Sub2APIAdmin(base_url)
        await admin.login(email, password)
        return admin
    raise HTTPException(status_code=400, detail="请配置 Hub Admin API Key，或管理员邮箱+密码")


@router.post("/hub/test")
async def test_hub():
    """测试 Sub2API Hub 连接"""
    try:
        admin = await _get_hub_admin()
        groups = await admin.list_groups()
        return {
            "success": True,
            "data": {
                "groups_count": len(groups),
                "groups": [{"id": g.get("id"), "name": g.get("name"),
                            "platform": g.get("platform")} for g in groups[:20]],
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# === 扫描 + 自动配置 ===

# 配置映射存储：记录上游渠道 → Sub2API Hub 的对应关系
MAPPINGS_FILE = DATA_DIR / "provision_mappings.json"


def _load_mappings() -> list[dict]:
    if not MAPPINGS_FILE.exists():
        return []
    try:
        return json.loads(MAPPINGS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_mappings(mappings: list[dict]):
    MAPPINGS_FILE.write_text(
        json.dumps(mappings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _find_mapping(account_id: str, group_name: str) -> dict | None:
    """查找已有的配置映射"""
    for m in _load_mappings():
        if m["upstream_account_id"] == account_id and m["upstream_group_name"] == group_name:
            return m
    return None


@router.get("/scanner/mappings")
async def list_mappings():
    """列出所有已配置的上游→Hub 映射"""
    return {"success": True, "data": _load_mappings()}


@router.delete("/scanner/mappings/{mapping_id}")
async def delete_mapping(mapping_id: str):
    """删除一条配置映射（不会删除 Hub 上的账号/分组）"""
    mappings = _load_mappings()
    mappings = [m for m in mappings if m.get("id") != mapping_id]
    _save_mappings(mappings)
    return {"success": True, "message": "映射已删除"}


@router.post("/scanner/scan")
async def scan_low_price():
    """扫描所有渠道，找出低于阈值的分组，并标注映射状态"""
    settings = _load_settings()
    threshold = settings.get("ultra_low_rate", 0.6)
    mappings = _load_mappings()

    cached = _cache.get("dashboard", {}).get("data")
    if not cached:
        await dashboard()
        cached = _cache.get("dashboard", {}).get("data")
        if not cached:
            raise HTTPException(status_code=400, detail="暂无数据，请先刷新仪表盘")

    accounts = cached.get("accounts", [])
    candidates = []
    for acc in accounts:
        acc_info = {
            "id": acc.get("id"),
            "name": acc.get("name"),
            "platform": acc.get("platform"),
            "base_url": acc.get("base_url"),
            "recharge_ratio": acc.get("recharge_ratio", 1.0),
        }
        stored_acc = next((a for a in _load_accounts() if a["id"] == acc["id"]), {})
        for g in (acc.get("groups") or []):
            ratio = g.get("ratio")
            if ratio is not None and ratio < threshold:
                gname = g.get("name", "")
                existing = _find_mapping(acc["id"], gname)
                usable_key = bool(_resolve_upstream_key(stored_acc, gname))
                candidates.append({
                    **acc_info,
                    "has_upstream_key": usable_key,
                    "group_name": gname,
                    "ratio": ratio,
                    "raw_ratio": g.get("raw_ratio", ratio),
                    "model_names": g.get("models", []),
                    "mapped": bool(existing),
                    "mapping_id": existing.get("id") if existing else None,
                    "hub_group_id": existing.get("hub_group_id") if existing else None,
                    "hub_account_id": existing.get("hub_account_id") if existing else None,
                    "last_ratio": existing.get("last_ratio") if existing else None,
                    "last_sync": existing.get("last_sync") if existing else None,
                    "ratio_changed": bool(
                        existing and abs(float(existing.get("last_ratio") or 0) - float(ratio)) > 0.0001
                    ),
                })

    candidates.sort(key=lambda x: x["ratio"])
    # 计算每个 (account, category) 的最优倍率
    best_in_category: dict[str, float] = {}
    for c in candidates:
        cat = _classify_group_category(c["group_name"])
        key = f"{c['id']}/{cat}"
        if key not in best_in_category or c["ratio"] < best_in_category[key]:
            best_in_category[key] = c["ratio"]
    # 标注：是否比已映射的同类别更低（升级机会）
    for c in candidates:
        cat = _classify_group_category(c["group_name"])
        key = f"{c['id']}/{cat}"
        c["is_best_in_category"] = (c["ratio"] == best_in_category[key])
        if c["mapped"]:
            c["beats_existing"] = False
        else:
            # 找同 account + category 的已映射最低倍率
            mapped_best = 999.0
            for m in mappings:
                if (m["upstream_account_id"] == c["id"]
                        and _classify_group_category(m["upstream_group_name"]) == cat):
                    if m["last_ratio"] < mapped_best:
                        mapped_best = m["last_ratio"]
            c["beats_existing"] = c["ratio"] < mapped_best
            c["existing_best_ratio"] = mapped_best if mapped_best < 999 else None
    return {"success": True, "data": {"threshold": threshold, "candidates": candidates}}


@router.post("/scanner/provision")
async def provision_account(req: dict):
    """将一个低价分组配置到 Sub2API Hub。

    body:
      account_id, group_name, ratio
      force: true 时即使已映射也会重新创建（追加新账号，更新本地映射）
    """
    account_id = req.get("account_id")
    group_name = req.get("group_name")
    ratio = req.get("ratio", 1.0)
    force = bool(req.get("force"))

    if not account_id or not group_name:
        raise HTTPException(status_code=400, detail="缺少 account_id 或 group_name")

    existing = _find_mapping(account_id, group_name)
    if existing and not force:
        return {
            "success": True,
            "data": {"status": "skipped", "reason": "已配置，无需重复创建",
                     "mapping": existing},
        }

    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="渠道不存在")

    try:
        upstream_key = _require_upstream_key(account, group_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    platform = _group_to_platform(group_name, account.get("platform"))
    try:
        admin = await _get_hub_admin()
        hub_gn = _hub_group_name(group_name)
        cat = _classify_group_category(group_name)
        result = await admin.provision_upstream(
            account_name=f"{account['name']}-{group_name}",
            base_url=account["base_url"],
            api_key=upstream_key,
            platform=platform,
            group_name=hub_gn,
            group_rate=float(ratio),
        )
        now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        mapping = {
            "id": existing["id"] if existing and force else str(uuid.uuid4())[:8],
            "upstream_account_id": account_id,
            "upstream_account_name": account["name"],
            "upstream_group_name": group_name,
            "category": cat,
            "upstream_base_url": account["base_url"],
            "platform": platform,
            "hub_group_id": result["group"]["id"],
            "hub_group_name": result["group"]["name"],
            "hub_account_id": result["account"]["id"],
            "hub_account_name": result["account"]["name"],
            "last_ratio": float(ratio),
            "last_sync": now,
            "created_at": (existing or {}).get("created_at") or now,
        }
        mappings = _load_mappings()
        if existing and force:
            mappings = [m for m in mappings if m.get("id") != existing.get("id")]
        mappings.append(mapping)
        _save_mappings(mappings)
        result["mapping"] = mapping
        result["status"] = "recreated" if force and existing else "created"
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/scanner/sync-ratio")
async def sync_ratio(mapping_id: str):
    """同步一条映射的倍率：如果上游倍率变了，更新 Hub 对应分组的 rate_multiplier"""
    mappings = _load_mappings()
    mapping = next((m for m in mappings if m.get("id") == mapping_id), None)
    if not mapping:
        raise HTTPException(status_code=404, detail="映射不存在")

    cached = _cache.get("dashboard", {}).get("data")
    if not cached:
        raise HTTPException(status_code=400, detail="暂无数据，请先刷新仪表盘")

    current_ratio = None
    for acc in cached.get("accounts", []):
        if acc["id"] != mapping["upstream_account_id"]:
            continue
        for g in (acc.get("groups") or []):
            if g.get("name") == mapping["upstream_group_name"]:
                current_ratio = g.get("ratio")
                break

    if current_ratio is None:
        raise HTTPException(status_code=400, detail="未找到该分组的当前倍率，可能已被删除")

    if abs(float(current_ratio) - float(mapping.get("last_ratio") or 0)) < 0.0001:
        return {"success": True, "data": {"status": "unchanged", "ratio": current_ratio}}

    try:
        admin = await _get_hub_admin()
        await admin.update_group(mapping["hub_group_id"], rate_multiplier=float(current_ratio))
        old_ratio = mapping["last_ratio"]
        mapping["last_ratio"] = float(current_ratio)
        mapping["last_sync"] = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        _save_mappings(mappings)
        return {
            "success": True,
            "data": {
                "status": "updated",
                "old_ratio": old_ratio,
                "new_ratio": current_ratio,
                "hub_group_id": mapping["hub_group_id"],
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/scanner/auto-sync")
async def auto_sync():
    """一键全自动同步：扫描→新建→倍率更新，返回操作摘要"""
    settings = _load_settings()
    threshold = settings.get("ultra_low_rate", 0.6)

    log: list[dict] = []
    new_count = 0
    update_count = 0
    skip_count = 0
    error_count = 0
    no_key_count = 0

    # 确保有最新数据
    await dashboard(force=True)
    cached = _cache.get("dashboard", {}).get("data")
    if not cached:
        raise HTTPException(status_code=400, detail="暂无数据")

    accounts_data = cached.get("accounts", [])
    stored_accounts = _load_accounts()

    # 整次同步共用一份 mappings，结束时统一落盘（避免倍率更新丢失）
    mappings = _load_mappings()

    def find_in_mappings(account_id: str, group_name: str) -> dict | None:
        for m in mappings:
            if m.get("upstream_account_id") == account_id and m.get("upstream_group_name") == group_name:
                return m
        return None

    need_hub = True
    admin = None
    try:
        admin = await _get_hub_admin()
    except HTTPException as e:
        need_hub = False
        log.append({"action": "error", "group": "*", "error": f"Hub 未就绪: {e.detail}"})

    for acc in accounts_data:
        stored = next((a for a in stored_accounts if a["id"] == acc["id"]), {})

        for g in (acc.get("groups") or []):
            ratio = g.get("ratio")
            if ratio is None or ratio >= threshold:
                continue

            group_name = g.get("name", "")
            try:
                upstream_key = _require_upstream_key(stored, group_name)
            except ValueError as e:
                no_key_count += 1
                log.append({
                    "action": "skipped",
                    "group": group_name,
                    "reason": str(e),
                })
                continue

            existing = find_in_mappings(acc["id"], group_name)

            if existing:
                if abs(float(existing.get("last_ratio") or 0) - float(ratio)) > 0.0001 and need_hub:
                    try:
                        await admin.update_group(
                            existing["hub_group_id"], rate_multiplier=float(ratio)
                        )
                        old_r = existing["last_ratio"]
                        existing["last_ratio"] = float(ratio)
                        existing["last_sync"] = time.strftime(
                            "%Y-%m-%d %H:%M", time.localtime()
                        )
                        log.append({
                            "action": "update_ratio",
                            "group": group_name,
                            "from": old_r,
                            "to": ratio,
                        })
                        update_count += 1
                    except Exception as e:
                        log.append({
                            "action": "error",
                            "group": group_name,
                            "error": str(e),
                        })
                        error_count += 1
                else:
                    skip_count += 1
            else:
                cat = _classify_group_category(group_name)
                mapped_best = 999.0
                for m in mappings:
                    if (m.get("upstream_account_id") == acc["id"]
                            and _classify_group_category(m.get("upstream_group_name", "")) == cat):
                        try:
                            if float(m.get("last_ratio", 999)) < mapped_best:
                                mapped_best = float(m["last_ratio"])
                        except (TypeError, ValueError):
                            pass
                if mapped_best < 999 and float(ratio) >= mapped_best:
                    log.append({
                        "action": "skipped",
                        "group": group_name,
                        "reason": f"{ratio}x >= 同分类已映射最优 {mapped_best}x",
                    })
                    skip_count += 1
                    continue

                if not need_hub:
                    log.append({
                        "action": "skipped",
                        "group": group_name,
                        "reason": "Hub 未配置",
                    })
                    skip_count += 1
                    continue

                try:
                    platform = _group_to_platform(group_name, acc.get("platform", "newapi"))
                    hub_gn = _hub_group_name(group_name)
                    result = await admin.provision_upstream(
                        account_name=f"{acc['name']}-{group_name}",
                        base_url=acc["base_url"],
                        api_key=upstream_key,
                        platform=platform,
                        group_name=hub_gn,
                        group_rate=float(ratio),
                    )
                    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
                    mapping = {
                        "id": str(uuid.uuid4())[:8],
                        "upstream_account_id": acc["id"],
                        "upstream_account_name": acc["name"],
                        "upstream_group_name": group_name,
                        "category": cat,
                        "upstream_base_url": acc["base_url"],
                        "platform": platform,
                        "hub_group_id": result["group"]["id"],
                        "hub_group_name": result["group"]["name"],
                        "hub_account_id": result["account"]["id"],
                        "hub_account_name": result["account"]["name"],
                        "last_ratio": float(ratio),
                        "last_sync": now,
                        "created_at": now,
                    }
                    mappings.append(mapping)
                    log.append({
                        "action": "created",
                        "group": group_name,
                        "ratio": ratio,
                        "hub_account": result["account"]["name"],
                    })
                    new_count += 1
                except Exception as e:
                    log.append({
                        "action": "error",
                        "group": group_name,
                        "error": str(e),
                    })
                    error_count += 1

    _save_mappings(mappings)
    return {
        "success": True,
        "data": {
            "summary": {
                "new": new_count,
                "updated": update_count,
                "skipped": skip_count,
                "errors": error_count,
                "no_key": no_key_count,
            },
            "log": log,
        },
    }


# === 分组级 API Key 管理 ===

GROUP_KEYS_FILE = DATA_DIR / "group_keys.json"


def _load_group_keys() -> dict[str, str]:
    """加载分组密钥映射 { 'account_id/group_name': 'sk-xxx' }"""
    if not GROUP_KEYS_FILE.exists():
        return {}
    try:
        return json.loads(GROUP_KEYS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_group_keys(keys: dict[str, str]):
    GROUP_KEYS_FILE.write_text(
        json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _get_group_key(account_id: str, group_name: str) -> str:
    """获取分组级 Key，没有则返回空"""
    return _load_group_keys().get(f"{account_id}/{group_name}", "")


@router.get("/group-keys")
async def list_group_keys():
    """列出所有分组密钥"""
    keys = _load_group_keys()
    result = []
    for k, v in keys.items():
        parts = k.split("/", 1)
        result.append({
            "account_id": parts[0],
            "group_name": parts[1] if len(parts) > 1 else "",
            "api_key": v[:12] + "****" + v[-4:] if len(v) > 16 else "****",
            "has_key": True,
        })
    return {"success": True, "data": result}


@router.post("/group-keys")
async def set_group_key(req: dict):
    """设置一个分组的 API Key"""
    account_id = req.get("account_id", "")
    group_name = req.get("group_name", "")
    api_key = req.get("api_key", "").strip()
    if not account_id or not group_name:
        raise HTTPException(status_code=400, detail="缺少 account_id 或 group_name")
    if not api_key:
        raise HTTPException(status_code=400, detail="缺少 api_key")
    keys = _load_group_keys()
    keys[f"{account_id}/{group_name}"] = api_key
    _save_group_keys(keys)
    return {"success": True, "message": "分组密钥已保存"}


@router.delete("/group-keys/{account_id}/{group_name:path}")
async def delete_group_key(account_id: str, group_name: str):
    """删除一个分组的 API Key（group_name 支持含 / 的路径）"""
    keys = _load_group_keys()
    key = f"{account_id}/{group_name}"
    if key in keys:
        del keys[key]
        _save_group_keys(keys)
    return {"success": True, "message": "分组密钥已删除"}


def _looks_like_api_key(key: str) -> bool:
    """判断是否可作为上游 OpenAI-compatible API Key（拒绝 session/JWT/cookie）。"""
    if not key:
        return False
    k = key.strip()
    if len(k) < 8:
        return False
    low = k.lower()
    if low.startswith("session="):
        return False
    if "session=" in low and ("=" in k and (";" in k or k.count("=") >= 1)):
        # cookie 串
        if not low.startswith("sk-"):
            return False
    if k.startswith("eyJ"):  # JWT
        return False
    if low.startswith("rt_"):  # refresh token
        return False
    if low.startswith("bearer "):
        return False
    # 明确接受 sk- / 较长 token；其余非 cookie/jwt 也允许（部分站自定义前缀）
    if low.startswith("sk-"):
        return True
    # 纯字母数字下划线短横，长度足够，且不含空白与 cookie 分隔
    if any(ch.isspace() for ch in k):
        return False
    if "=" in k and ";" in k:
        return False
    return len(k) >= 16


def _resolve_upstream_key(account: dict, group_name: str) -> str:
    """解析上游 Key：分组级 > 账号级 upstream_key > 可用的 access_token(sk-)。

    绝不会把 session cookie / JWT 当作上游 Key。
    """
    if not account:
        return ""
    gk = _get_group_key(account.get("id", ""), group_name)
    if _looks_like_api_key(gk):
        return gk.strip()
    uk = (account.get("upstream_key") or "").strip()
    if _looks_like_api_key(uk):
        return uk
    # access_token 仅当它本身就是 API Key 时可用（如 user_api_key）
    at = (account.get("access_token") or "").strip()
    if account.get("credential_type") in ("user_api_key", "bearer", "token") and _looks_like_api_key(at):
        return at
    if _looks_like_api_key(at) and at.lower().startswith("sk-"):
        return at
    return ""


def _require_upstream_key(account: dict, group_name: str) -> str:
    """解析上游 Key；不可用时抛出可读错误。"""
    key = _resolve_upstream_key(account, group_name)
    if key:
        return key
    name = account.get("name") or account.get("id") or "渠道"
    raise ValueError(
        f"「{name} / {group_name}」缺少可用的上游 API Key（sk-…）。"
        f"Cookie/JWT 不能用于 Sub2API 上游调用，请在扫描器设置分组密钥，或在渠道填写上游 Key。"
    )


_PLATFORM_KEYWORDS: list[tuple[str, str]] = [
    ("claude", "anthropic"), ("anthropic", "anthropic"),
    ("sonnet", "anthropic"), ("opus", "anthropic"), ("haiku", "anthropic"),
    ("kiro", "anthropic"), ("krio", "anthropic"),
    ("gpt", "openai"), ("openai", "openai"), ("o1", "openai"),
    ("o3", "openai"), ("o4", "openai"), ("chatgpt", "openai"),
    ("codex", "openai"),
    ("gemini", "gemini"), ("google", "gemini"),
    ("grok", "grok"), ("xai", "grok"),
    ("antigravity", "antigravity"), ("反重力", "antigravity"),
]


def _group_to_platform(group_name: str, account_platform: str) -> str:
    n = (group_name or "").lower()
    for kw, plat in _PLATFORM_KEYWORDS:
        if kw in n:
            return plat
    return "anthropic" if account_platform == "newapi" else "openai"


# 模型分类关键词（与前端 classifyGroup 保持一致）
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    # Claude-Kiro must be checked BEFORE Claude-官号
    "Claude-Kiro": ["kiro", "krio"],
    "Claude-官号": ["claude", "anthropic", "sonnet", "opus", "haiku", "max",
                   "antigravity", "反重力", "aws", "bedrock"],
    "GPT": ["gpt", "openai", "o1", "o3", "o4", "chatgpt", "codex", "黑冲", "plus", "team", "正价pro", "低价"],
    "Gemini": ["gemini", "google", "bard", "palm"],
    "Grok": ["grok", "xai"],
}


def _classify_group_category(group_name: str) -> str:
    """将分组名归类到模型厂商"""
    n = (group_name or "").lower()
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in n:
                return cat
    return "其他"


def _hub_group_name(group_name: str) -> str:
    """生成 Hub 中的分组名，按模型类型区分"""
    return f"超低价-{_classify_group_category(group_name)}"


class UpstreamKeyUpdate(BaseModel):
    upstream_key: str | None = None


class UserIdUpdate(BaseModel):
    user_id: str | None = None


@router.patch("/accounts/{account_id}/upstream_key")
async def update_upstream_key(account_id: str, req: UpstreamKeyUpdate):
    """更新渠道的上游 API Key（用于 Sub2API 自动配置）"""
    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    account["upstream_key"] = req.upstream_key
    _save_accounts(accounts)
    return {"success": True, "message": "上游 Key 已更新"}


@router.patch("/accounts/{account_id}/user_id")
async def update_user_id(account_id: str, req: UserIdUpdate):
    """更新渠道的 User ID（Cookie 模式需要，用于 New-Api-User 头）"""
    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    account["user_id"] = req.user_id or ""
    _save_accounts(accounts)
    _cache_invalidate()
    return {"success": True, "message": "User ID 已更新"}


# === 连通性探活 ===

PROBE_PROFILES_FILE = DATA_DIR / "probe_profiles.json"


class ProbeProfileCreate(BaseModel):
    name: str = ""
    account_id: str
    group_name: str = ""
    model: str
    prompt: str = "ping"
    max_tokens: int = 8
    timeout: int = 30
    enabled: bool = True


class ProbeProfileUpdate(BaseModel):
    name: str | None = None
    account_id: str | None = None
    group_name: str | None = None
    model: str | None = None
    prompt: str | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    enabled: bool | None = None


class ProbeRunRequest(BaseModel):
    """即时探活（可不保存为配置）"""
    account_id: str
    group_name: str = ""
    model: str
    prompt: str = "ping"
    max_tokens: int = 8
    timeout: int = 30


def _load_probe_profiles() -> list[dict]:
    if not PROBE_PROFILES_FILE.exists():
        return []
    try:
        data = json.loads(PROBE_PROFILES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("profiles") or []
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _save_probe_profiles(profiles: list[dict]):
    PROBE_PROFILES_FILE.write_text(
        json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def _run_probe_for_account(
    account: dict,
    *,
    group_name: str,
    model: str,
    prompt: str = "ping",
    max_tokens: int = 8,
    timeout: int = 30,
) -> dict[str, Any]:
    from services.probe import probe_chat_completion

    base_meta = {
        "account_id": account.get("id"),
        "account_name": account.get("name"),
        "base_url": account.get("base_url"),
        "tested_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "group_name": group_name or "",
        "model": model or "",
    }
    try:
        api_key = _require_upstream_key(account, group_name or "")
    except ValueError as e:
        return {
            "ok": False,
            "status": "auth",
            "latency_ms": 0,
            "http_status": None,
            "reply": "",
            "error": str(e),
            "usage": {},
            **base_meta,
        }

    try:
        result = await probe_chat_completion(
            base_url=account.get("base_url", ""),
            api_key=api_key,
            model=model,
            group_name=group_name or "",
            prompt=prompt or "ping",
            max_tokens=max_tokens or 8,
            timeout=float(timeout or 30),
        )
    except UnicodeEncodeError as e:
        return {
            "ok": False,
            "status": "error",
            "latency_ms": 0,
            "http_status": None,
            "reply": "",
            "error": f"编码错误: {e}。请重启服务（python main.py）后再试。",
            "usage": {},
            **base_meta,
        }
    except Exception as e:
        return {
            "ok": False,
            "status": "error",
            "latency_ms": 0,
            "http_status": None,
            "reply": "",
            "error": str(e)[:500],
            "usage": {},
            **base_meta,
        }

    result["account_id"] = base_meta["account_id"]
    result["account_name"] = base_meta["account_name"]
    result["base_url"] = base_meta["base_url"]
    result["tested_at"] = base_meta["tested_at"]
    return result


@router.get("/probe/profiles")
async def list_probe_profiles():
    """列出用户配置的探活目标（渠道+分组+模型）"""
    profiles = _load_probe_profiles()
    accounts = {a["id"]: a for a in _load_accounts()}
    for p in profiles:
        acc = accounts.get(p.get("account_id") or "")
        p["account_name"] = (acc or {}).get("name", "")
        p["base_url"] = (acc or {}).get("base_url", "")
        p["has_key"] = bool(acc and _resolve_upstream_key(acc, p.get("group_name") or ""))
    return {"success": True, "data": profiles}


@router.post("/probe/profiles")
async def create_probe_profile(req: ProbeProfileCreate):
    """新增探活配置"""
    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == req.account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="渠道不存在")
    if not (req.model or "").strip():
        raise HTTPException(status_code=400, detail="请填写模型名")

    profile = {
        "id": str(uuid.uuid4())[:8],
        "name": (req.name or "").strip() or f"{account['name']}-{req.group_name or 'default'}-{req.model}",
        "account_id": req.account_id,
        "group_name": (req.group_name or "").strip(),
        "model": req.model.strip(),
        "prompt": (req.prompt or "ping").strip() or "ping",
        "max_tokens": max(1, min(int(req.max_tokens or 8), 64)),
        "timeout": max(5, min(int(req.timeout or 30), 120)),
        "enabled": bool(req.enabled),
        "created_at": time.strftime("%Y-%m-%d %H:%M", time.localtime()),
        "last_result": None,
    }
    profiles = _load_probe_profiles()
    profiles.append(profile)
    _save_probe_profiles(profiles)
    return {"success": True, "data": profile}


@router.put("/probe/profiles/{profile_id}")
async def update_probe_profile(profile_id: str, req: ProbeProfileUpdate):
    profiles = _load_probe_profiles()
    profile = next((p for p in profiles if p.get("id") == profile_id), None)
    if not profile:
        raise HTTPException(status_code=404, detail="探活配置不存在")
    data = req.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is None:
            continue
        if k == "max_tokens":
            profile[k] = max(1, min(int(v), 64))
        elif k == "timeout":
            profile[k] = max(5, min(int(v), 120))
        elif k in ("name", "group_name", "model", "prompt", "account_id"):
            profile[k] = str(v).strip() if isinstance(v, str) else v
        elif k == "enabled":
            profile[k] = bool(v)
    if profile.get("account_id"):
        acc = next((a for a in _load_accounts() if a["id"] == profile["account_id"]), None)
        if not acc:
            raise HTTPException(status_code=400, detail="渠道不存在")
    _save_probe_profiles(profiles)
    return {"success": True, "data": profile}


@router.delete("/probe/profiles/{profile_id}")
async def delete_probe_profile(profile_id: str):
    profiles = _load_probe_profiles()
    new_list = [p for p in profiles if p.get("id") != profile_id]
    if len(new_list) == len(profiles):
        raise HTTPException(status_code=404, detail="探活配置不存在")
    _save_probe_profiles(new_list)
    return {"success": True, "message": "已删除"}


@router.post("/probe/run")
async def run_probe_once(req: ProbeRunRequest):
    """即时探活（不依赖已保存配置）"""
    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == req.account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="渠道不存在")
    result = await _run_probe_for_account(
        account,
        group_name=req.group_name or "",
        model=req.model,
        prompt=req.prompt or "ping",
        max_tokens=req.max_tokens or 8,
        timeout=req.timeout or 30,
    )
    return {"success": True, "data": result}


@router.post("/probe/profiles/{profile_id}/run")
async def run_probe_profile(profile_id: str):
    """运行单条探活配置，并回写 last_result"""
    profiles = _load_probe_profiles()
    profile = next((p for p in profiles if p.get("id") == profile_id), None)
    if not profile:
        raise HTTPException(status_code=404, detail="探活配置不存在")
    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == profile.get("account_id")), None)
    if not account:
        raise HTTPException(status_code=400, detail="配置关联的渠道不存在，请重新选择")

    result = await _run_probe_for_account(
        account,
        group_name=profile.get("group_name") or "",
        model=profile.get("model") or "",
        prompt=profile.get("prompt") or "ping",
        max_tokens=int(profile.get("max_tokens") or 8),
        timeout=int(profile.get("timeout") or 30),
    )
    result["profile_id"] = profile_id
    result["profile_name"] = profile.get("name")
    profile["last_result"] = result
    profile["last_run"] = result.get("tested_at")
    _save_probe_profiles(profiles)
    return {"success": True, "data": result}


@router.post("/probe/run-all")
async def run_all_probes(only_enabled: bool = Query(True)):
    """并发运行全部（或仅启用的）探活配置"""
    profiles = _load_probe_profiles()
    if only_enabled:
        targets = [p for p in profiles if p.get("enabled", True)]
    else:
        targets = list(profiles)
    if not targets:
        return {"success": True, "data": {"results": [], "summary": {"total": 0, "ok": 0, "fail": 0}}}

    accounts = {a["id"]: a for a in _load_accounts()}

    async def _one(p: dict) -> dict:
        acc = accounts.get(p.get("account_id") or "")
        if not acc:
            return {
                "ok": False,
                "status": "error",
                "latency_ms": 0,
                "error": "渠道不存在",
                "profile_id": p.get("id"),
                "profile_name": p.get("name"),
                "model": p.get("model"),
                "group_name": p.get("group_name"),
                "account_name": "",
                "tested_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            }
        r = await _run_probe_for_account(
            acc,
            group_name=p.get("group_name") or "",
            model=p.get("model") or "",
            prompt=p.get("prompt") or "ping",
            max_tokens=int(p.get("max_tokens") or 8),
            timeout=int(p.get("timeout") or 30),
        )
        r["profile_id"] = p.get("id")
        r["profile_name"] = p.get("name")
        return r

    results = list(await asyncio.gather(*(_one(p) for p in targets)))

    # 回写 last_result
    by_id = {r.get("profile_id"): r for r in results if r.get("profile_id")}
    for p in profiles:
        if p.get("id") in by_id:
            p["last_result"] = by_id[p["id"]]
            p["last_run"] = by_id[p["id"]].get("tested_at")
    _save_probe_profiles(profiles)

    ok = sum(1 for r in results if r.get("ok"))
    return {
        "success": True,
        "data": {
            "results": results,
            "summary": {"total": len(results), "ok": ok, "fail": len(results) - ok},
        },
    }
