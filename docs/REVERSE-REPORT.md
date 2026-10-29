# Code AI v3.3.2 逆向报告（第一轮）

目标：`ai-code-generator-v3-3-2-mod-apkvision.apk`（29.5 MB）
包名：`com.itechgemini.code_ai`
发行方：itechgemini（支持邮箱 `support@itechgemini.com`）
分析日期：本轮会话
产物位置：`D:\apk-analysis\aicg332\`

---

## 0. 结论摘要

| 项 | 结论 | 标签 |
|---|---|---|
| 后端形态 | Firebase Cloud Functions **callable function**，单端点 | 已验证 |
| 上游地址 | `https://us-central1-codeai-6970b.cloudfunctions.net/getOpenAIResponse` | 已验证 |
| 鉴权 | Firebase 匿名账号 `idToken`（`Authorization: Bearer`） | 已验证 |
| 是否需要 APK/真机/Frida | **不需要**。纯协议即可打通 | 已验证 |
| 签名 / 设备指纹 / 证书固定 | 全部无 | 已验证（500 个候选函数名 + 全字段探测均无签名报错路径） |
| 模型 | OpenAI GPT-4 系（自述 GPT-4 architecture，知识截止 2023-09），**上游硬编码，`model` 参数被忽略** | 已验证 |
| 免费额度 | 每 Firebase uid **5 次**聊天；`visionApiTries` 15 | 已验证 |
| 并发模型 | 同 uid **严格串行**（并发第二个请求 409 `pending`） | 已验证 |
| 输出上限 | ≈1900 字符 / ≈500 token；超长请求上游直接 500 空结果 | 已验证 |
| 实测延迟 | p50 6.5–10 s（短答）；长答可达 26 s | 已验证 |
| 免费多模态 | 无。图片走设备端 MLKit OCR，之后只发文本 | 静态确认 + 行为验证 |

**一句话**：这是一个「GPT-4 套壳的代码问答 App」，服务端只有 1 个 Firebase 函数，鉴权只靠 Firebase 匿名 token，
因此匿名 uid 铸造池 = 无限免费额度，协议面极薄，适合做 2API 渠道。

---

## 1. 静态侦察

### 1.1 结构

```
raw/
├── classes.dex / classes2.dex          (7.6M + 2.9M, 基本都是 Flutter/Ads/Firebase 插件)
├── lib/arm64-v8a/
│   ├── libapp.so          7.2M   ← Dart AOT 快照，业务逻辑全在这里
│   ├── libflutter.so     10.1M
│   └── libmlkit_google_ocr_pipeline.so 9.2M
└── assets/flutter_assets/  (assets/images 145 张 png, lottie 动画, 无任何配置文件)
```

判定：**Flutter (Dart AOT)** App。业务逻辑不在 dex 里，密码学常量/接口地址都在 `libapp.so`。

### 1.2 Firebase 配置（`resources.arsc` 字符串池直接可读）

| 键 | 值 |
|---|---|
| `google_api_key` | `AIzaSyApRibMUSjrvy2tgRgRzPLSEG-ZVuU9Xl8` |
| `google_app_id` | `1:185515517167:android:a311e6353b297592f527` |
| `project_id`（由 idToken 的 `aud`/`iss` 反推确认） | `codeai-6970b` |
| project number | `185515517167` |

> `project_id` 在 arsc 里没有明文，是通过匿名登录后的 idToken JWT 载荷反解出来的：
> `{"iss":"https://securetoken.google.com/codeai-6970b","aud":"codeai-6970b",...}`。

### 1.3 从 `libapp.so` 抽出的业务骨架

`strings`（自写 Python 扫描器，18408 条 ASCII 串）逐条过滤后：

- 业务 Dart 包：`package:code_ai/...`，共 41 个源文件，关键几个：
  - `services/firebase_services.dart` ← 网络层
  - `services/other_services.dart`
  - `model/free_trial.dart` ← 免费额度模型
  - `screens/home/chat_screen.dart`、`codescreen.dart`、`explain_code_screen.dart`、
    `code_error_finding_screen.dart`、`debug_uploaded_code_screen.dart`、
    `image_explain/upload_image_screen.dart`、`recognized_text_screen.dart`
