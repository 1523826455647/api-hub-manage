"""服务适配器基类 + 共享 SSL 的 HTTP 客户端工厂"""
import ssl
from abc import ABC, abstractmethod
from typing import Any

import httpx

# ── 共享 SSL context ───────────────────────────────────────────────
# 关键性能修复：httpx.AsyncClient() 默认每次构造都会重建 SSL/CA context，
# 在部分 Windows 环境实测 ~1.8s（同步阻塞，会卡死 asyncio 事件循环）。
# 仪表盘一次刷新会创建约 30 个 client → 串行阻塞数十秒 → 前端一直转圈。
# 进程内只建一次 SSL context，之后所有 client 复用它：
# 单个 client 创建从 ~1.8s 降到 <1ms，同时仍做完整证书校验。
try:
    import certifi
    _CA_FILE = certifi.where()
except Exception:
    _CA_FILE = None

try:
    SSL_CONTEXT: ssl.SSLContext | None = (
        ssl.create_default_context(cafile=_CA_FILE) if _CA_FILE
        else ssl.create_default_context()
    )
except Exception:
    SSL_CONTEXT = None


def new_async_client(timeout: float = 8.0, connect: float = 3.0,
                     **kwargs: Any) -> httpx.AsyncClient:
    """构造复用共享 SSL context 的 httpx 异步客户端。

    读/写/池超时 timeout 秒，连接超时 connect 秒（不可达站点快速失败）。
    返回真正的 AsyncClient，可直接 `async with new_async_client(...) as c`。
    """
    verify = kwargs.pop("verify", SSL_CONTEXT if SSL_CONTEXT is not None else True)
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=connect),
        verify=verify,
        follow_redirects=True,
        headers={"User-Agent": "API-Hub-Manager/1.0"},
        **kwargs,
    )


class BaseAdapter(ABC):
    """中转站服务适配器基类"""

    def __init__(self, base_url: str, access_token: str | None = None,
                 credential_type: str = "token"):
        self.base_url = base_url.rstrip("/")
        self.credential_type = credential_type  # cookie | token | bearer | user_api_key
        # 清洗 token：去掉用户可能粘贴的 "Bearer " 前缀和前后空白
        if access_token:
            t = access_token.strip()
            if t.lower().startswith("bearer "):
                t = t[7:].strip()
            self.access_token = t
        else:
            self.access_token = access_token

    @abstractmethod
    async def login(self, username: str, password: str, turnstile_token: str = "") -> str:
        """登录并返回 access_token"""
        ...

    @abstractmethod
    async def get_balance(self) -> dict[str, Any]:
        """获取余额信息"""
        ...

    @abstractmethod
    async def get_groups(self) -> list[dict[str, Any]]:
        """获取分组列表及倍率"""
        ...

    @abstractmethod
    async def get_models(self) -> list[dict[str, Any]]:
        """获取可用模型及价格"""
        ...

    @abstractmethod
    async def get_usage(self) -> dict[str, Any]:
        """获取消耗统计"""
        ...

    async def redeem_code(self, code: str) -> dict[str, Any]:
        """兑换码兑换（可选实现）"""
        raise NotImplementedError("该平台不支持兑换码功能")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            if self.credential_type == "cookie":
                headers["Cookie"] = self.access_token
            elif self.credential_type in ("bearer", "user_api_key"):
                headers["Authorization"] = f"Bearer {self.access_token}"
            else:
                # 系统令牌 / 裸 token，直接放 Authorization，不加 Bearer
                headers["Authorization"] = self.access_token
        return headers
