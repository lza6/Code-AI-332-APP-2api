"""Playwright 真实浏览器 e2e：验证 demo 页在真实浏览器里能跑通。

    python verify_demo.py --url http://127.0.0.1:19032            # mock 网关
    python verify_demo.py --url http://127.0.0.1:19031 --real     # 真实上游

覆盖：页面加载 → /health 连接指示灯变绿 → 输入并发送 → 收到正文 → 截图。
失败以非 0 退出码结束，不做「假装通过」。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:19032")
    ap.add_argument("--prompt", default="用一句话说明什么是幂等性")
    ap.add_argument("--real", action="store_true", help="真实上游：等待时间放宽到 120s")
    ap.add_argument("--screenshot", default="")
    args = ap.parse_args()

    shot = HERE / (args.screenshot or ("demo_real.png" if args.real else "demo_mock.png"))
    wait_ms = 120_000 if args.real else 15_000
    failures: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        console_errors: list[str] = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        with page.expect_response(lambda r: "/health" in r.url, timeout=20_000) as health_info:
            page.goto(args.url + "/demo", wait_until="domcontentloaded")
        health = health_info.value
        if health.status != 200:
            failures.append(f"/health returned {health.status}")
        else:
            try:
                if health.json().get("status") != "ok":
                    failures.append(f"/health not ok: {health.text()[:160]}")
            except Exception:
                failures.append(f"/health not JSON: {health.text()[:160]}")

        # 连接指示灯变绿
        try:
            page.wait_for_selector("#hdot.ok", timeout=15_000)
        except Exception:
            failures.append("health dot did not turn green (#hdot.ok)")

        # 池面板填上了数字
        try:
            page.wait_for_function(
                "() => { const t = document.querySelector('#k-pool').textContent; "
                "return t !== '–' && Number(t) > 0; }",
                timeout=20_000,
            )
        except Exception:
            failures.append("pool panel never showed a positive pool size")

        page.fill("#input", args.prompt)
        page.click("#send")

        # 等 AI 气泡出现非空正文
        try:
            page.wait_for_function(
                "() => { const els = [...document.querySelectorAll('.msg.ai .body')]; "
                "return els.length > 0 && els[els.length-1].textContent.trim().length > 3; }",
                timeout=wait_ms,
            )
        except Exception:
            failures.append(f"no AI reply within {wait_ms} ms")

        ai_text = page.evaluate(
            "() => { const els=[...document.querySelectorAll('.msg.ai .body')];"
            " return els.length ? els[els.length-1].textContent : ''; }"
        )
        user_msgs = page.eval_on_selector_all(".msg.user", "els => els.length")
        if user_msgs < 1:
            failures.append("user bubble missing")

        page.screenshot(path=str(shot), full_page=True)
        browser.close()

    print(f"AI reply ({len(ai_text)} chars): {ai_text[:220].strip()}")
    print(f"screenshot -> {shot}")
    if console_errors:
        print("console errors:", console_errors[:5])
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
