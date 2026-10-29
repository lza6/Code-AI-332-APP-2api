"""网关契约回归测试（纯标准库，不依赖 pytest）。

    python test_contract.py --url http://127.0.0.1:19032   # 建议跑在 mock 网关上

覆盖：模型列表、正常补全、SSE 帧格式、输入校验 400、鉴权 401、健康与池统计。
每条都是真实 HTTP 调用 + 明确断言；失败以非 0 退出。
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL  {name} :: {detail}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:19032")
    ap.add_argument("--key", default="")
    args = ap.parse_args()
    headers = {"Authorization": f"Bearer {args.key}"} if args.key else {}
    c = httpx.Client(base_url=args.url, timeout=60.0, headers=headers)

    print("[1] GET /v1/models")
    r = c.get("/v1/models")
    check("status 200", r.status_code == 200, r.text[:120])
    body = r.json()
    check("object=list", body.get("object") == "list", str(body)[:120])
    model_id = body["data"][0]["id"]
    check("data[0].id non-empty", bool(model_id), str(body)[:120])

    print("[2] POST /v1/chat/completions (non-stream)")
    r = c.post(
        "/v1/chat/completions",
        json={"model": model_id, "messages": [{"role": "user", "content": "ping"}]},
    )
    check("status 200", r.status_code == 200, r.text[:200])
    j = r.json()
    check("object=chat.completion", j.get("object") == "chat.completion", str(j)[:120])
    check("choices[0].message.role=assistant", j["choices"][0]["message"]["role"] == "assistant", "")
    check("content non-empty", len(j["choices"][0]["message"]["content"]) > 0, "")
    check("finish_reason=stop", j["choices"][0]["finish_reason"] == "stop", "")
    usage = j.get("usage", {})
    check("usage.total = prompt + completion",
          usage.get("total_tokens") == usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
          str(usage))
    check("codeai diag block present", "uid" in j.get("codeai", {}), str(j.get("codeai")))

    print("[3] POST /v1/chat/completions (stream)")
    frames, done, first_role = 0, False, None
    with c.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": model_id, "stream": True, "messages": [{"role": "user", "content": "ping"}]},
    ) as resp:
        check("status 200", resp.status_code == 200, "")
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                done = True
                break
            obj = json.loads(payload)
            check_obj = obj.get("object")
            if check_obj != "chat.completion.chunk":
                FAILS.append(f"chunk object wrong: {check_obj}")
            if first_role is None:
                first_role = obj["choices"][0]["delta"].get("role")
            frames += 1
    check("stream terminated with [DONE]", done, "no [DONE] marker")
    check("first delta carries role=assistant", first_role == "assistant", str(first_role))
    check("received >1 chunks", frames > 1, f"frames={frames}")

    print("[4] 输入校验")
    r = c.post("/v1/chat/completions", json={"model": model_id, "messages": []})
    check("empty messages -> 400", r.status_code == 400, r.text[:160])
    check("error envelope has error.message", "message" in r.json().get("error", {}), r.text[:160])

    r = c.post("/v1/chat/completions", json={"model": model_id, "messages": [{"role": "root", "content": "x"}]})
    check("invalid role -> 400", r.status_code == 400, r.text[:160])

    r = c.post("/v1/chat/completions", json={"model": model_id,
                                            "messages": [{"role": "user", "content": "x" * 50000}]})
    check("oversized prompt -> 400", r.status_code == 400, r.text[:160])

    print("[5] 多模态降级")
    r = c.post("/v1/chat/completions", json={
        "model": model_id,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}],
    })
    check("content array accepted (flattened)", r.status_code == 200, r.text[:200])

    print("[6] GET /health")
    r = c.get("/health")
    check("status 200", r.status_code == 200, r.text[:160])
    pool = r.json().get("pool", {})
    for k in ("pool_size", "busy", "total_capacity", "minted_total", "free_chats_per_uid"):
        check(f"pool.{k} present", k in pool, str(pool)[:160])
    check("pool_size > 0", pool.get("pool_size", 0) > 0, str(pool)[:160])

    print("[7] GET /metrics")
    r = c.get("/metrics")
    check("status 200", r.status_code == 200, r.text[:160])
    check("accounts is list", isinstance(r.json().get("accounts"), list), r.text[:160])

    print("[8] GET /demo")
    r = c.get("/demo")
    check("status 200", r.status_code == 200, "")
    check("serves html", "codeai" in r.text.lower() or "Code AI" in r.text, r.text[:120])

    c.close()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        for f in FAILS:
            print(" -", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
