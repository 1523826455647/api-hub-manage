"""
Sub2API Admin API 客户端

使用管理员账号/JWT 鉴权，调用 /api/v1/admin/* 端点：
- 账号管理：创建/更新上游账号（OpenAI-compatible API Key 类型）
- 分组管理：创建/查询分组，绑定账号
- 密钥管理：查询分组已绑定账号

参考: https://github.com/Wei-Shaw/sub2api
"""
import httpx
from typing import Any
from . import new_async_client


class Sub2APIAdmin:
    """Sub2API 管理端客户端"""

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
        async with new_async_client(10.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/v1/auth/login",
                json={"email": email, "password": password},
            )
            data = resp.json()
            if data.get("code", -1) != 0:
                raise ValueError(f"登录失败: {data.get('message', '未知错误')}")
            self.jwt = data["data"]["access_token"]
            return self.jwt

    async def _request(self, method: str, path: str, json: dict = None) -> dict:
        async with new_async_client(10.0) as client:
            r = await client.request(method, f"{self.base_url}{path}", headers=self._headers(),
                                     json=json or None)
            data = r.json()
            if data.get("code", -1) != 0:
                raise ValueError(f"Sub2API 请求失败 [{path}]: {data.get('message', '')}")
            return data.get("data", data)

    # ── 分组 ──────────────────────────────────────────────
    PLATFORMS = ["anthropic", "openai", "gemini", "antigravity", "grok"]

    async def list_groups(self, platform: str = "") -> list[dict]:
        """列出所有分组（含 inactive）"""
        params = {"include_inactive": "true"}
        if platform:
            params["platform"] = platform
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return await self._request("GET", f"/api/v1/admin/groups/all?{qs}")

    async def create_group(self, **fields) -> dict:
        """创建分组，返回新分组对象"""
        return await self._request("POST", "/api/v1/admin/groups", json=fields)

    async def ensure_group(self, name: str, platform: str, rate: float = 1.0,
                           description: str = "") -> dict:
        """确保分组存在（不存在则创建），返回分组对象"""
        existing = await self.list_groups(platform=platform)
        for g in existing:
            if g.get("name", "").lower() == name.lower():
                return g
        return await self.create_group(
            name=name, platform=platform, rate_multiplier=rate,
            description=description, subscription_type="standard",
        )

    async def get_group_accounts(self, group_id: int) -> list[dict]:
        """获取分组下的账号列表（通过 account list 过滤）"""
        result = await self._request(
            "GET", f"/api/v1/admin/accounts?group_id={group_id}&page_size=200"
        )
        return result if isinstance(result, list) else result.get("items", result.get("accounts", []))

    # ── 账号 ──────────────────────────────────────────────
    async def create_account(self, **fields) -> dict:
        """创建上游账号"""
        return await self._request("POST", "/api/v1/admin/accounts", json=fields)

    async def list_accounts(self, platform: str = "", search: str = "",
                            status: str = "", page_size: int = 200) -> list[dict]:
        """列出上游账号"""
        params = {"page_size": page_size}
        if platform:
            params["platform"] = platform
        if search:
            params["search"] = search
        if status:
            params["status"] = status
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        result = await self._request("GET", f"/api/v1/admin/accounts?{qs}")
        return result if isinstance(result, list) else result.get("items", result.get("accounts", []))

    async def update_account(self, account_id: int, **fields) -> dict:
        """更新上游账号"""
        return await self._request("PUT", f"/api/v1/admin/accounts/{account_id}", json=fields)

    async def bulk_update_accounts(self, account_ids: list[int], group_ids: list[int] = None,
                                   status: str = None) -> dict:
        """批量更新账号"""
        body: dict = {"account_ids": account_ids}
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
        1. 确保「超低价分组」存在
        2. 创建上游账号（api_key 类型）
        3. 绑定到超低价分组
        返回创建结果摘要
        """
        # 1. 确保分组
        group = await self.ensure_group(
            name=group_name, platform=platform, rate=group_rate,
            description="由 API Hub Manager 自动创建的超低价分组",
        )
        # 2. 创建账号
        acct = await self.create_account(
            name=account_name,
            type="apikey",
            platform=platform,
            base_url=base_url,
            api_key=api_key,
            enabled=True,
            group_ids=[group["id"]],
        )
        return {
            "group": {"id": group["id"], "name": group["name"]},
            "account": {"id": acct.get("id"), "name": acct.get("name")},
        }
