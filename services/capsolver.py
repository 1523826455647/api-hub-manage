"""
CapSolver 打码平台集成
用于自动解决 Cloudflare Turnstile 人机验证

API 文档: https://docs.capsolver.com/guide/captcha/cloudflare_turnstile/
流程: createTask → 轮询 getTaskResult → 获取 token
"""
import asyncio
import httpx
from typing import Any

CAPSOLVER_API = "https://api.capsolver.com"


class CapSolverError(Exception):
    pass


async def solve_turnstile(
    api_key: str,
    website_url: str,
    website_key: str,
    timeout: int = 60,
) -> str:
    """
    使用 CapSolver 解决 Cloudflare Turnstile 验证码

    Args:
        api_key: CapSolver API Key
        website_url: 目标站点 URL
        website_key: Turnstile site key
        timeout: 最大等待时间（秒）

    Returns:
        Turnstile token 字符串

    Raises:
        CapSolverError: 解决失败
    """
    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1: 创建任务
        create_resp = await client.post(
            f"{CAPSOLVER_API}/createTask",
            json={
                "clientKey": api_key,
                "task": {
                    "type": "AntiTurnstileTaskProxyLess",
                    "websiteURL": website_url,
                    "websiteKey": website_key,
                },
            },
        )
        create_data = create_resp.json()

        if create_data.get("errorId", 0) != 0:
            raise CapSolverError(
                f"创建任务失败: {create_data.get('errorDescription', create_data.get('errorCode', '未知错误'))}"
            )

        task_id = create_data.get("taskId")
        if not task_id:
            raise CapSolverError("创建任务失败: 未返回 taskId")

        # Step 2: 轮询结果
        elapsed = 0
        poll_interval = 3  # 每 3 秒查询一次

        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            result_resp = await client.post(
                f"{CAPSOLVER_API}/getTaskResult",
                json={
                    "clientKey": api_key,
                    "taskId": task_id,
                },
            )
            result_data = result_resp.json()

            if result_data.get("errorId", 0) != 0:
                raise CapSolverError(
                    f"查询失败: {result_data.get('errorDescription', '未知错误')}"
                )

            status = result_data.get("status", "")

            if status == "ready":
                solution = result_data.get("solution", {})
                token = solution.get("token", "")
                if not token:
                    raise CapSolverError("解决成功但未返回 token")
                return token

            if status == "failed":
                raise CapSolverError("验证码解决失败")

            # status == "processing", 继续等待

        raise CapSolverError(f"超时（{timeout}秒）: 验证码未能解决")


async def get_balance(api_key: str) -> float:
    """查询 CapSolver 账户余额"""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CAPSOLVER_API}/getBalance",
            json={"clientKey": api_key},
        )
        data = resp.json()
        if data.get("errorId", 0) != 0:
            raise CapSolverError(
                f"查询余额失败: {data.get('errorDescription', '未知错误')}"
            )
        return data.get("balance", 0)