- 网络层函数名：`getOpenAIResponse`、`getOpenAIResponseFromCloud`
- 依赖：`cloud_functions`、`cloud_firestore`、`firebase_messaging`、`google_mobile_ads`、
  `purchases_flutter`(RevenueCat)、`highlighter`(代码高亮)、`lottie`

**关键否定证据**：全量扫描 `libapp.so` 后，
- `sk-[A-Za-z0-9]{20,}` → **0 命中**（无 OpenAI key）
- `chat/completions`、`/v1/...` → **0 命中**（无 REST 端点）
- `cloudfunctions`、`baseUrl`、任何第三方域名 → **0 命中**
- 仅有的 7 条 URL 全是 flutter.dev / github.com/flutter / play.google.com / firebase 文档链接

→ 说明**上游地址是运行时由 `projectId` 拼出来的**（Firebase SDK 行为），所以静态看不到。

### 1.4 Prompt 模板在 APK 里（本地拼装）

`libapp.so` 里有大量完整 prompt 文本，例如：

```
"I want you to act as a Senior Frontend developer. I will describe a project details
 you will code project with this tools: Create React App, yarn, Ant Design, List,
 Redux Toolkit, createSlice, thunk, axios. You should merge files in single index.js
 file and nothing else. Do not write explanations. My first request is ..."
"Act as a Full-Stack Software Developer"
```

→ 上游服务端是**无 system prompt 的通用 GPT-4 代理**，所有「代码生成/解释/找错/调试」能力都是
客户端把模板塞进 `conversation` 实现的。这对 2API 是好消息：我们直接透传用户 prompt 即可，不需要复刻人设。

### 1.5 图片能力是设备端 OCR，不是视觉模型

`libmlkit_google_ocr_pipeline.so` + `assets/mlkit-google-ocr-models` + `recognized_text_screen.dart`
→ 流程是「拍照 → MLKit 本地 OCR → 把文本发给 chat 函数」。
行为验证也确认：往 `conversation` 塞 `image_url` 内容段会被判 `Valid messages are required`，
`image` / `imageBase64` / `base64Image` / `imageData` 等字段全部被忽略（模型回答「我没看到图片」）。
`visionApiTries: 15` 是遗留字段，实际不可用。

---

## 2. 动态还原（全是实打实的请求/响应）

### 2.1 打穿路径

1. 用 arsc 里的 `google_api_key` 调 Firebase 匿名登录：
   `POST https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=<KEY>` body `{"returnSecureToken":true}`
   → 200，拿到 `idToken` / `refreshToken` / `localId`，idToken 载荷里泄露 `projectId`。
2. 按 Firebase callable 命名规则拼函数 URL，对 6 个区域 × 2 个候选函数名做探活：
   - `us-central1-codeai-6970b.cloudfunctions.net/getOpenAIResponse` → **HTTP 400 业务错误**（存在）
   - 其余全部 404 `Page not found`
3. 用 500 个候选函数名（`get*/handle*/process*` × 业务词根）扫一遍 → **只有 `getOpenAIResponse` 存在**。
   即服务端确实只有 1 个函数。

### 2.2 请求 DTO（逐字还原，靠上游错误信息作为 oracle）

上游校验顺序对应错误信息，把它当 schema 探测器：

| 试的 payload | 上游返回 | 推断 |
|---|---|---|
| `{}` | `Valid conversation is required.` | 必填 `conversation` |
| `{conversation:[{role,text}]}` | `Valid messages are required.` | `conversation[].content` 必须是字段名 `content` |
| `{conversation:[{role,content}]}` | `A request UUID is required.` | 必填 UUID 字段 |
| `{...,messages:[...]}` | `A request UUID is required.` | `messages` 必填但已通过 |
| `{...,requestUUID|request_uuid|uuid|id|requestUuid}` | 仍报 UUID | 字段名不是这些 |
| `{...,requestId:"<uuid4>"}` | **HTTP 200 + 真实回答** | 字段名是 `requestId` |

最终可用请求体（Firebase callable 信封 `{"data": ...}`）：

