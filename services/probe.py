"""
上游连通性探活

对 NewAPI / Sub2API 等 OpenAI-compatible 端点发送轻量 chat.completions 请求，
返回耗时、状态、错误信息（样式与 Sub2API 探活结果类似）。
"""
from __future__ import annotations

import time
from typing import Any

from . import new_async_client


def _classify_error(http_status: int | None, message: str) -> str:
    msg = (message or "").lower()
    if http_status == 401 or http_status == 403 or "unauthorized" in msg or "invalid api" in msg or "api key" in msg:
        return "auth"
    if http_status == 429 or "rate" in msg or "quota" in msg or "limit" in msg or "负载" in msg or "过载" in msg:
        return "rate_limit"
    if http_status == 404 or "model" in msg and ("not found" in msg or "不存在" in msg or "unknown" in msg):
        return "model"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    return "error"


async def probe_chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    group_name: str = "",
    prompt: str = "ping",
    max_tokens: int = 8,
    timeout: float = 30.0,
    temperature: float = 0,
) -> dict[str, Any]:
    """发送一次最小 chat 请求，探活上游。"""
    base = (base_url or "").rstrip("/")
    model = (model or "").strip()
    api_key = (api_key or "").strip()
    if not base:
        return _fail("error", "缺少 base_url", 0)
    if not api_key:
        return _fail("auth", "缺少上游 API Key", 0)
    if not model:
        return _fail("model", "缺少模型名", 0)

    url = f"{base}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # 部分 NewAPI 分支支持分组头（无副作用；站点不识别会忽略）
    g = (group_name or "").strip()
    if g:
        headers["x-api-group"] = g
        headers["New-Api-Group"] = g

    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt or "ping"}],
        "max_tokens": max(1, int(max_tokens or 8)),
        "temperature": temperature,
        "stream": False,
    }

    t0 = time.perf_counter()
    try:
        async with new_async_client(timeout=float(timeout or 30), connect=5.0) as client:
            resp = await client.post(url, headers=headers, json=body)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        http_status = resp.status_code
        text = resp.text or ""
        try:
            data = resp.json()
        except Exception:
            data = None

        if http_status >= 400:
            msg = ""
            if isinstance(data, dict):
                err = data.get("error") or data.get("message") or data.get("msg") or ""
                if isinstance(err, dict):
                    msg = err.get("message") or err.get("msg") or str(err)
                else:
                    msg = str(err)
            if not msg:
                msg = text[:300] or f"HTTP {http_status}"
            status = _classify_error(http_status, msg)
            return {
                "ok": False,
                "status": status,
                "latency_ms": latency_ms,
                "http_status": http_status,
                "model": model,
                "group_name": g,
                "reply": "",
                "error": msg[:500],
                "usage": {},
                "url": url,
            }

        reply = ""
        usage: dict[str, Any] = {}
        used_model = model
        if isinstance(data, dict):
            usage = data.get("usage") or {}
            used_model = data.get("model") or model
            choices = data.get("choices") or []
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message") or {}
                if isinstance(msg, dict):
                    reply = str(msg.get("content") or "")
                if not reply:
                    # 兼容部分返回 delta / text
                    reply = str(choices[0].get("text") or choices[0].get("content") or "")

        return {
            "ok": True,
            "status": "ok",
            "latency_ms": latency_ms,
            "http_status": http_status,
            "model": used_model,
            "group_name": g,
            "reply": (reply or "").strip()[:500],
            "error": "",
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            },
            "url": url,
        }
    except Exception as e:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        em = str(e)
        status = _classify_error(None, em)
        if "timeout" in em.lower() or "timed out" in em.lower():
            status = "timeout"
        return {
            "ok": False,
            "status": status,
            "latency_ms": latency_ms,
            "http_status": None,
            "model": model,
            "group_name": g,
            "reply": "",
            "error": em[:500],
            "usage": {},
            "url": url,
        }


def _fail(status: str, error: str, latency_ms: int) -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "latency_ms": latency_ms,
        "http_status": None,
        "model": "",
        "group_name": "",
        "reply": "",
        "error": error,
        "usage": {},
        "url": "",
    }
