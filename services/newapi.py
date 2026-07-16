"""
NewAPI 服务适配器
兼容 one-api / new-api 系列中转站的 API 接口

关键端点：
- GET  /api/status              - 获取站点状态（含 quota_per_unit）
- POST /api/user/login          - 用户登录（username/password）
- GET  /api/user/self           - 获取用户信息（含 quota, used_quota, group）
- GET  /api/user/self/groups    - 获取用户可见分组及倍率
- GET  /api/log/self/stat       - 获取用户消耗统计
- GET  /api/models              - 获取模型列表
- POST /api/user/topup          - 兑换码兑换
"""
import httpx
from typing import Any
from . import BaseAdapter


def _extract_user_id_from_session(cookie_value: str) -> str:
    """从 NewAPI session cookie 的 gob 编码中尝试提取 user_id"""
    import base64
    try:
        val = cookie_value.strip()
        # session cookie 格式: base64(timestamp | gob_data | hmac)
        padded = val + "=" * (4 - len(val) % 4) if len(val) % 4 else val
        decoded = base64.urlsafe_b64decode(padded)
        # 跳过 timestamp
        pipe1 = decoded.find(b"|")
        if pipe1 < 0:
            return ""
        gob_part = decoded[pipe1 + 1:]
        # Gob 编码中, int 字段的结构:
        # - 字段类型(1B) + 字段名长度(varint) + 字段名 + 值编码
        # int 值编码: 对于小整数 < 128: 直接一个字节
        # 对于中整数 < 2^31: 用 varint 编码
        # 我们扫描 gob 数据, 找 "id" 字段后面的整数值
        idx = 0
        while idx < len(gob_part) - 5:
            # 找 "id" 标记 (0x02 是 string 的 field number 2)
            chunk = gob_part[idx:idx + 4]
            # 模式: \x02\x02id\x04... 即 field 2, name "id", type int(4)
            if chunk[:2] == b"\x02\x02" and chunk[2:4] == b"id":
                # 接下来应该是 int 的值编码
                after_name = gob_part[idx + 4:]
                if after_name:
                    int_tag = after_name[0]  # should be 0x04 (int type)
                    if int_tag in (0x04, 0x02):
                        val_start = idx + 5
                        val_byte = gob_part[val_start] if val_start < len(gob_part) else 0
                        if val_byte < 128:
                            # Direct small int
                            return str(val_byte)
                        elif val_byte >= 0x80 and val_byte < 0xfe:
                            # Varint encoded
                            try:
                                result = 0
                                shift = 0
                                pos = val_start
                                while pos < len(gob_part):
                                    b = gob_part[pos]
                                    result |= (b & 0x7f) << shift
                                    shift += 7
                                    pos += 1
                                    if b < 0x80:
                                        break
                                return str(result)
                            except Exception:
                                pass
            idx += 1
    except Exception:
        pass
    return ""


