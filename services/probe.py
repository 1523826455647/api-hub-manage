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
    if http_status == 404 or ("model" in msg and ("not found" in msg or "不存在" in msg or "unknown" in msg)):
        return "model"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    return "error"


def _is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _safe_error_text(err: BaseException | str) -> str:
    try:
        text = str(err) if not isinstance(err, str) else err
    except Exception:
        text = repr(err)
    return text.replace("\x00", "")[:500]


def _ascii_headers(headers: dict[str, str]) -> dict[str, str]:
    """严格过滤：任何非 ASCII 头一律丢弃，杜绝 httpx/httpcore 的 ascii encode 崩溃。"""
    out: dict[str, str] = {}
    for k, v in headers.items():
        ks, vs = str(k), str(v)
        if _is_ascii(ks) and _is_ascii(vs):
            out[ks] = vs
    return out


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
    g = (group_name or "").strip()

    if not base:
        return _fail("error", "缺少 base_url", 0, g)
    if not api_key:
        return _fail("auth", "缺少上游 API Key", 0, g)
    if not model:
        return _fail("model", "缺少模型名", 0, g)

    url = f"{base}/v1/chat/completions"

    # Header 只放纯 ASCII。中文分组名绝不进 header（会触发
    # 'ascii' codec can't encode characters ...）。
    raw_headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if g and _is_ascii(g):
        raw_headers["x-api-group"] = g
        raw_headers["New-Api-Group"] = g
    headers = _ascii_headers(raw_headers)

    # JSON body 是 UTF-8，中文 prompt / model / group 都安全
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": str(prompt or "ping")}],
        "max_tokens": max(1, int(max_tokens or 8)),
        "temperature": float(temperature or 0),
        "stream": False,
    }
    if g:
        body["group"] = g

    t0 = time.perf_counter()
    try:
        async with new_async_client(timeout=float(timeout or 30), connect=5.0) as client:
            resp = await client.post(url, headers=headers, json=body)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        http_status = resp.status_code
        text = ""
        try:
            text = resp.text or ""
        except Exception:
            text = ""
        data = None
        try:
            data = resp.json()
        except Exception:
            data = None

        if http_status >= 400:
            msg = ""
            if isinstance(data, dict):
                err = data.get("error") or data.get("message") or data.get("msg") or ""
                if isinstance(err, dict):
                    msg = str(err.get("message") or err.get("msg") or err)
                else:
                    msg = str(err)
            if not msg:
                # 响应体可能是 HTML，截断并保证可返回
                msg = (text[:300] if text else f"HTTP {http_status}")
            return {
                "ok": False,
                "status": _classify_error(http_status, msg),
                "latency_ms": latency_ms,
                "http_status": http_status,
                "model": model,
                "group_name": g,
                "reply": "",
                "error": _safe_error_text(msg),
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
                msg_obj = choices[0].get("message") or {}
                if isinstance(msg_obj, dict):
                    reply = str(msg_obj.get("content") or "")
                if not reply:
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
    except UnicodeEncodeError as e:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "ok": False,
            "status": "error",
            "latency_ms": latency_ms,
            "http_status": None,
            "model": model,
            "group_name": g,
            "reply": "",
            "error": (
                "编码错误（请求含非 ASCII 字符被错误放入 HTTP 头）。"
                f"详情: {_safe_error_text(e)}。"
                "请重启 python main.py 后再试；中文分组请依赖 body.group / 分组绑定的 sk- Key。"
            ),
            "usage": {},
            "url": url,
        }
    except Exception as e:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        em = _safe_error_text(e)
        status = _classify_error(None, em)
        if "timeout" in em.lower() or "timed out" in em.lower():
            status = "timeout"
        if "codec can't encode" in em.lower() or "ordinal not in range" in em.lower():
            status = "error"
            em = (
                "请求编码失败（多为中文分组名写入了 HTTP 头）。"
                "已在新版本修复：请彻底关闭旧 python 进程后重启服务再探活。"
                f" 原始错误: {em}"
            )
        return {
            "ok": False,
            "status": status,
            "latency_ms": latency_ms,
            "http_status": None,
            "model": model,
            "group_name": g,
            "reply": "",
            "error": em,
            "usage": {},
            "url": url,
        }


def _fail(status: str, error: str, latency_ms: int, group_name: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "latency_ms": latency_ms,
        "http_status": None,
        "model": "",
        "group_name": group_name or "",
        "reply": "",
        "error": error,
        "usage": {},
        "url": "",
    }
