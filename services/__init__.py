"""服务适配器基类"""
from abc import ABC, abstractmethod
from typing import Any


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
