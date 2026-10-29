"""Code AI (com.itechgemini.code_ai v3.3.2) 上游协议客户端 + 匿名账号池.

上游不是 REST，而是 Firebase Cloud Functions 的可调用函数 (callable function)：
    https://us-central1-codeai-6970b.cloudfunctions.net/getOpenAIResponse
鉴权只认 Firebase 匿名账号签发的 idToken，无自定义签名 / 无设备指纹 / 无证书固定。

免费额度按「Firebase uid」计：每个 uid 5 次聊天、15 次视觉尝试，且同 uid 严格串行
（并发同号一律 409 pending）。因此吞吐模型 = 账号数量 × (1 / 单请求延迟)。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("codeai")

# --- 静态还原出的常量（来自 resources.arsc + libapp.so 字符串表） ---------------
FIREBASE_API_KEY = "AIzaSyApRibMUSjrvy2tgRgRzPLSEG-ZVuU9Xl8"
FIREBASE_PROJECT_ID = "codeai-6970b"
FIREBASE_PROJECT_NUMBER = "185515517167"
FIREBASE_APP_ID = "1:185515517167:android:a311e6353b297592f527"
FUNCTIONS_REGION = "us-central1"
FUNCTION_NAME = "getOpenAIResponse"

FN_URL = f"https://{FUNCTIONS_REGION}-{FIREBASE_PROJECT_ID}.cloudfunctions.net/{FUNCTION_NAME}"
SIGNUP_URL = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_API_KEY}"
REFRESH_URL = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"

# 每个匿名 uid 的初始免费聊天次数（实测剩余 4 -> 3 -> 2 -> 1 -> 0，第 6 次 429）
FREE_CHATS_PER_UID = int(os.getenv("CODEAI_FREE_CHATS", "5"))

# 上游把输出卡在 ~1900 字符（≈500 token）；超长请求上游直接 500 空结果，
# 所以这里主动截断提示词长度，避免把可用的额度烧在必然失败的长请求上。
MAX_PROMPT_CHARS = int(os.getenv("CODEAI_MAX_PROMPT_CHARS", "24000"))

HTTP_TIMEOUT = float(os.getenv("CODEAI_UPSTREAM_TIMEOUT", "180"))

# 压测/联调用：不碰真实上游，只在本地合成响应。只证明网关管道逻辑，不证明上游可用性。
MOCK_UPSTREAM = os.getenv("CODEAI_MOCK", "").strip() in ("1", "true", "yes")
MOCK_LATENCY_S = float(os.getenv("CODEAI_MOCK_LATENCY", "0.25"))


class UpstreamError(Exception):
    """上游业务错误。status 为上游 HTTP 码，reason 为 details.reason（可能为空）。"""

    def __init__(self, status: int, message: str, reason: str = "", pending: bool = False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.reason = reason
        self.pending = pending


class QuotaExhausted(UpstreamError):
    """该 uid 的免费聊天额度用尽（HTTP 429 / reason=quota-exhausted）。"""


class AccountAuthError(UpstreamError):
    """idToken 失效或账号被删（HTTP 401 / UNAUTHENTICATED）。"""


@dataclass
class Account:
    uid: str
    id_token: str
    refresh_token: str
    token_expires_at: float
    remaining_chats: int = FREE_CHATS_PER_UID
    vision_tries: int = 15
    is_pro: bool = False
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    served: int = 0
    dead: bool = False
    dead_reason: str = ""
    # 上游同 uid 严格串行，用 1 容量信号量表达「这个号此刻只能跑一个请求」
    lock: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))

    @property
    def alive(self) -> bool:
        return not self.dead and self.remaining_chats > 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "remaining_chats": self.remaining_chats,
            "vision_tries": self.vision_tries,
            "is_pro": self.is_pro,
            "served": self.served,
            "dead": self.dead,
            "dead_reason": self.dead_reason,
            "age_s": round(time.time() - self.created_at, 1),
            "token_ttl_s": max(0, round(self.token_expires_at - time.time())),
        }


class AccountPool:
    """匿名 uid 铸造池。

    - 懒铸造：acquire() 拿不到可用号时现场铸造，铸造过程有全局并发闸门。
    - 后台补给：keeper() 周期性把空闲号补到 min_idle。
    - 号失效自动出池：额度耗尽 / 401 无法刷新 → dead。
    """

    def __init__(
        self,
        min_idle: int = 4,
        max_size: int = 400,
        mint_concurrency: int = 4,
    ) -> None:
        self.min_idle = min_idle
        self.max_size = max_size
        self._mint_gate = asyncio.Semaphore(mint_concurrency)
        self._accounts: list[Account] = []
        self._guard = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._keeper_task: asyncio.Task[None] | None = None
        self._mint_failures = 0
        self._mint_backoff_until = 0.0
        self.minted_total = 0
        self.mint_failed_total = 0
        self.retired_total = 0

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(HTTP_TIMEOUT, connect=20.0),
            headers={
                "User-Agent": "okhttp/4.12.0",
                "Accept-Encoding": "gzip",
                "Content-Type": "application/json",
            },
            follow_redirects=True,
        )
        await asyncio.gather(*[self.mint() for _ in range(self.min_idle)], return_exceptions=True)
        self._keeper_task = asyncio.create_task(self._keeper())

    async def close(self) -> None:
        if self._keeper_task:
            self._keeper_task.cancel()
            try:
                await self._keeper_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()

    # ---------------------------------------------------------------- 铸造/刷新
    async def mint(self) -> Account | None:
        """铸造一个全新匿名 uid（= 一份全新的 5 次额度）。"""
        assert self._client is not None
        if time.time() < self._mint_backoff_until:
            return None
        async with self._mint_gate:
            async with self._guard:
                if len(self._live_accounts()) >= self.max_size:
                    return None
            if MOCK_UPSTREAM:
                acct = Account(
                    uid=f"mock-{uuid.uuid4().hex[:12]}",
                    id_token="mock-token",
                    refresh_token="mock-refresh",
                    token_expires_at=time.time() + 3000,
                )
                async with self._guard:
                    self._accounts.append(acct)
                self.minted_total += 1
                return acct
            try:
                resp = await self._client.post(SIGNUP_URL, json={"returnSecureToken": True})
                body = resp.json()
            except Exception as exc:  # noqa: BLE001 - 网络异常统一降级
                self._on_mint_failure(f"mint network error: {exc!r}")
                return None
            if resp.status_code != 200 or "idToken" not in body:
                self._on_mint_failure(f"mint http {resp.status_code}: {str(body)[:200]}")
                return None
            acct = Account(
                uid=body["localId"],
                id_token=body["idToken"],
                refresh_token=body.get("refreshToken", ""),
                token_expires_at=time.time() + float(body.get("expiresIn", 3600)) - 60,
            )
            async with self._guard:
                self._accounts.append(acct)
            self.minted_total += 1
            self._mint_failures = 0
            log.info("minted uid=%s pool=%d", acct.uid[:8], len(self._live_accounts()))
            return acct

    def _on_mint_failure(self, msg: str) -> None:
        self.mint_failed_total += 1
        self._mint_failures += 1
        # 连续失败指数退避，避免把 Firebase 侧打成异常流量
        if self._mint_failures >= 3:
            backoff = min(300.0, 5.0 * 2 ** (self._mint_failures - 3))
            self._mint_backoff_until = time.time() + backoff
            log.warning("mint backoff %.0fs after %d failures (%s)", backoff, self._mint_failures, msg)
        else:
            log.warning("mint failed: %s", msg)

    async def _refresh(self, acct: Account) -> bool:
        """用 refreshToken 换新 idToken。uid 与额度都会保留。"""
        assert self._client is not None
        if not acct.refresh_token:
            return False
        try:
            resp = await self._client.post(
                REFRESH_URL,
                data={"grant_type": "refresh_token", "refresh_token": acct.refresh_token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("refresh network error for %s: %r", acct.uid[:8], exc)
            return False
        if resp.status_code != 200 or "id_token" not in body:
            log.warning("refresh failed for %s: %s", acct.uid[:8], str(body)[:160])
            return False
        acct.id_token = body["id_token"]
        acct.refresh_token = body.get("refresh_token", acct.refresh_token)
        acct.token_expires_at = time.time() + float(body.get("expires_in", 3600)) - 60
        return True

    # -------------------------------------------------------------------- 取号
    def _live_accounts(self) -> list[Account]:
        return [a for a in self._accounts if a.alive]

    async def acquire(self, mint_if_needed: bool = True) -> Account:
        """取一个「空闲且有余量」的账号并独占它，直到 release()。

        顺序：优先剩余额度多的号（把快死的号留作尾巴），避免每次都在半死号上撞 429。
        """
        while True:
            async with self._guard:
                candidates = sorted(
                    (a for a in self._live_accounts() if a.lock.locked() is False),
                    key=lambda a: (-a.remaining_chats, a.last_used_at),
                )
            for acct in candidates:
                # 非阻塞抢占；抢不到说明被别的请求拿去串行了
                if acct.lock.locked():
                    continue
                await acct.lock.acquire()
                if not acct.alive:  # 抢占后二次确认（期间可能被标记死亡）
                    acct.lock.release()
                    continue
                if acct.token_expires_at <= time.time() and not await self._refresh(acct):
                    self._kill(acct, "token refresh failed")
                    acct.lock.release()
                    continue
                acct.last_used_at = time.time()
                return acct
            if not mint_if_needed:
                raise UpstreamError(503, "no account available")
            minted = await self.mint()
            if minted is None:
                if not self._live_accounts():
                    raise UpstreamError(503, "account pool empty and mint is failing")
                await asyncio.sleep(0.35)
            # 已有别的可用号时让它重新进循环（避免同步阻塞式铸造）

    def release(self, acct: Account) -> None:
        acct.last_used_at = time.time()
        try:
            acct.lock.release()
        except ValueError:
            pass

    def _kill(self, acct: Account, reason: str) -> None:
        if not acct.dead:
            acct.dead = True
            acct.dead_reason = reason
            self.retired_total += 1
            log.info("account %s retired: %s", acct.uid[:8], reason)

    def mark_exhausted(self, acct: Account) -> None:
        acct.remaining_chats = 0
        self._kill(acct, "quota-exhausted")

    # ------------------------------------------------------------------ 守护
    async def _keeper(self) -> None:
        while True:
            try:
                idle = [a for a in self._live_accounts() if not a.lock.locked()]
                await self._guard.acquire()
                try:
                    # 掉出池子的号清掉，避免内存里堆尸体
                    self._accounts = [a for a in self._accounts if a.alive]
                finally:
                    self._guard.release()
                if len(idle) < self.min_idle:
                    await self.mint()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("keeper error: %r", exc)
            await asyncio.sleep(2.0)

    # ------------------------------------------------------------------ 调用
    async def chat(self, conversation: list[dict[str, Any]], max_retries: int = 4) -> dict[str, Any]:
        """跑一次上游聊天。自动换号、自动处理 429/409/401。"""
        assert self._client is not None
        last_error: UpstreamError | None = None
        for attempt in range(max_retries):
            acct = await self.acquire()
            try:
                return await self._chat_once(acct, conversation)
            except QuotaExhausted as exc:
                self.mark_exhausted(acct)
                last_error = exc
            except AccountAuthError as exc:
                if not await self._refresh(acct):
                    self._kill(acct, "auth rejected")
                last_error = exc
                await asyncio.sleep(0.2)
            except UpstreamError as exc:
                if exc.pending:
                    # 409 pending：这个号手上还有别的在飞，换号重试
                    last_error = exc
                    await asyncio.sleep(0.15)
                elif exc.status >= 500:
                    # 上游 500 多数是「输出超长被截断」或函数冷启动抖动，属于确定性失败
                    last_error = exc
                else:
                    raise
            finally:
                self.release(acct)
            if attempt == max_retries - 1:
                break
        raise last_error or UpstreamError(502, "upstream failed")

    async def _chat_once(self, acct: Account, conversation: list[dict[str, Any]]) -> dict[str, Any]:
        assert self._client is not None
        if MOCK_UPSTREAM:
            await asyncio.sleep(MOCK_LATENCY_S)
            acct.served += 1
            acct.remaining_chats -= 1
            if acct.remaining_chats <= 0:
                self._kill(acct, "quota-exhausted")
            last = conversation[-1]["content"] if conversation else ""
            return {
                "text": f"[mock] echo: {last[:120]}",
                "request_id": str(uuid.uuid4()),
                "remaining_chats": max(0, acct.remaining_chats),
                "rewarded_credits": 0,
                "is_pro": False,
                "uid": acct.uid,
                "latency_s": MOCK_LATENCY_S,
            }
        payload = {
            "data": {
                "conversation": conversation,
                "messages": conversation,
                "requestId": str(uuid.uuid4()),
            }
        }
        started = time.time()
        try:
            resp = await self._client.post(
                FN_URL,
                json=payload,
                headers={"Authorization": f"Bearer {acct.id_token}"},
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(504, f"upstream transport error: {exc!r}") from exc

        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 - 上游 404/500 会回 HTML
            raise UpstreamError(resp.status_code, resp.text[:200] or "non-json response") from None

        if resp.status_code == 200 and isinstance(body, dict) and "result" in body:
            result = body["result"]
            acct.served += 1
            if isinstance(result.get("remainingChats"), int):
                acct.remaining_chats = result["remainingChats"]
                if acct.remaining_chats <= 0:
                    self._kill(acct, "quota-exhausted")
            if isinstance(result.get("visionApiTries"), int):
                acct.vision_tries = result["visionApiTries"]
            acct.is_pro = bool(result.get("isPro", False))
            return {
                "text": result.get("response", "") or "",
                "request_id": result.get("requestId"),
                "remaining_chats": result.get("remainingChats"),
                "rewarded_credits": result.get("rewardedCredits"),
                "is_pro": acct.is_pro,
                "uid": acct.uid,
                "latency_s": round(time.time() - started, 3),
            }

        err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
        message = err.get("message") or (resp.text[:200] if resp.text else "upstream error")
        status = err.get("status", "")
        details = err.get("details") or {}
        reason = details.get("reason", "")
        if reason == "quota-exhausted" or resp.status_code == 429:
            raise QuotaExhausted(resp.status_code, message, reason)
        if resp.status_code == 401 or status == "UNAUTHENTICATED":
            raise AccountAuthError(resp.status_code, message, reason)
        raise UpstreamError(
            resp.status_code,
            message,
            reason,
            pending=bool(details.get("pending")),
        )

    # ------------------------------------------------------------------ 观测
    def stats(self) -> dict[str, Any]:
        live = self._live_accounts()
        return {
            "pool_size": len(live),
            "busy": sum(1 for a in live if a.lock.locked()),
            "total_capacity": sum(a.remaining_chats for a in live),
            "consumed_capacity": sum(FREE_CHATS_PER_UID - a.remaining_chats for a in live),
            "retired": self.retired_total,
            "minted_total": self.minted_total,
            "mint_failed_total": self.mint_failed_total,
            "mint_backoff_s": max(0, round(self._mint_backoff_until - time.time(), 1)),
            "min_idle": self.min_idle,
            "max_size": self.max_size,
            "free_chats_per_uid": FREE_CHATS_PER_UID,
        }

    def accounts_snapshot(self, limit: int = 20) -> list[dict[str, Any]]:
        return [a.snapshot() for a in itertools.islice(self._accounts, limit)]
