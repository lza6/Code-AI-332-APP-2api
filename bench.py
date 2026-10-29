"""Code AI 2API 压测。

    # 本地管道压测（网关起在 CODEAI_MOCK=1 下，不碰真实上游）
    python bench.py --mode mock --concurrency 1,5,10,25,50 --requests 100

    # 真实上游压测（烧真实额度，默认只跑少量）
    python bench.py --mode real --concurrency 2 --requests 6

只报告实跑数字；失败会以非 0 退出码结束。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

DEFAULT_PROMPT = "用一句话解释什么是 HTTP/2 多路复用。"


@dataclass
class Sample:
    ok: bool
    status: int
    latency: float
    chars: int = 0
    error: str = ""
    stream_chunks: int = 0


@dataclass
class Result:
    mode: str
    concurrency: int
    wall_s: float = 0.0
    samples: list[Sample] = field(default_factory=list)

    def summary(self) -> dict:
        lat = sorted(s.latency for s in self.samples)
        ok = [s for s in self.samples if s.ok]
        wall = self.wall_s or (lat[-1] if lat else 0.0)
        pct = lambda p: round(lat[min(len(lat) - 1, int(len(lat) * p))], 3) if lat else 0.0  # noqa: E731
        return {
            "mode": self.mode,
            "concurrency": self.concurrency,
            "requests": len(self.samples),
            "ok": len(ok),
            "fail": len(self.samples) - len(ok),
            "rps": round(len(self.samples) / wall, 2) if wall else 0.0,
            "p50_s": pct(0.50),
            "p90_s": pct(0.90),
            "p95_s": pct(0.95),
            "p99_s": pct(0.99),
            "mean_s": round(statistics.fmean(lat), 3) if lat else 0.0,
            "max_s": round(lat[-1], 3) if lat else 0.0,
            "avg_chars": round(statistics.fmean(s.chars for s in ok), 1) if ok else 0.0,
            "errors": sorted({s.error for s in self.samples if not s.ok})[:6],
        }


async def one_request(client: httpx.AsyncClient, prompt: str, stream: bool, model: str) -> Sample:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
    }
    t0 = time.perf_counter()
    try:
        if not stream:
            resp = await client.post("/v1/chat/completions", json=body)
            dt = time.perf_counter() - t0
            if resp.status_code != 200:
                return Sample(False, resp.status_code, dt, error=f"{resp.status_code}:{resp.text[:120]}")
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            return Sample(True, 200, dt, chars=len(text))
        # 流式：消费完整个 SSE
        chunks = 0
        acc = 0
        async with client.stream("POST", "/v1/chat/completions", json=body) as resp:
            if resp.status_code != 200:
                raw = await resp.aread()
                return Sample(False, resp.status_code, time.perf_counter() - t0, error=f"{resp.status_code}:{raw[:120]!r}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    continue
                try:
                    j = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = j.get("choices", [{}])[0].get("delta", {}).get("content")
                if delta:
                    chunks += 1
                    acc += len(delta)
        return Sample(True, 200, time.perf_counter() - t0, chars=acc, stream_chunks=chunks)
    except Exception as exc:  # noqa: BLE001
        return Sample(False, 0, time.perf_counter() - t0, error=f"{type(exc).__name__}:{exc}"[:120])


async def run_level(base_url: str, mode: str, concurrency: int, requests: int, prompt: str, model: str, stream: bool) -> Result:
    limits = httpx.Limits(max_connections=concurrency + 4, max_keepalive_connections=concurrency + 4)
    timeout = httpx.Timeout(300.0, connect=20.0)
    result = Result(mode, concurrency)
    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=timeout) as client:
        # 预热：让连接池与上游账号池就绪，避免把首次冷启动算进分位
        await one_request(client, prompt, stream, model)
        sem = asyncio.Semaphore(concurrency)

        async def guarded() -> Sample:
            async with sem:
                return await one_request(client, prompt, stream, model)

        t_level = time.perf_counter()
        result.samples = await asyncio.gather(*[guarded() for _ in range(requests)])
        result.wall_s = time.perf_counter() - t_level
    return result


def print_row(s: dict) -> None:
    print(
        f"  conc={s['concurrency']:>3}  n={s['requests']:>4}  ok={s['ok']:>4}  fail={s['fail']:>3}  "
        f"rps={s['rps']:>7.2f}  p50={s['p50_s']:>6.2f}s  p95={s['p95_s']:>6.2f}s  "
        f"p99={s['p99_s']:>6.2f}s  chars={s['avg_chars']:>6.1f}"
    )
    if s["errors"]:
        for e in s["errors"]:
            print(f"        ! {e}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8931")
    ap.add_argument("--mode", default="mock", choices=["mock", "real"])
    ap.add_argument("--concurrency", default="1,5,10")
    ap.add_argument("--requests", type=int, default=60)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--model", default="codeai/gpt-4")
    ap.add_argument("--stream", action="store_true", help="用 SSE 流式路径压测")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    tag = "STREAM" if args.stream else "JSON"
    print(f"[{args.mode.upper()}/{tag}] target={args.base_url} requests/level={args.requests}")

    rows: list[dict] = []
    failures = 0
    for c in levels:
        n = args.requests
        if args.mode == "real":
            # 真实上游：每个 uid 只有 5 次免费额度，别把压测打成批量注册
            n = min(n, 6)
        r = await run_level(args.base_url, args.mode, c, n, args.prompt, args.model, args.stream)
        s = r.summary()
        rows.append(s)
        print_row(s)
        failures += s["fail"]

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        print(f"\nsummary -> {args.json_out}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