class NewAPIAdapter(BaseAdapter):
    """NewAPI / One-API 适配器"""

    def __init__(self, base_url: str, access_token: str | None = None,
                 credential_type: str = "token"):
        super().__init__(base_url, access_token, credential_type)
        self.user_id: str = ""
        # 向后兼容：旧数据没有 credential_type 或为默认 token，根据 token 格式自动判断
        if credential_type in (None, "", "token"):
            if access_token:
                cred = access_token.strip()
                if cred.lower().startswith("session=") or (";" in cred and "=" in cred):
                    self.credential_type = "cookie"
                elif cred.lower().startswith("sk-"):
                    # sk- 前缀大概率是 User API Key（非系统令牌）
                    # 保留 token 让用户显式选择，但如果调 /api/* 失败则可能需切换
                    self.credential_type = "token"
                else:
                    self.credential_type = "token"
            else:
                self.credential_type = "token"

    async def _get_quota_per_unit(self) -> float:
        """从 /api/status 获取 quota_per_unit（每美元对应的 quota 数）"""
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.get(f"{self.base_url}/api/status")
                data = resp.json()
                qpu = data.get("data", data).get("quota_per_unit", 500000)
                return qpu if qpu > 0 else 500000
            except Exception:
                return 500000

    async def check_turnstile(self) -> dict[str, Any]:
        """检查站点是否开启了 Turnstile 人机验证"""
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.get(f"{self.base_url}/api/status")
                data = resp.json()
                status = data.get("data", data)
                return {
                    "enabled": bool(status.get("turnstile_check", False)),
                    "site_key": status.get("turnstile_site_key", ""),
                }
            except Exception:
                return {"enabled": False, "site_key": ""}

    async def login(self, username: str, password: str, turnstile_token: str = "") -> str:
        """使用用户名密码登录，返回 session cookie

        对于开启了 Cloudflare Turnstile 的站点，需要传入 turnstile_token。
        token 可通过打码平台（CapSolver/2Captcha）获取，或从浏览器手动提取。
        """
        async with httpx.AsyncClient(timeout=10) as client:
            params = {}
            if turnstile_token:
                params["turnstile"] = turnstile_token

            resp = await client.post(
                f"{self.base_url}/api/user/login",
                json={"username": username, "password": password},
                params=params,
            )
            data = resp.json()
            if not data.get("success", False):
                msg = data.get("message", "未知错误")
                # 提示用户可能需要 Turnstile 验证
                if "turnstile" in msg.lower() or "captcha" in msg.lower() or "human" in msg.lower():
                    raise ValueError(
                        f"登录失败（需要人机验证）: {msg}\n"
                        "提示：该站点开启了 Cloudflare Turnstile，请使用 Token/Cookie 方式添加账号"
                    )
                raise ValueError(f"登录失败: {msg}")

            login_data = data.get("data", {})
            if isinstance(login_data, dict) and login_data.get("require_2fa"):
                raise ValueError("该账号启用了两步验证，暂不支持")

            # 提取 cookie
            cookies = dict(resp.cookies)
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

            # 提取 user_id
            if isinstance(login_data, dict):
                self.user_id = str(login_data.get("id", ""))

            self.cookie = cookie_str
            self.access_token = cookie_str  # 存储用于持久化
            return cookie_str

    def _headers(self) -> dict[str, str]:
        """
        构建请求头。
        credential_type=cookie → Cookie 头
        credential_type=bearer → Authorization: Bearer <token>
        credential_type=token  → Authorization: <token>（裸令牌，NewAPI 标准格式）
        """
        headers = super()._headers()
        if self.user_id:
            headers["New-Api-User"] = self.user_id
        return headers

    async def _ensure_user_id(self):
        """Token 模式下首次请求时，尝试获取 user_id（某些站点可选，失败不阻塞）"""
        if self.user_id:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/api/user/self", headers=self._headers()
                )
                data = resp.json()
                if data.get("success", False):
                    user_data = data.get("data", {})
                    uid = str(user_data.get("id", ""))
                    if uid:
                        self.user_id = uid
            except Exception:
                pass

    async def get_balance(self) -> dict[str, Any]:
        """获取用户余额信息"""
        if self.credential_type == "user_api_key":
            raise ValueError("User API Key 无法查询余额，请使用 Cookie 登录方式")
        await self._ensure_user_id()
        quota_per_unit = await self._get_quota_per_unit()

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/api/user/self",
                headers=self._headers(),
            )
            data = resp.json()
            if not data.get("success", False):
                raise ValueError(f"查询失败: {data.get('message', '未知错误')}")

            user_data = data.get("data", {})
            quota = user_data.get("quota", 0)
            used_quota = user_data.get("used_quota", 0)

            balance = quota / quota_per_unit
            used = used_quota / quota_per_unit

            return {
                "balance": round(balance, 4),
                "used_quota": round(used, 4),
                "total_quota": round(balance + used, 4),
                "raw_quota": quota,
                "raw_used_quota": used_quota,
                "quota_per_unit": quota_per_unit,
                "username": user_data.get("username", ""),
                "display_name": user_data.get("display_name", ""),
                "group": user_data.get("group", "default"),
                "email": user_data.get("email", ""),
            }

    async def get_groups(self) -> list[dict[str, Any]]:
        """获取分组信息和倍率 - 使用 /api/user/self/groups"""
        if self.credential_type == "user_api_key":
            raise ValueError("User API Key 无法查询分组倍率，请使用 Cookie 登录方式")
        await self._ensure_user_id()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/api/user/self/groups",
                headers=self._headers(),
            )
            data = resp.json()
            groups = []

            if data.get("success", False):
                group_data = data.get("data", {})
                # NewAPI 返回格式: {"default": {"ratio": 1, "desc": "..."}, ...}
                if isinstance(group_data, dict):
                    for name, info in group_data.items():
                        if isinstance(info, dict):
                            ratio = info.get("ratio", 1.0)
                            # ratio 可能是字符串（如 "自动"），跳过
                            if isinstance(ratio, str):
                                continue
                            groups.append({
                                "name": name,
                                "ratio": float(ratio),
                                "description": info.get("desc", ""),
                                "models": info.get("models", []),
                            })
                        else:
                            try:
                                groups.append({
                                    "name": name,
                                    "ratio": float(info),
                                    "description": "",
                                    "models": [],
                                })
                            except (ValueError, TypeError):
                                continue

            # 备选：尝试旧版 /api/group/ 接口
            if not groups:
                try:
                    resp2 = await client.get(
                        f"{self.base_url}/api/group/",
                        headers=self._headers(),
                    )
                    data2 = resp2.json()
                    if data2.get("success", False):
                        gd = data2.get("data", {})
                        if isinstance(gd, dict):
                            for name, info in gd.items():
                                if isinstance(info, dict):
                                    groups.append({
                                        "name": name,
                                        "ratio": info.get("ratio", 1.0),
                                        "description": info.get("desc", ""),
                                        "models": info.get("models", []),
                                    })
                except Exception:
                    pass

            return groups

    async def get_models(self) -> list[dict[str, Any]]:
        """获取可用模型列表"""
        # User API Key 走 OpenAI-compatible /v1/models
        if self.credential_type == "user_api_key":
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.base_url}/v1/models",
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
                return models

        await self._ensure_user_id()
        async with httpx.AsyncClient(timeout=10) as client:
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
        """获取消耗统计"""
        if self.credential_type == "user_api_key":
            raise ValueError("User API Key 无法查询消耗统计，请使用 Cookie 登录方式")
        await self._ensure_user_id()
        import time as _time
        quota_per_unit = await self._get_quota_per_unit()

        async with httpx.AsyncClient(timeout=10) as client:
            # 获取今日消耗
            now = int(_time.time())
            # 今天零点
            from datetime import datetime, timezone
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            start_ts = int(today.timestamp())

            today_cost = 0.0
            try:
                resp = await client.get(
                    f"{self.base_url}/api/log/self/stat",
                    params={
                        "type": 0,
                        "token_name": "",
                        "model_name": "",
                        "start_timestamp": start_ts,
                        "end_timestamp": now,
                        "group": "",
                    },
                    headers=self._headers(),
                )
                data = resp.json()
                if data.get("success", False):
                    stat = data.get("data", {})
                    if isinstance(stat, dict):
                        today_cost = stat.get("quota", 0) / quota_per_unit
                    elif isinstance(stat, (int, float)):
                        today_cost = stat / quota_per_unit
            except Exception:
                pass

            # 获取累计消耗（从 user/self 的 used_quota）
            total_cost = 0.0
            try:
                resp2 = await client.get(
                    f"{self.base_url}/api/user/self",
                    headers=self._headers(),
                )
                data2 = resp2.json()
                if data2.get("success", False):
                    user_data = data2.get("data", {})
                    total_cost = user_data.get("used_quota", 0) / quota_per_unit
            except Exception:
                pass

            return {
                "today_cost": round(today_cost, 4),
                "total_cost": round(total_cost, 4),
                "quota_per_unit": quota_per_unit,
            }

    async def redeem_code(self, code: str) -> dict[str, Any]:
        """兑换码兑换"""
        await self._ensure_user_id()
        quota_per_unit = await self._get_quota_per_unit()

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self.base_url}/api/user/topup",
                json={"key": code},
                headers=self._headers(),
            )
            data = resp.json()
            if not data.get("success", False):
                raise ValueError(f"兑换失败: {data.get('message', '未知错误')}")

            quota = data.get("data", 0)
            if isinstance(quota, dict):
                quota = quota.get("quota", 0)
            value = float(quota) / quota_per_unit
            return {
                "message": "兑换成功",
                "type": "balance",
                "value": round(value, 4),
            }
