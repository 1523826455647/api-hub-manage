"""
Sub2API 服务适配器

关键端点：
- GET  /api/v1/settings/public     - 公开设置
- POST /api/v1/auth/login           - 登录（email/password）→ access_token + refresh_token
- POST /api/v1/auth/refresh         - 刷新 token
- GET  /api/v1/auth/me              - 获取用户信息（含 balance）
- GET  /api/v1/groups/available     - 获取可用分组
- GET  /api/v1/groups/rates         - 获取分组倍率
- GET  /api/v1/usage/dashboard/stats - 获取消耗统计
- GET  /api/v1/announcements        - 获取公告
- POST /api/v1/redeem               - 兑换码兑换
- GET  /api/v1/payment/checkout-info - 充值信息
"""
import httpx
from typing import Any
from . import BaseAdapter


class Sub2APIAdapter(BaseAdapter):
    """Sub2API 适配器"""

    def __init__(self, base_url: str, access_token: str | None = None,
                 credential_type: str = "bearer"):
        super().__init__(base_url, access_token, credential_type)
        self.refresh_token: str | None = None

    async def login(self, username: str, password: str, turnstile_token: str = "") -> str:
        """使用邮箱/密码登录，返回 access_token"""
        async with httpx.AsyncClient(timeout=30) as client:
            body: dict[str, Any] = {"email": username, "password": password}
            if turnstile_token:
                body["turnstile_token"] = turnstile_token

            resp = await client.post(
                f"{self.base_url}/api/v1/auth/login",
                json=body,
            )
            data = resp.json()
            # Sub2API 响应格式: { code: 0, message: "...", data: {...} }
            code = data.get("code", -1)
            if code != 0:
                raise ValueError(f"登录失败: {data.get('message', '未知错误')}")

            login_data = data.get("data", {})
            if login_data.get("requires_2fa"):
                raise ValueError("该账号启用了两步验证，暂不支持")

            token = login_data.get("access_token", "")
            if not token:
                raise ValueError("登录失败: 未获取到 access_token")

            self.access_token = token
            self.refresh_token = login_data.get("refresh_token", "")
            return token

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    async def _request_json(self, client: httpx.AsyncClient, url: str) -> Any:
        """发送 GET 请求并解析 Sub2API 格式的响应"""
        resp = await client.get(url, headers=self._headers())
        data = resp.json()
        code = data.get("code", -1)
        if code != 0:
            raise ValueError(data.get("message", "请求失败"))
        return data.get("data", {})

    async def refresh_session(self) -> str:
        """使用 refresh_token 刷新 access_token"""
        if not self.refresh_token:
            raise ValueError("无 refresh_token，无法刷新")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/api/v1/auth/refresh",
                json={"refresh_token": self.refresh_token},
                headers={"Content-Type": "application/json"},
            )
            data = resp.json()
            if data.get("code", -1) != 0:
                raise ValueError(f"刷新失败: {data.get('message', '')}")
            token_data = data.get("data", {})
            self.access_token = token_data.get("access_token", "")
            if token_data.get("refresh_token"):
                self.refresh_token = token_data["refresh_token"]
            return self.access_token

    async def get_balance(self) -> dict[str, Any]:
        """获取用户余额 - 使用 /api/v1/auth/me"""
        async with httpx.AsyncClient(timeout=30) as client:
            me = await self._request_json(client, f"{self.base_url}/api/v1/auth/me")
            balance = me.get("balance", 0)
            return {
                "balance": round(float(balance), 4),
                "used_quota": 0,  # Sub2API 的 me 接口不直接返回 used
                "total_quota": round(float(balance), 4),
                "username": me.get("username", me.get("email", "")),
                "display_name": me.get("nickname", me.get("name", "")),
                "email": me.get("email", ""),
                "group": me.get("group_name", ""),
            }

    async def get_groups(self) -> list[dict[str, Any]]:
        """获取分组信息 - /api/v1/groups/available + /api/v1/groups/rates"""
        async with httpx.AsyncClient(timeout=30) as client:
            # 获取可用分组
            groups_data = await self._request_json(
                client, f"{self.base_url}/api/v1/groups/available"
            )

            # 获取倍率覆盖
            overrides = {}
            try:
                rates_data = await self._request_json(
                    client, f"{self.base_url}/api/v1/groups/rates"
                )
                if isinstance(rates_data, dict):
                    overrides = rates_data
            except Exception:
                pass

            groups = []
            if isinstance(groups_data, list):
                for g in groups_data:
                    gid = g.get("id", 0)
                    rate = g.get("rate_multiplier", 1.0)
                    # 检查是否有倍率覆盖
                    if str(gid) in overrides:
                        rate = overrides[str(gid)]
                    groups.append({
                        "id": gid,
                        "name": g.get("name", ""),
                        "description": g.get("description", ""),
                        "ratio": float(rate),
                        "models": g.get("models", []),
                    })

            return groups

    async def get_models(self) -> list[dict[str, Any]]:
        """获取可用模型列表"""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.base_url}/api/models",
                headers=self._headers(),
            )
            data = resp.json()
            models = []
            model_list = data.get("data", [])
            if isinstance(model_list, list):
                for m in model_list:
                    if isinstance(m, dict):
                        models.append({
                            "model": m.get("id", ""),
                            "owned_by": m.get("owned_by", ""),
                        })
                    else:
                        models.append({"model": str(m), "owned_by": ""})
            return models

    async def get_usage(self) -> dict[str, Any]:
        """获取消耗统计 - /api/v1/usage/dashboard/stats"""
        async with httpx.AsyncClient(timeout=30) as client:
            stats = await self._request_json(
                client, f"{self.base_url}/api/v1/usage/dashboard/stats"
            )
            return {
                "today_cost": round(float(stats.get("today_actual_cost", 0)), 4),
                "total_cost": round(float(stats.get("total_actual_cost", 0)), 4),
            }

    async def get_announcements(self) -> list[dict[str, Any]]:
        """获取公告 - /api/v1/announcements"""
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                items = await self._request_json(
                    client, f"{self.base_url}/api/v1/announcements"
                )
                if isinstance(items, list):
                    return [
                        {
                            "id": a.get("id", 0),
                            "title": a.get("title", ""),
                            "content": a.get("content", ""),
                            "created_at": a.get("created_at", ""),
                        }
                        for a in items
                    ]
            except Exception:
                pass
            return []

    async def redeem_code(self, code: str) -> dict[str, Any]:
        """兑换码兑换 - /api/v1/redeem"""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/api/v1/redeem",
                json={"code": code},
                headers=self._headers(),
            )
            data = resp.json()
            if data.get("code", -1) != 0:
                raise ValueError(f"兑换失败: {data.get('message', '未知错误')}")
            result = data.get("data", {})
            return {
                "message": result.get("message", "兑换成功"),
                "type": result.get("type", "balance"),
                "value": result.get("value", 0),
                "new_balance": result.get("new_balance"),
                "group_name": result.get("group_name"),
            }