```json
{
  "data": {
    "conversation": [
      {"role": "system",    "content": "..."},
      {"role": "user",      "content": "..."},
      {"role": "assistant", "content": "..."},
      {"role": "user",      "content": "..."}
    ],
    "messages": [ ... 同样的数组 ... ],
    "requestId": "8b7f4c1e-9a3d-4d0e-9f2a-0c1b2d3e4f50"
  }
}
```

请求头：
```
Authorization: Bearer <firebase anonymous idToken>
Content-Type: application/json
```

### 2.3 `conversation` vs `messages` 到底谁生效

A/B 实验：

| conversation | messages | 模型实际回答 | 结论 |
|---|---|---|---|
| `[user:"What is 2+2?"]` | `[system:"Answer only in French.", user:"What is 2+2?"]` | `4` | system 未生效 |
| `[user:"zzz"]` | `[user:"Reply with the single word: BANANA"]` | 围绕 `zzz` 展开聊 | **`conversation` 才驱动模型** |

→ `messages` 只过校验、不参与推理（疑似用于服务端日志/风控统计）。
实现上两个字段填同一份数组最安全。

`role` 支持 `system` / `user` / `assistant`（多轮上下文实测有效：先告知「我叫 Zed」，下一轮能答出「你的名字是 Zed」）。

### 2.4 响应 DTO

```json
{
  "result": {
    "response": "……正文……",
    "requestId": "48c5bba6-365b-43ca-a17e-7bd52fe5d36b",
    "remainingChats": 4,
    "rewardedCredits": 0,
    "visionApiTries": 15,
    "isPro": false
  }
}
```

| 字段 | 含义 | 备注 |
|---|---|---|
| `response` | 正文纯文本 | 非 SSE，一次性返回；无 markdown 结构化字段 |
| `remainingChats` | 该 uid 剩余免费次数 | 首次调用后为 4，逐次递减到 0 |
| `rewardedCredits` | 看激励广告获得的额外次数 | 服务端发放（AdMob SSV），客户端无法自增 |
| `visionApiTries` | 视觉尝试余额（遗留） | 恒 15，实际无视觉能力 |
| `isPro` | 是否订阅 | 请求里传 `isPro:true` / `premium:true` **无效**，服务端读自己的订阅状态 |

### 2.5 错误码表（全部实测）

| HTTP | body | 触发条件 | 网关处理 |
|---|---|---|---|
| 400 | `{"error":{"message":"Valid conversation is required.","status":"INVALID_ARGUMENT"}}` | 缺 `conversation` | 不应出现（网关强制构造） |
| 400 | `Valid messages are required.` | `messages` 缺 / 内容非字符串 | 同上 |
| 400 | `A request UUID is required.` | 缺 `requestId` | 同上（网关用 uuid4） |
| 401 | `{"error":{"message":"Please reconnect to sign in.","status":"UNAUTHENTICATED"}}` | 无 token / token 失效 | refreshToken 换新，失败则弃号换号 |
| 409 | `{"error":{"details":{"pending":true},"message":"A previous request is still pending."}}` | 同 uid 并发第二个请求 | 换号重试 |
| 429 | `{"error":{"details":{"reason":"quota-exhausted"},"message":"Watch an ad or upgrade to continue.","status":"RESOURCE_EXHAUSTED"}}` | 该 uid 5 次用完 | 弃号 + 换新号重试 |
| 500 | `{"error":{"details":{"final":true},"message":"No result was returned. Please retry.","status":"INTERNAL"}}` | 输出过长被截断 / 函数内部异常 | 直接透传，不重试（确定性失败） |

---

## 3. 额度与吞吐（实测）

### 3.1 免费额度

- 新 uid 第一次调用 → `remainingChats: 4` → 3 → 2 → 1 → 0，第 6 次 429 `quota-exhausted`。
- 即 **每个 uid 5 次**。额度绑定 Firebase uid，换 uid = 换一份新额度。
- `refreshToken` 刷新 idToken **不重置**额度（uid 不变）——这点和 AI Chat 1.4.28 一致。

### 3.2 并发

