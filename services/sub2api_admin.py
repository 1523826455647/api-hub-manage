"""
Sub2API Admin API 客户端

使用管理员账号/JWT 或 x-api-key 鉴权，调用 /api/v1/admin/* 端点：
- 账号管理：创建/更新上游账号（apikey 类型，credentials 字典）
- 分组管理：创建/查询分组，绑定账号，更新 rate_multiplier

参考: https://github.com/Wei-Shaw/sub2api
"""
from typing import Any

from . import new_async_client


class Sub2APIAdmin:
    """Sub2API 管理端客户端"""

    PLATFORMS = ["anthropic", "openai", "gemini", "antigravity", "grok"]

    def __init__(self, base_url: str, api_key: str = "", jwt: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.jwt = jwt

    # ── 鉴权 ──────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["x-api-key"] = self.api_key
        elif self.jwt:
            h["Authorization"] = f"Bearer {self.jwt}"
        return h

    async def login(self, email: str, password: str) -> str:
        """管理员登录，返回 JWT access_token"""
        async with new_async_client(15.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/v1/auth/login",
                json={"email": email, "password": password},
            )
            data = resp.json()
            if data.get("code", -1) != 0:
                raise ValueError(f"登录失败: {data.get('message', '未知错误')}")
            token = (data.get("data") or {}).get("access_token", "")
            if not token:
                raise ValueError("登录失败: 未返回 access_token")
            self.jwt = token
            return self.jwt

    async def _request(self, method: str, path: str, json: dict | None = None) -> Any:
        async with new_async_client(15.0) as client:
            r = await client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=json if json is not None else None,
            )
            try:
                data = r.json()
            except Exception:
                raise ValueError(f"Sub2API 响应非 JSON [{path}] HTTP {r.status_code}: {r.text[:200]}")
            # 部分接口可能直接返回对象；优先按 code 协议解析
            if isinstance(data, dict) and "code" in data:
                if data.get("code", -1) != 0:
                    raise ValueError(f"Sub2API 请求失败 [{path}]: {data.get('message', '') or r.status_code}")
                return data.get("data", data)
            if r.status_code >= 400:
                raise ValueError(f"Sub2API 请求失败 [{path}] HTTP {r.status_code}: {str(data)[:200]}")
            return data

    # ── 分组 ──────────────────────────────────────────────
    async def list_groups(self, platform: str = "") -> list[dict]:
        """列出所有分组（含 inactive）"""
        params = ["include_inactive=true"]
        if platform:
            params.append(f"platform={platform}")
        result = await self._request("GET", f"/api/v1/admin/groups/all?{'&'.join(params)}")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("items", "groups", "data", "list"):
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    async def create_group(self, **fields) -> dict:
        """创建分组，返回新分组对象"""
        return await self._request("POST", "/api/v1/admin/groups", json=fields)

    async def update_group(self, group_id: int | str, **fields) -> dict:
        """更新分组字段（如 rate_multiplier）"""
        return await self._request("PUT", f"/api/v1/admin/groups/{group_id}", json=fields)

    async def ensure_group(self, name: str, platform: str, rate: float = 1.0,
                           description: str = "") -> dict:
        """确保分组存在；已存在则必要时同步 rate_multiplier。"""
        existing = await self.list_groups(platform=platform)
        for g in existing:
            if (g.get("name") or "").lower() == name.lower():
                # 倍率变化时更新
                try:
                    cur = float(g.get("rate_multiplier", 1.0) or 1.0)
                except (TypeError, ValueError):
                    cur = 1.0
                if abs(cur - float(rate)) > 0.0001:
                    try:
                        updated = await self.update_group(g["id"], rate_multiplier=float(rate))
                        if isinstance(updated, dict) and updated.get("id"):
                            return updated
                        g["rate_multiplier"] = float(rate)
                    except Exception:
                        # 更新失败仍返回已有分组，由上层记录错误
                        pass
                return g
        return await self.create_group(
            name=name,
            platform=platform,
            rate_multiplier=float(rate),
            description=description or "由 API Hub Manager 自动创建的超低价分组",
            subscription_type="standard",
        )

    async def get_group_accounts(self, group_id: int) -> list[dict]:
        """获取分组下的账号列表"""
        result = await self._request(
            "GET", f"/api/v1/admin/accounts?group_id={group_id}&page_size=200"
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("items") or result.get("accounts") or []
        return []

    # ── 账号 ──────────────────────────────────────────────
    async def create_account(self, **fields) -> dict:
        """创建上游账号（原始透传，供高级调用）"""
        return await self._request("POST", "/api/v1/admin/accounts", json=fields)

    async def list_accounts(self, platform: str = "", search: str = "",
                            status: str = "", page_size: int = 200) -> list[dict]:
        """列出上游账号"""
        params = [f"page_size={page_size}"]
        if platform:
            params.append(f"platform={platform}")
        if search:
            params.append(f"search={search}")
        if status:
            params.append(f"status={status}")
        result = await self._request("GET", f"/api/v1/admin/accounts?{'&'.join(params)}")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("items") or result.get("accounts") or []
        return []

    async def update_account(self, account_id: int | str, **fields) -> dict:
        """更新上游账号"""
        return await self._request("PUT", f"/api/v1/admin/accounts/{account_id}", json=fields)

    async def bulk_update_accounts(self, account_ids: list[int], group_ids: list[int] | None = None,
                                   status: str | None = None) -> dict:
        """批量更新账号"""
        body: dict[str, Any] = {"account_ids": account_ids}
        if group_ids is not None:
            body["group_ids"] = group_ids
        if status is not None:
            body["status"] = status
        return await self._request("POST", "/api/v1/admin/accounts/bulk-update", json=body)

    # ── 高层封装 ──────────────────────────────────────────
    async def provision_upstream(
        self,
        account_name: str,
        base_url: str,
        api_key: str,
        platform: str = "openai",
        group_name: str = "超低价自动化",
        group_rate: float = 1.0,
    ) -> dict:
        """
        自动配置上游：
        1. 确保「超低价分组」存在（并同步倍率）
        2. 创建上游账号（type=apikey，credentials 字典，符合官方 schema）
        3. 绑定到目标分组
        """
        if platform not in self.PLATFORMS:
            # 未知平台回退到 openai（OpenAI-compatible 最通用）
            platform = "openai"

        group = await self.ensure_group(
            name=group_name,
            platform=platform,
            rate=group_rate,
            description="由 API Hub Manager 自动创建的超低价分组",
        )
        if not group or group.get("id") is None:
            raise ValueError(f"创建/获取 Hub 分组失败: {group_name}")

        # 官方 CreateAccountRequest：credentials 为必填 map
        # apikey 类型常见字段：api_key + base_url（OpenAI-compatible 上游）
        base = (base_url or "").rstrip("/")
        credentials: dict[str, Any] = {
            "api_key": api_key,
            "base_url": base,
        }
        payload: dict[str, Any] = {
            "name": account_name,
            "type": "apikey",
            "platform": platform,
            "credentials": credentials,
            "extra": {"base_url": base},
            "group_ids": [group["id"]],
            "concurrency": 3,
            "priority": 50,
        }

        try:
            acct = await self.create_account(**payload)
        except ValueError as e:
            # 兼容少数旧版/变体：尝试把 base_url 仅放 extra
            msg = str(e).lower()
            if "credential" in msg or "base_url" in msg or "unknown" in msg or "field" in msg:
                payload_alt = {
                    "name": account_name,
                    "type": "apikey",
                    "platform": platform,
                    "credentials": {"api_key": api_key},
                    "extra": {"base_url": base, "api_base": base},
                    "group_ids": [group["id"]],
                }
                acct = await self.create_account(**payload_alt)
            else:
                raise

        if not isinstance(acct, dict):
            raise ValueError(f"创建账号返回异常: {acct}")

        return {
            "group": {
                "id": group.get("id"),
                "name": group.get("name", group_name),
                "rate_multiplier": group.get("rate_multiplier", group_rate),
            },
            "account": {
                "id": acct.get("id"),
                "name": acct.get("name", account_name),
            },
        }
