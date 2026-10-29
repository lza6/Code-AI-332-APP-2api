# Code AI 2API

把 **Code AI v3.3.2**（`com.itechgemini.code_ai`，itechgemini）逆向成 **OpenAI 兼容网关**。

上游不是 REST API，而是 **Firebase Cloud Functions callable function**：

```
POST https://us-central1-codeai-6970b.cloudfunctions.net/getOpenAIResponse
Authorization: Bearer <Firebase 匿名账号 idToken>
{"data":{"conversation":[...],"messages":[...],"requestId":"<uuid4>"}}
```

无自定义签名、无设备指纹、无证书固定 —— 纯协议即可打通，**不需要 APK / 真机 / Frida**。
免费额度按 Firebase uid 计（5 次/号，严格串行），网关用**匿名 uid 铸造池**自动换号续量。

> 完整逆向证据链见 [`docs/REVERSE-REPORT.md`](docs/REVERSE-REPORT.md)
> new-api 对接见 [`docs/NEW-API-INTEGRATION.md`](docs/NEW-API-INTEGRATION.md)

---

## 能力边界（先看清楚再接）

| 项 | 实况 |
|---|---|
| 模型 | OpenAI GPT-4 系，**上游硬编码**；请求里的 `model` 被忽略，只做回显 |
| 输出上限 | **≈500 token / ≈1900 字符**；超长请求上游直接返回 500 空结果（不是截断） |
| 延迟 | p50 **6.5–10 s**，长答可达 26 s |
| 流式 | 上游非流式。网关把整段文本切片成标准 SSE 帧（客户端体验是流式，但首个字符仍要等上游返回） |
| 多模态 | **不支持**。原 App 的「图片解释」是设备端 MLKit OCR 后发文本，上游无视觉能力 |
| 多轮 | 支持（`system`/`user`/`assistant` 三种 role 都吃） |
| 工具调用 | 不支持（上游无 function calling） |

这是一条**短问答型**渠道，别拿它跑长文生成 / Agent。

---

## 快速开始

```bash
pip install -r requirements.txt

# 起网关（真实上游）
python -m uvicorn server:app --host 127.0.0.1 --port 19031

# 或起 mock 网关（不碰上游，用于联调/压测）
set CODEAI_MOCK=1
python -m uvicorn server:app --host 127.0.0.1 --port 19032
```

打开 `http://127.0.0.1:19031/demo` 看 demo 页（右侧是实时账号池面板）。

```bash
curl -X POST http://127.0.0.1:19031/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"codeai/gpt-4\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hi\"}]}"
```

---

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/models` | 模型列表（单模型 `codeai/gpt-4`） |
| POST | `/v1/chat/completions` | OpenAI Chat Completions，支持 `stream:true` |
| GET | `/health` | 存活 + 账号池容量（`pool_size` / `total_capacity` / `minted_total` / `mint_backoff_s`） |
| GET | `/metrics` | 账号池逐号明细（配了 `GATEWAY_KEY` 时需要鉴权） |
| GET | `/demo` | demo 页面 |

非流式响应在标准字段之外多一个只读诊断块：

```json
"codeai": {"uid":"…","request_id":"…","remaining_chats":4,"is_pro":false,"latency_s":16.556}
```

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_KEY` | 空 | 设置后 `/v1/*` 与 `/metrics` 要求 `Authorization: Bearer <KEY>` |
| `CODEAI_MODEL_NAME` | `codeai/gpt-4` | 对外暴露的模型名（仅回显） |
| `CODEAI_POOL_MIN` | `4` | 常备空闲账号数（后台 keeper 维持） |
| `CODEAI_POOL_MAX` | `400` | 池上限 |
| `CODEAI_FREE_CHATS` | `5` | 每个 uid 的免费次数（新号初始容量假设） |
| `CODEAI_MAX_PROMPT_CHARS` | `24000` | 输入字符上限，超了直接 400（省额度） |
| `CODEAI_UPSTREAM_TIMEOUT` | `180` | 单次上游超时秒数 |
| `CODEAI_MOCK` | 空 | `1` = 本地合成响应，不碰上游 |
| `CODEAI_MOCK_LATENCY` | `0.25` | mock 模式模拟的上游延迟 |

