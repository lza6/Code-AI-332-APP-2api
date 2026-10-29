"""Code AI 2API — OpenAI 兼容网关.

    GET  /v1/models              模型列表
    POST /v1/chat/completions    OpenAI Chat Completions（支持 stream）
    GET  /health                 存活 + 账号池容量
    GET  /demo                   demo 页面
    GET  /metrics                账号池明细（需 admin key，未配置则公开）

启动：
    python -m uvicorn server:app --host 127.0.0.1 --port 8931
环境变量：
    GATEWAY_KEY      设置后 /v1/* 需要 Authorization: Bearer <GATEWAY_KEY>
    CODEAI_POOL_MIN  常备空闲账号数（默认 4）
    CODEAI_POOL_MAX  池上限（默认 400）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from codeai_client import (
    AccountPool,
    AccountAuthError,
    QuotaExhausted,
    UpstreamError,
    MAX_PROMPT_CHARS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("gateway")

GATEWAY_KEY = os.getenv("GATEWAY_KEY", "").strip()
PUBLIC_MODEL = os.getenv("CODEAI_MODEL_NAME", "codeai/gpt-4")
POOL_MIN = int(os.getenv("CODEAI_POOL_MIN", "4"))
POOL_MAX = int(os.getenv("CODEAI_POOL_MAX", "400"))

HERE = Path(__file__).resolve().parent
pool = AccountPool(min_idle=POOL_MIN, max_size=POOL_MAX)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await pool.start()
    log.info("gateway ready: pool_min=%d pool_max=%d model=%s", POOL_MIN, POOL_MAX, PUBLIC_MODEL)
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="Code AI 2API", version="1.0.0", lifespan=lifespan)


def _authorize(authorization: str | None) -> None:
    if not GATEWAY_KEY:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization.removeprefix("Bearer ").strip() != GATEWAY_KEY:
        raise HTTPException(status_code=401, detail="invalid gateway key")


def _openai_error(status: int, message: str, code: str = "upstream_error") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": code, "code": code, "param": None}},
    )


def _estimate_tokens(text: str) -> int:
    """上游不返回 usage，用 4 字符 ≈ 1 token 粗估，仅供计费占位。"""
    return max(1, round(len(text) / 4))


def _flatten_content(content: Any) -> str:
    """OpenAI content 可能是字符串，也可能是 [{type:text,text:...}, ...] 数组。

    上游只接受字符串内容（传数组会被判 'Valid messages are required'），
    所以这里把多模态分段拍平成纯文本，并把图片段降级为占位说明。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for seg in content:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type")
            if seg_type in ("text", "input_text") and isinstance(seg.get("text"), str):
                parts.append(seg["text"])
            elif seg_type in ("image_url", "input_image"):
                parts.append("[image omitted: upstream function is text-only]")
        return "\n".join(parts)
    return str(content)


def _to_conversation(messages: Any) -> list[dict[str, str]]:
    """把 OpenAI messages 转成上游 conversation。

    上游接受 user/assistant/system 三种 role，tool/function 角色统一降级为 user。
    """
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty array")
    conversation: list[dict[str, str]] = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise ValueError(f"messages[{idx}] must be an object")
        role = msg.get("role")
        if role not in ("system", "user", "assistant", "tool", "function", "developer"):
            raise ValueError(f"messages[{idx}].role invalid: {role!r}")
        text = _flatten_content(msg.get("content"))
        if role in ("tool", "function"):
            role = "user"
        if role == "developer":
            role = "system"
        if not text.strip() and role == "user":
            continue
        conversation.append({"role": role, "content": text})
    if not conversation:
        raise ValueError("messages produced an empty conversation")
    if conversation[-1]["role"] != "user":
        conversation.append({"role": "user", "content": "Continue."})
    total = sum(len(m["content"]) for m in conversation)
    if total > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt too long: {total} chars > {MAX_PROMPT_CHARS}")
    return conversation


@app.get("/v1/models")
async def list_models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _authorize(authorization)
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": PUBLIC_MODEL,
                "object": "model",
                "created": now,
                "owned_by": "itechgemini-codeai",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(default=None)):
    _authorize(authorization)
    try:
        body = await request.json()
    except Exception:
        return _openai_error(400, "invalid JSON body", "invalid_request_error")

    try:
        conversation = _to_conversation(body.get("messages"))
    except ValueError as exc:
        return _openai_error(400, str(exc), "invalid_request_error")

    stream = bool(body.get("stream"))
    want_model = body.get("model") or PUBLIC_MODEL
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    try:
        result = await pool.chat(conversation)
    except QuotaExhausted:
        return _openai_error(429, "upstream free quota exhausted and no fresh account available", "rate_limit_exceeded")
    except AccountAuthError as exc:
        return _openai_error(502, f"upstream auth failed: {exc.message}", "upstream_auth_error")
    except UpstreamError as exc:
        status = 429 if exc.status == 429 else (503 if exc.status >= 500 else 502)
        return _openai_error(status, f"upstream error {exc.status}: {exc.message}")
    except Exception as exc:  # noqa: BLE001 - 兜底，避免 500 裸栈
        log.exception("unexpected gateway error")
        return _openai_error(500, f"gateway error: {exc!r}")

    text = result["text"]
    prompt_tokens = _estimate_tokens(json.dumps(conversation, ensure_ascii=False))
    completion_tokens = _estimate_tokens(text)

    if not stream:
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": want_model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "codeai": {
                "uid": result["uid"],
                "request_id": result["request_id"],
                "remaining_chats": result["remaining_chats"],
                "is_pro": result["is_pro"],
                "latency_s": result["latency_s"],
            },
        }

    async def event_stream():
        base = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": want_model,
        }
        first = {"role": "assistant", "content": ""}
        yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': first, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
        # 上游是非流式的一次性 JSON，这里按 32 字符切片模拟增量输出
        step = 32
        for i in range(0, len(text), step):
            chunk = {
                **base,
                "choices": [{"index": 0, "delta": {"content": text[i : i + step]}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0)
        final = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    stats = pool.stats()
    ok = stats["pool_size"] > 0
    return {"status": "ok" if ok else "degraded", "model": PUBLIC_MODEL, "pool": stats}


@app.get("/metrics")
async def metrics(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _authorize(authorization)
    return {"pool": pool.stats(), "accounts": pool.accounts_snapshot(limit=30)}


@app.get("/", response_class=HTMLResponse)
@app.get("/demo", response_class=HTMLResponse)
async def demo() -> HTMLResponse:
    page = HERE / "demo.html"
    if not page.exists():
        return HTMLResponse("<h1>demo.html missing</h1>", status_code=404)
    return HTMLResponse(page.read_text(encoding="utf-8"))