同 uid 8 路并发实测：7 个 409 `pending`，1 个成功。**严格串行**。
→ 吞吐上限 = 存活账号数 ÷ 单请求延迟。以 7 s 中位延迟算，1 个号 ≈ 8.5 请求/分钟。

### 3.3 账号铸造速率

30 个 uid 用 10 线程铸造：**30/30 成功，wall 9.4 s**（≈3.2 个/秒，单次 ~2.76 s 主要是 TLS 握手）。
网关内置指数退避（连续 3 次失败开始退避，最长 300 s），避免把 Firebase 侧打成异常流量。

### 3.4 延迟实测

| 场景 | 并发 | n | 成功率 | p50 | p95 |
|---|---|---|---|---|---|
| 真实短答（JSON） | 2 | 6 | 6/6 | 6.50 s | 7.84 s |
| 真实短答（JSON） | 4 | 6 | 6/6 | 9.98 s | 18.77 s |
| 真实流式（模拟 SSE） | 2 | 4 | 4/4 | 6.42 s | 7.86 s |
| mock 管道（JSON） | 1 / 10 / 25 / 50 | 100×4 | 400/400 | 0.26–0.34 s | ≤0.48 s |
| mock 管道（stream） | 1 / 10 / 25 | 60×3 | 180/180 | 0.26–0.27 s | ≤0.30 s |

> mock 只证明**网关管道**（并发、账号池换号、SSE 拼帧）正确，不证明上游可用性。
> 真实链路已单独实测 16 次全绿（`bench_real.json` / `bench_real_stream.json`）。

### 3.5 输出上限

- 要「重复输出 3000 个 alpha」→ 上游直接 **HTTP 500 `No result was returned`**（空结果）。
- 正常长答实测落在 ≈1890 字符（≈500 token）处被切断。
- 结论：单次输出上限约 **500 token**，且**超限不是截断而是直接报错**。
  网关已把 `CODEAI_MAX_PROMPT_CHARS`（默认 24000）作为输入上限，避免把额度烧在必然失败的请求上。

---

## 4. 商业面（静态确认）

- 订阅：`purchases_flutter`(RevenueCat)，产品档位在 `raw/assets/` 里的 UI 图（pro.png / upgrade.png /
  paywall_bg.png）配合 `upgrade_widget.dart` / `paymentwall.dart`。
- 变现：AdMob 激励视频（`rewardedCredits`）+ RevenueCat 订阅（`isPro`）。
- App 本身：Google Play `com.itechgemini.code_ai`，隐私政策自述「对话经 OpenAI API 处理，不保存」。
- 无自有账号体系 —— **登录就是 Firebase 匿名登录**，因此服务端拿不到任何真实用户身份，
  风控只能靠 uid + IP。这也是它容易被池化的根因。

---

## 5. 风险与失效条件

| 风险 | 影响 | 判据 / 应对 |
|---|---|---|
| 项目方开启 Firebase App Check / Play Integrity | 匿名铸造立刻全废（需要真实设备 attestation） | 目前 `accounts:signUp` 无任何 attestation 要求，若某天返回 403 即命中 |
| 项目方加 IP 频控 | 铸造速率受限 | 网关已有铸造退避；必要时挂代理池 |
| 项目方换上游模型/加 system prompt | 回答风格变化，但协议不变 | 无需改代码 |
| 项目方下架/弃更 | 函数仍在但模型可能降级 | 无解，属于外部依赖 |
| 上游输出 500 token 硬上限 | 生成长代码会失败 | 已在网关侧做输入约束 + 错误透传 |
| 批量注册的合规风险 | 非技术风险 | 已在本报告显式标注；上线前请自行评估 |

---

## 6. 复现步骤

```bash
# 1. 抽 Firebase 配置
python scan_arsc.py            # 见 D:\apk-analysis\aicg332\（本项目根目录）

# 2. 匿名铸造 + 直接打函数（不经网关）
python -c "..."                # 见 docs 中 2.1 的三步

# 3. 起网关
cd 2api && python -m uvicorn server:app --host 127.0.0.1 --port 19031

# 4. 打真实请求
python bench.py --base-url http://127.0.0.1:19031 --mode real --concurrency 2 --requests 6
```
