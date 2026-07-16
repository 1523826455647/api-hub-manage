"""后端 API 路由"""
import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from services.newapi import NewAPIAdapter
from services.sub2api import Sub2APIAdapter
from services import capsolver

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
    credential_type: str = "token"  # cookie | token | bearer
    username: str | None = None
    password: str | None = None
    recharge_ratio: float = 1.0  # 充值比例如 1:1=1.0, 1:10=10.0


class RedeemRequest(BaseModel):
    code: str


class SettingsUpdate(BaseModel):
    capsolver_api_key: str | None = None
    hub_base_url: str | None = None
    hub_email: str | None = None
    hub_password: str | None = None
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
# 内存缓存：避免每次刷新都请求上游站点
_cache: dict[str, dict[str, Any]] = {}

CACHE_TTL_DASHBOARD = 300  # 仪表盘缓存 5 分钟
CACHE_TTL_ACCOUNT = 180    # 单个账号缓存 3 分钟


def _cache_get(key: str, ttl: int) -> Any | None:
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < ttl:
        return entry["data"]
    return None


def _cache_set(key: str, data: Any):
    _cache[key] = {"data": data, "ts": time.time()}


def _cache_invalidate(prefix: str = ""):
    if not prefix:
        _cache.clear()
    else:
        keys = [k for k in _cache if k.startswith(prefix)]
        for k in keys:
            del _cache[k]

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
            "username": acc.get("username", ""),
            "has_token": bool(acc.get("access_token")),
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
                    async with _httpx.AsyncClient(timeout=15) as _c:
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

    try:
        result["balance"] = await adapter.get_balance()
    except Exception as e:
        result["balance"] = {"error": str(e)}

    try:
        groups = await adapter.get_groups()
        for g in groups:
            g["raw_ratio"] = g.get("ratio", 1.0)
            g["ratio"] = round(g.get("ratio", 1.0) / recharge_ratio, 6)
            g["effective"] = True
        result["groups"] = groups
    except Exception as e:
        result["groups"] = {"error": str(e)}

    try:
        result["usage"] = await adapter.get_usage()
    except Exception as e:
        result["usage"] = {"error": str(e)}

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
            async with httpx.AsyncClient(timeout=15) as client:
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

@router.get("/dashboard")
async def dashboard(force: bool = Query(False, description="强制刷新，忽略缓存")):
    """仪表盘汇总 - 打开页面秒出缓存，点刷新才拉上游"""
    cache_key = "dashboard"
    if not force:
        cached = _cache.get(cache_key, {}).get("data")
        if cached:
            cached["from_cache"] = True
            return {"success": True, "data": cached}
        else:
            # 首次启动无缓存，返回空壳避免阻塞
            return {"success": True, "data": {
                "total_balance": 0, "today_cost": 0, "total_cost": 0,
                "account_count": len(_load_accounts()), "error_count": 0,
                "accounts": [], "cached_at": 0, "from_cache": False,
                "empty": True,
            }}

    accounts = _load_accounts()
    total_balance = 0.0
    today_cost = 0.0
    total_cost = 0.0
    error_count = 0
    account_summaries = []

    for account in accounts:
        adapter = _get_adapter(account)
        rr = account.get("recharge_ratio", 1.0) or 1.0
        s = {"id": account["id"], "name": account["name"], "platform": account["platform"],
             "base_url": account["base_url"], "recharge_ratio": rr,
             "credential_type": account.get("credential_type", "token")}
        try:
            b = await adapter.get_balance()
            s["balance"] = b.get("balance", 0)
            s["group"] = b.get("group", "")
        except Exception as e:
            s["balance"] = None
            em = str(e)
            if "User API Key" in em: s["balance_note"] = em
            else: s["error"] = em
        try:
            u = await adapter.get_usage()
            s["today_cost"] = u.get("today_cost", 0)
            s["total_cost"] = u.get("total_cost", 0)
        except Exception:
            s["today_cost"] = None; s["total_cost"] = None
        try:
            grps = await adapter.get_groups()
            for g in grps:
                g["raw_ratio"] = g.get("ratio", 1.0)
                g["ratio"] = round(g.get("ratio", 1.0) / rr, 6)
                g["effective"] = True
            s["groups"] = grps
        except Exception as e:
            s["groups"] = []
            if "User API Key" not in str(e) and not s.get("error"):
                s["error"] = str(e)
        account_summaries.append(s)
        if s.get("balance"): total_balance += s["balance"]
        if s.get("today_cost"): today_cost += s["today_cost"] or 0
        if s.get("total_cost"): total_cost += s["total_cost"] or 0
        if s.get("error") and "User API Key" not in str(s.get("error","")):
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
    }
    _cache_set(cache_key, result)
    # 倍率历史快照（force=true 时保存）
    _cache_set("dashboard", result)
    if force:
        _save_snapshot_to_history()
    return {"success": True, "data": result}


# === 设置 ===

