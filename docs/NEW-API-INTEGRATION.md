# Code AI 2API → new-api 对接指南

本网关是 **OpenAI 兼容**的（`/v1/models`、`/v1/chat/completions`、支持 `stream`），
所以在 new-api 里用**「OpenAI」类型渠道**直连即可，不需要写 Go 适配器。

---

## 1. 先把网关跑起来

```bash
cd D:\apk-analysis\aicg332\2api
pip install fastapi uvicorn httpx          # 已装可跳过

# 生产建议：设一个网关密钥，别裸奔
set GATEWAY_KEY=sk-codeai-<你随便定的串>
set CODEAI_POOL_MIN=8                       # 常备空闲账号数
set CODEAI_POOL_MAX=400                     # 池上限

python -m uvicorn server:app --host 127.0.0.1 --port 19031
```

自检三条：

```bash
curl http://127.0.0.1:19031/health
# {"status":"ok","model":"codeai/gpt-4","pool":{"pool_size":8,"total_capacity":40,...}}

curl http://127.0.0.1:19031/v1/models -H "Authorization: Bearer sk-codeai-xxx"

curl -X POST http://127.0.0.1:19031/v1/chat/completions ^
  -H "Authorization: Bearer sk-codeai-xxx" -H "Content-Type: application/json" ^
  -d "{\"model\":\"codeai/gpt-4\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"
```

浏览器打开 `http://127.0.0.1:19031/demo` 有可视化 demo（带账号池实时面板）。

---

## 2. new-api 渠道配置（字段级）

| 字段 | 填什么 | 说明 |
|---|---|---|
| **类型** | `OpenAI`（type=1） | 网关就是 OpenAI 协议 |
| **名称** | `CodeAI-GPT4` | 随意 |
| **Base URL** | `http://127.0.0.1:19031` | **不要**带 `/v1`，new-api 会自己拼 `/v1/chat/completions`。若你的 new-api 版本要求带，就填 `http://127.0.0.1:19031/v1` |
| **密钥** | `sk-codeai-<网关密钥>` | 必须和网关 `GATEWAY_KEY` 一致；网关未设 key 时随便填 |
| **模型** | `codeai/gpt-4` | 单一模型；上游是硬编码 GPT-4 系，填别的名字会被网关原样回显但**不会真的切换到别的模型** |
| **分组** | 按需 | — |
| **模型重定向** | `gpt-4o:codeai/gpt-4`<br>`gpt-4o-mini:codeai/gpt-4`<br>`gpt-4.1:codeai/gpt-4` | 把用户熟悉的名字映射到实际模型，避免客户端报「模型不存在」 |
| **流式** | 开启 | 网关对 `stream:true` 输出标准 SSE（上游本身是非流式，网关切片模拟） |
| **代理** | 留空 | 网关已在本机；**不要**给这条渠道再套代理池 |

### 单价怎么填（重要，防止暴扣）

上游**单次输出 ≤ ≈500 token**，且实际模型是 GPT-4 级但延迟 7–20 s。建议：

- 计费按 **token 计**（网关已按 `len/4` 粗估 usage，能算出数）
- 输入价按 `$0.15 / 1M tokens` 量级
- 输出价按 `$0.60 / 1M tokens` 量级
- 若你按「次」计费，单次建议 `≈500~1000 quota`（取决于你的倍率）

> 千万不要直接套用真 GPT-4 的牌价（$30/1M 输出），否则一次 500 token 的回复会被扣走
> 相当于真 GPT-4 长文的价格，用户体验和账目都会炸。

---

## 3. 验证四步（做完再放量）

1. **渠道测试**：new-api 后台渠道页 →「测试」。若报 `模型不存在`，检查「模型」字段与网关 `/v1/models` 返回是否逐字一致。
2. **单请求**：在 new-api 的「聊天」页发一句 `hello`。返回里应带正文；同时网关日志出现 `minted uid=...`。
3. **额度轮换**：连续发 6 次。前 5 次正常，第 6 次由网关**自动换号**，用户侧应**感知不到**任何错误。
   观察 `curl .../health`，`pool_size` 会随 `retired` 增长而保持不低于 `CODEAI_POOL_MIN`。
4. **流式**：客户端开 `stream=true`，应逐字出现；若一次性蹦出全部文本，说明走的是非流式分支（不是故障）。

---

## 4. 排错表

| 现象 | 原因 | 处理 |
|---|---|---|
| `HTTP 401 invalid gateway key` | new-api 渠道密钥 ≠ 网关 `GATEWAY_KEY` | 改成一致，或清空网关 `GATEWAY_KEY` |
| `HTTP 400 prompt too long` | 输入超过 `CODEAI_MAX_PROMPT_CHARS`（默认 24000 字符） | 调大环境变量，或让客户端截断历史 |
| `HTTP 429 upstream free quota exhausted and no fresh account available` | 账号池被打空 + 铸造失败（多半是 Firebase 侧限流） | 看 `curl /health` 里 `mint_backoff_s`；降到 `CODEAI_POOL_MIN` 或挂代理 |
| `HTTP 502 upstream error 500: No result was returned` | 上游输出超 500 token 被截断 | 客户端让模型「回答简短些」；网关不重试（确定性失败，重试只会烧额度） |
| `HTTP 502 upstream error 409: A previous request is still pending` | 同 uid 串行冲突（网关已换号重试，仍报说明池太小） | 调大 `CODEAI_POOL_MIN` |
| 回复质量突然变差 / 答非所问 | 上游换了模型或加了 system prompt | 协议不用改，属于外部变化 |
| `/health` 显示 `degraded` | 池子空了 | 看网关日志 `mint failed` 原因 |

---

## 5. 容量估算

单个账号：**5 次聊天**，且**严格串行**。

```
每分钟请求数 ≈ 存活账号数 × 60 / 单请求延迟(s)
```

按实测 p50 = 7 s：

| 常备账号 | 理论 QPS | 可持续请求总量 |
|---|---|---|
| 8 | 1.14 | 40 次（用完即需再铸） |
| 40 | 5.7 | 200 次 |
| 200 | 28.6 | 1000 次 |

铸造速率实测 ≈3.2 uid/s，所以**长期稳态**由「铸造速度 vs 消耗速度」决定，而不是池上限。
若客户端请求量 > 3.2 × 60 / 5 ≈ **38 次/分钟** 时，池子会开始供不应求 —— 这时要么降低 `CODEAI_POOL_MIN`
让它更懒地补号，要么放弃这条渠道。

---

## 6. 交接清单

- [x] `codeai_client.py` — 协议 + 匿名 uid 池（懒铸造 / 后台补给 / 429·409·401 全自动换号 / 铸造退避）
- [x] `server.py` — OpenAI 兼容网关（chat / models / health / metrics / demo / SSE）
- [x] `demo.html` — 可视化 demo（实时账号池面板）
- [x] `bench.py` — 压测（mock 管道 + real 上游双模）
- [x] `verify_demo.py` — Playwright 真实浏览器 e2e
- [x] `docs/REVERSE-REPORT.md` — 逆向全证据链
- [x] `docs/NEW-API-INTEGRATION.md` — 本文件
- [ ] new-api 渠道（需你确认后再动核心代码）