---

## 账号池行为

- **懒铸造**：`acquire()` 拿不到可用号才现场铸，铸造有全局并发闸门（默认 4）。
- **后台补给**：keeper 每 2 s 检查，空闲号低于 `POOL_MIN` 就铸一个。
- **自动退役**：`remainingChats` 归 0（429 `quota-exhausted`）或 401 且 refresh 失败 → 踢出池。
- **串行保序**：每个账号一把容量 1 的信号量 —— 上游同 uid 并发会 409 `pending`，必须串行。
- **铸造退避**：连续 3 次铸造失败开始指数退避（5 s → 300 s），避免把 Firebase 侧打成异常流量。
- **选号策略**：优先剩余额度多的号，把快用尽的号留作尾巴。

容量公式：

```
每分钟请求数 ≈ 存活账号数 × 60 / 单请求延迟(s)
```

实测铸造速率 ≈ **3.2 uid/s**，所以长期稳态由「铸造速度 vs 消耗速度」决定。

---

## 验证（实跑命令 + 真实数字）

```bash
# 管道压测（mock，0 上游成本）
python bench.py --base-url http://127.0.0.1:19032 --mode mock --concurrency 1,10,25,50 --requests 100

# 真实上游压测（默认每档最多 6 个请求，避免打成批量注册）
python bench.py --base-url http://127.0.0.1:19031 --mode real --concurrency 2,4 --requests 6

# 浏览器 e2e（Playwright，真实点击 + 截图）
python verify_demo.py --url http://127.0.0.1:19031 --real

# 契约回归（30 条断言：models/补全/SSE 帧/校验/鉴权/池统计/demo）
python test_contract.py --url http://127.0.0.1:19032
```

本轮实测结果：

| 场景 | 并发 | 样本 | 成功率 | p50 | p95 |
|---|---|---|---|---|---|
| mock JSON | 1 / 10 / 25 / 50 | 100 × 4 | **400/400** | 0.26–0.34 s | ≤0.48 s |
| mock stream | 1 / 10 / 25 | 60 × 3 | **180/180** | 0.26–0.27 s | ≤0.30 s |
| 真实 JSON | 2 / 4 | 6 × 2 | **12/12** | 6.50 / 9.98 s | 7.84 / 18.77 s |
| 真实 stream | 2 | 4 | **4/4** | 6.42 s | 7.86 s |
| 换号决定性验证（池上限=1，连打 7 次） | 1 | 7 | **7/7** | — | 第 6 次自动换号，`retired=1` |
| 契约回归 `test_contract.py`（30 条断言） | — | mock + real | **ALL PASS / ALL PASS** | — | — |
| Playwright e2e | — | mock + real | **PASS / PASS** | — | — |

> mock 只证明网关管道（并发、换号、SSE 拼帧）正确，**不证明上游可用性**；
> 真实链路单独实测 16 次全绿。

---

## 目录

```
2api/
├── codeai_client.py     # 协议 + 匿名 uid 铸造池
├── server.py            # OpenAI 兼容网关 (FastAPI)
├── demo.html            # demo 页（含实时账号池面板）
├── bench.py             # 压测（mock / real 双模）
├── verify_demo.py       # Playwright 浏览器 e2e
├── test_contract.py     # 网关契约回归（30 条断言）
├── requirements.txt
└── docs/
    ├── REVERSE-REPORT.md
    └── NEW-API-INTEGRATION.md
```

---

## 免责声明

本仓库仅用于**协议研究与互操作性验证**。上游服务、账号、模型所有权归 itechgemini 所有。
批量注册匿名账号以绕过免费额度限制可能违反上游服务条款，由此产生的一切后果由使用者自行承担。
请勿用于商业分发或对外售卖。