@router.get("/settings")
async def get_settings():
    """获取系统设置"""
    settings = _load_settings()
    cs_key = settings.get("capsolver_api_key", "")
    masked = ""
    if cs_key:
        masked = cs_key[:6] + "****" + cs_key[-4:] if len(cs_key) > 10 else "****"
    return {
        "success": True,
        "data": {
            "capsolver_api_key_masked": masked,
            "capsolver_configured": bool(cs_key),
            "hub_base_url": settings.get("hub_base_url", ""),
            "hub_email": settings.get("hub_email", ""),
            "hub_configured": bool(settings.get("hub_base_url") and settings.get("hub_email")),
            "hub_password_set": bool(settings.get("hub_password")),
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
    if req.hub_password is not None:
        settings["hub_password"] = req.hub_password
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


@router.post("/hub/test")
async def test_hub():
    """测试 Sub2API Hub 连接"""
    settings = _load_settings()
    base_url = settings.get("hub_base_url", "")
    email = settings.get("hub_email", "")
    password = settings.get("hub_password", "")
    if not base_url or not email or not password:
        raise HTTPException(status_code=400, detail="请先配置 Hub 地址和管理员账号")
    from services.sub2api_admin import Sub2APIAdmin
    admin = Sub2APIAdmin(base_url)
    try:
        jwt = await admin.login(email, password)
        admin.jwt = jwt
        groups = await admin.list_groups()
        return {
            "success": True,
            "data": {
                "groups_count": len(groups),
                "groups": [{"id": g.get("id"), "name": g.get("name"),
                            "platform": g.get("platform")} for g in groups[:20]],
            },
        }
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
            "has_upstream_key": bool(
                (acc.get("id") and
                 next((a for a in _load_accounts() if a["id"] == acc["id"]), {})
                 .get("upstream_key", ""))
                or next((a for a in _load_accounts() if a["id"] == acc["id"]), {})
                .get("access_token", "")
            ),
        }
        for g in (acc.get("groups") or []):
            ratio = g.get("ratio")
            if ratio is not None and ratio < threshold:
                existing = _find_mapping(acc["id"], g.get("name", ""))
                candidates.append({
                    **acc_info,
                    "group_name": g.get("name", ""),
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
                        existing and abs(existing.get("last_ratio", ratio) - ratio) > 0.0001
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
    """将一个低价分组配置到 Sub2API Hub（自动去重，已存在则跳过）"""
    account_id = req.get("account_id")
    group_name = req.get("group_name")
    ratio = req.get("ratio", 1.0)

    if not account_id or not group_name:
        raise HTTPException(status_code=400, detail="缺少 account_id 或 group_name")

    # 检查是否已配置
    existing = _find_mapping(account_id, group_name)
    if existing:
        return {
            "success": True,
            "data": {"status": "skipped", "reason": "已配置，无需重复创建",
                     "mapping": existing},
        }

    accounts = _load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="渠道不存在")

    upstream_key = _resolve_upstream_key(account, group_name)
    if not upstream_key:
        raise HTTPException(status_code=400,
                            detail=f"分组「{group_name}」未配置上游 API Key。请在扫描器中为该分组设置密钥，或在渠道中填写上游 Key")
    # Cookie 不能作为上游 Key（除非有分组级 Key 覆盖）
    if (account.get("credential_type") == "cookie" and not account.get("upstream_key")
            and not _get_group_key(account_id, group_name)):
        raise HTTPException(status_code=400,
                            detail="该渠道使用 Cookie 鉴权且未设置分组密钥。请在扫描器中为具体分组设置 sk- 密钥")

    platform = _group_to_platform(group_name, account.get("platform"))

    settings = _load_settings()
    hub_url = settings.get("hub_base_url", "")
    hub_email = settings.get("hub_email", "")
    hub_password = settings.get("hub_password", "")
    if not hub_url or not hub_email or not hub_password:
        raise HTTPException(status_code=400, detail="请先在设置中配置 Sub2API Hub")

    from services.sub2api_admin import Sub2APIAdmin
    admin = Sub2APIAdmin(hub_url)
    try:
        await admin.login(hub_email, hub_password)
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
        # 记录映射
        mapping = {
            "id": str(uuid.uuid4())[:8],
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
            "last_ratio": ratio,
            "last_sync": time.strftime("%Y-%m-%d %H:%M", time.localtime()),
            "created_at": time.strftime("%Y-%m-%d %H:%M", time.localtime()),
        }
        mappings = _load_mappings()
        mappings.append(mapping)
        _save_mappings(mappings)
        result["mapping"] = mapping
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/scanner/sync-ratio")
async def sync_ratio(mapping_id: str):
    """同步一条映射的倍率：如果上游倍率变了，更新 Hub 对应分组的 rate_multiplier"""
    mappings = _load_mappings()
    mapping = next((m for m in mappings if m.get("id") == mapping_id), None)
    if not mapping:
        raise HTTPException(status_code=404, detail="映射不存在")

    # 从缓存获取当前倍率
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

    if abs(current_ratio - mapping["last_ratio"]) < 0.0001:
        return {"success": True, "data": {"status": "unchanged", "ratio": current_ratio}}

    # 更新 Hub 分组倍率
    settings = _load_settings()
    hub_url = settings.get("hub_base_url", "")
    hub_email = settings.get("hub_email", "")
    hub_password = settings.get("hub_password", "")
    if not hub_url or not hub_email or not hub_password:
        raise HTTPException(status_code=400, detail="请先配置 Hub")

    from services.sub2api_admin import Sub2APIAdmin
    admin = Sub2APIAdmin(hub_url)
    try:
        await admin.login(hub_email, hub_password)
        await admin._request("PUT", f"/api/v1/admin/groups/{mapping['hub_group_id']}",
                             json={"rate_multiplier": current_ratio})
        # 更新映射记录
        old_ratio = mapping["last_ratio"]
        mapping["last_ratio"] = current_ratio
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
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/scanner/auto-sync")
async def auto_sync():
    """一键全自动同步：扫描→新建→倍率更新，返回操作摘要"""
    settings = _load_settings()
    threshold = settings.get("ultra_low_rate", 0.6)
    hub_url = settings.get("hub_base_url", "")
    hub_email = settings.get("hub_email", "")
    hub_password = settings.get("hub_password", "")

    log: list[dict] = []
    new_count = 0
    update_count = 0
    skip_count = 0
    error_count = 0

    # 确保有最新数据
    await dashboard(force=True)
    cached = _cache.get("dashboard", {}).get("data")
    if not cached:
        raise HTTPException(status_code=400, detail="暂无数据")

    accounts_data = cached.get("accounts", [])
    stored_accounts = _load_accounts()

    # 是否需要 Hub 操作
    need_hub = bool(hub_url and hub_email and hub_password)
    admin = None
    if need_hub:
        from services.sub2api_admin import Sub2APIAdmin
        admin = Sub2APIAdmin(hub_url)
        try:
            await admin.login(hub_email, hub_password)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Hub 登录失败: {e}")

    for acc in accounts_data:
        stored = next((a for a in stored_accounts if a["id"] == acc["id"]), {})
        upstream_key = _resolve_upstream_key(stored, g.get("name", ""))
        if not upstream_key:
            continue  # 无可用上游 key
        # Cookie 无独立 key 且无分组密钥时不能用于上游调用
        if (stored.get("credential_type") == "cookie" and not stored.get("upstream_key")
                and not _get_group_key(acc["id"], g.get("name", ""))):
            continue

        for g in (acc.get("groups") or []):
            ratio = g.get("ratio")
            if ratio is None or ratio >= threshold:
                continue

            group_name = g.get("name", "")
            existing = _find_mapping(acc["id"], group_name)

            if existing:
                # 已存在，检查倍率是否变化
                if abs(existing["last_ratio"] - ratio) > 0.0001 and need_hub:
                    try:
                        await admin._request(
                            "PUT", f"/api/v1/admin/groups/{existing['hub_group_id']}",
                            json={"rate_multiplier": ratio})
                        old_r = existing["last_ratio"]
                        existing["last_ratio"] = ratio
                        existing["last_sync"] = time.strftime("%Y-%m-%d %H:%M",
                                                              time.localtime())
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
                # Check if better than existing mapped ratio in same category
                cat = _classify_group_category(group_name)
                mapped_best = 999.0
                for m in _load_mappings():
                    if (m["upstream_account_id"] == acc["id"]
                            and _classify_group_category(m["upstream_group_name"]) == cat):
                        if m["last_ratio"] < mapped_best:
                            mapped_best = m["last_ratio"]
                is_upgrade = ratio < mapped_best
                if not is_upgrade and mapped_best < 999:
                    log.append({"action": "skipped", "group": group_name,
                                "reason": f"{ratio}x >= {mapped_best}x"})
                    skip_count += 1
                    continue
                # New or better - create
                if need_hub:
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
                            "last_ratio": ratio,
                            "last_sync": time.strftime("%Y-%m-%d %H:%M", time.localtime()),
                            "created_at": time.strftime("%Y-%m-%d %H:%M", time.localtime()),
                        }
                        mappings = _load_mappings()
                        mappings.append(mapping)
                        _save_mappings(mappings)
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
                else:
                    log.append({
                        "action": "skipped",
                        "group": group_name,
                        "reason": "Hub 未配置",
                    })
                    skip_count += 1

    _save_mappings(_load_mappings())  # 确保 mapping 更新已持久化
    return {
        "success": True,
        "data": {
            "summary": {
                "new": new_count,
                "updated": update_count,
                "skipped": skip_count,
                "errors": error_count,
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


@router.delete("/group-keys/{account_id}/{group_name}")
async def delete_group_key(account_id: str, group_name: str):
    """删除一个分组的 API Key"""
    keys = _load_group_keys()
    key = f"{account_id}/{group_name}"
    if key in keys:
        del keys[key]
        _save_group_keys(keys)
    return {"success": True, "message": "分组密钥已删除"}


def _resolve_upstream_key(account: dict, group_name: str) -> str:
    """解析上游 Key：分组级 > 账号级 > access_token"""
    gk = _get_group_key(account["id"], group_name)
    if gk:
        return gk
    return account.get("upstream_key", "") or account.get("access_token", "")


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
