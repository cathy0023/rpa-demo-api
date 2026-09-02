# Implementation Plan: WECOM-001 企微侧边栏·智能销售话术助手

## Goal

在 rpa-demo-api（FastAPI）新增 `app/wecom/` 侧边栏模块（签名/登录/画像/历史/生成 5 端点 + 会话存档同步），在 rpa-demo-web（React18+Vite）新增 `src/sidebar/` H5 入口，实现「打开侧边栏 → 自动识别客户 → 画像+历史注入 → 生成 1 条话术 → 复制」闭环。会话存档（msgaudit）链路带 `WECOM_SID_ENABLED` 降级开关。

## Architecture

- 后端：新增 `app/wecom/` 包（config/token/signature/auth/contact/msgaudit/sync/context/generate/router 共 10 模块），挂载到现有 `api_router`（`/api/v1/wecom/sidebar/*`）；SQLite 新增 3 表（wecom_chat_history / sync_state / wecom_profile_cache），复用 db.py 的 WAL+mutex 范式；复用 llm.py 的 httpx 调用与思维链兼容逻辑（提取共享函数，原 RPA 路径行为不变）。
- 前端：Vite 多页（根 `index.html` 不动 + 新增 `sidebar.html` + `src/sidebar/`），jweixin SDK script 引入，auth.ts 全链路鉴权封装，Assistant.tsx 三态 UI。
- API 信封：统一 `{code: 2000, data, message}`；5 端点中 sign 无需会话，其余 4 个（login 兑换后）要求会话 cookie。

## Tech Stack

- 后端：Python 3.14 / FastAPI 0.115 / pydantic 2.10 / httpx / cryptography（RSA PKCS1v15 + AES-256-CBC）/ SQLite（sqlite3 标准库）/ pytest + pytest-asyncio
- 前端：React 18.3 / TypeScript 5.6 / Vite 5 / pnpm
- 企微：JS-SDK（wx.config + wx.agentConfig + wx.qy.login + getCurExternalContact）、服务端 access_token + 双 jsapi_ticket、会话存档 C SDK（ctypes，mock 测试）

## Acceptance Criteria 覆盖映射

| AC | Tasks |
|----|-------|
| AC1 单测全绿（签名/token/cookie/RSA-AES 往返/幂等/降级 + pnpm build） | T1-T8, T10 |
| AC2 [联调] 鉴权链路 | T1-T3, T9 |
| AC3 画像与历史注入 | T4, T7, T8 |
| AC4 [本地] 话术生成 | T5, T6 |
| AC5 [本地] 幂等与留存 | T8 |
| AC6 [本地] 降级可用 | T7, T8 |
| AC7 [本地] 密钥零入库 | T1, T11 |
| AC8 [本地] 访问控制 401 | T3, T5 |

---

## Tasks

### T1: wecom config + token + signature 模块与单测

- [ ] Write test: `tests/wecom/test_token.py`（mock httpx：access_token/企业 ticket/应用 ticket 获取、缓存命中不再请求、过期前 300s 主动刷新三场景）+ `tests/wecom/test_signature.py`（已知向量：sha1(jsapi_ticket=..&noncestr=..&timestamp=..&url=..) 与官方示例一致；url 含 # 时截断）
- [ ] Run test → 失败（模块不存在）
- [ ] Implement: `app/wecom/__init__.py`、`app/wecom/config.py`（WecomConfig frozen dataclass：WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_APP_SECRET / WECOM_SID_TRUSTED_DOMAIN / WECOM_SID_COOKIE_SECRET / WECOM_SID_ENABLED / WECOM_SID_POLL_INTERVAL，默认关）、`app/wecom/token.py`（内存缓存 dataclass + httpx 获取，双 ticket 分开）、`app/wecom/signature.py`（jsapi_signature(ticket, nonce_str, timestamp, url)）
- [ ] Run test → 通过
- [ ] Commit: `feat(wecom): config/token/signature modules with tests`

### T2: llm 共享调用函数提取（不动 RPA 路径）

- [ ] Write test: `tests/test_llm_shared.py`（`call_chat_completion(messages, max_tokens, temperature) -> str`：mock httpx 正常返回 / 思维链 content=null 走 thinking_blocks / HTTP 错误抛 LlmError；另跑 `tests/test_llm.py` 原 17 测试确保不回归）
- [ ] Run test → 失败
- [ ] Implement: `app/wecom/llm_shared.py`——把 llm.py 的 httpx 请求+响应解析+思维链兼容提取为独立函数；`llm.py` 的 `generate_reply` 改为薄封装调用它（行为完全不变）
- [ ] Run test → 通过（新旧测试都绿）
- [ ] Commit: `refactor(llm): extract shared chat-completion caller, keep RPA path intact`

### T3: auth 会话锚点 + sign/login 端点 + 401 守卫

- [ ] Write test: `tests/wecom/test_auth.py`（sign_cookie/verify_cookie 往返、篡改 cookie 拒绝、过期拒绝）；`tests/wecom/test_router_auth.py`（/sign 返回双签名结构且校验 url 域名白名单、/login mock getuserinfo 成功 Set-Cookie、无 cookie 访问 profile/history/generate 返回 code!=2000 且 401 语义）
- [ ] Run test → 失败
- [ ] Implement: `app/wecom/auth.py`（HMAC 签名 cookie，itsdangerous 不引入——用 hmac+json+base64 自实现 30 行内）、`app/wecom/deps.py`（FastAPI Depends 会话校验）、`app/wecom/router.py` 骨架（/sign /login + 4 端点占位 401）
- [ ] Run test → 通过
- [ ] Commit: `feat(wecom): auth session anchor + sign/login endpoints + guard`

### T4: contact 画像代理 + profile 端点（含 cache）

- [ ] Write test: `tests/wecom/test_contact.py`（mock httpx：externalcontact/get 精简字段映射、缓存 10min 内命中不打企微、过期重取、写入 wecom_profile_cache 表）；`tests/wecom/test_router_profile.py`
- [ ] Run test → 失败
- [ ] Implement: `app/wecom/contact.py` + router /profile 端点（替换 T3 占位）；`db.py` 无需改（cache 表在 T6 建，此任务先建表语法放 T6 会阻塞——改为：T4 内先执行 `CREATE TABLE IF NOT EXISTS wecom_profile_cache` 于独立迁移函数 `app/wecom/migrations.py`）
- [ ] Run test → 通过
- [ ] Commit: `feat(wecom): contact profile proxy with cache + /profile endpoint`

### T5: generate 话术生成 + /generate 端点

- [ ] Write test: `tests/wecom/test_generate.py`（mock llm_shared：画像+历史 prompt 组装含客户名/标签/最近消息、exclude 注入「避免与以下重复」、空历史降级仅画像+scenario、LLM 未配 key 返回 LlmError）；`tests/wecom/test_router_generate.py`（/generate 需会话 cookie（AC8），返回 {script}）
- [ ] Run test → 失败
- [ ] Implement: `app/wecom/generate.py`（侧边栏 SYSTEM_PROMPT ≤200 字话术）+ `/generate` 端点；场景 prompt 组装函数放 `app/wecom/context.py`（build_prompt(profile, history, scenario, exclude)）
- [ ] Run test → 通过
- [ ] Commit: `feat(wecom): script generation with context prompt + /generate endpoint`

### T6: SQLite 3 表迁移

- [ ] Write test: `tests/wecom/test_migrations.py`（迁移后 3 表存在、字段含 sender_userid/seq UNIQUE、重复执行幂等、旧库（模拟 rpa_demo 已有表）共存不破坏）
- [ ] Run test → 失败
- [ ] Implement: `app/wecom/migrations.py` 统一建 wecom_chat_history（含 sender_userid）/ sync_state / wecom_profile_cache 三表 + `db.py` 初始化处调用；T4 的独立建表迁入
- [ ] Run test → 通过
- [ ] Commit: `feat(wecom): sqlite migrations for chat history/sync state/profile cache`

### T7: msgaudit 解密层（纯函数，可测）+ /history 端点降级

- [ ] Write test: `tests/wecom/test_msgaudit_crypto.py`（cryptography 库生成 RSA 密钥对自构造向量：encrypt_random_key RSA-PKCS1 加密 secret_key → decrypt_random_key 往返；encrypt_chat_msg AES-256-CBC(pad) → 解密往返；错误私钥抛异常）；`tests/wecom/test_router_history.py`（SID_ENABLED=false 时 /history 返回 code:2000 data:[]（AC6 降级语义））
- [ ] Run test → 失败
- [ ] Implement: `app/wecom/msgaudit.py` 的解密部分（`decrypt_msg(private_key_pem, encrypt_random_key, encrypt_chat_msg) -> dict` 纯函数；SDK 拉取部分留接口 `ChatArchiveClient` Protocol + `DisabledChatArchiveClient` 实现）；`app/wecom/router.py` /history 端点（查 wecom_chat_history 按用户倒序 limit）
- [ ] Run test → 通过
- [ ] Commit: `feat(wecom): msgaudit decrypt layer + /history with degrade`

### T8: sync 同步任务（SDK 接口 + 幂等落库）

- [ ] Write test: `tests/wecom/test_sync.py`（mock ChatArchiveClient 返回加密消息批：解密成功落库、同 seq 重复跳过（幂等 AC5）、解密失败单条跳过不中断、last_seq 推进持久化、from_role 判定 sender==external_userid、非外部联系人对话不入库）；`tests/wecom/test_router_history_sync.py`（sync 后 /history 返回消息（AC3 本地部分））
- [ ] Run test → 失败
- [ ] Implement: `app/wecom/sync.py`（asyncio task：lifespan 启动、WECOM_SID_ENABLED=false 不启动、to_thread 落库、批 100、poll interval 配置）；`app/wecom/msgaudit.py` 补 ctypes 拉取实现（Init/GetChatData/Destroy，SDK .so 路径可配，import 失败时降级 Disabled）
- [ ] Run test → 通过；`pytest tests/` 全绿
- [ ] Commit: `feat(wecom): background sync task with idempotent persistence`

### T9: 前端 sidebar H5（web worktree）

- [ ] Write/build check: web worktree `pnpm build` 通过（含新入口 tsc 检查）
- [ ] Implement: `sidebar.html`（jweixin SDK script + 挂载点）、`src/sidebar/main.tsx`、`src/sidebar/auth.ts`（getSign→wx.config→wx.agentConfig→wx.qy.login→POST /login→getCurExternalContact，逐步骤错误态）、`src/sidebar/Assistant.tsx`（三态：loading/script-card/error-retry；卡片含客户名+标签+话术+复制+换一条（带 exclude）；15s 前端超时）、`src/sidebar/api.ts`（fetch 封装 credentials:include）、vite.config.ts 多页 + proxy `/api` → uvicorn、根 App/index.html 不动
- [ ] Run: `pnpm build` 通过
- [ ] Commit（web 仓库）: `feat(sidebar): wecom sidebar H5 with auth flow and script assistant`

### T10: 集成验证（端到端本地冒烟）

- [ ] Write test: `tests/wecom/test_e2e_local.py`（TestClient 全链路：/sign → /login(mock code) → 携带 cookie /profile(mock) → /generate(mock llm) 返回 script；SID_ENABLED=false 时 /history 空数组；无 cookie 401）
- [ ] Run test → 失败
- [ ] Implement: 仅修暴露的集成问题（不新增功能）
- [ ] Run test → 通过；`pytest tests/` 17+新增全绿
- [ ] Commit: `test(wecom): local e2e smoke for sidebar endpoints`

### T11: 配置文档 + 密钥零入库核查（AC7）

- [ ] 核查：`grep -r "corp\|secret\|key" --include="*.py" app/ tests/` 无真实密钥值
- [ ] Implement: `.env.example` 补全 `WECOM_*`（corp_id/agent_id/app_secret/trusted_domain/cookie_secret/sid_enabled/poll_interval/sdk_path）；README 新增「企微侧边栏配置」节（企微后台步骤 + Whistle 联调指引 + 降级开关说明）；更新 `docs/rfcs/README.md` 无需动
- [ ] Run: `pytest tests/` 全绿 + `pnpm build` 通过（web）
- [ ] Commit: `docs(wecom): env example and sidebar setup guide`

---

## Execution Notes

- 工作目录：后端在 `/Users/chenyan/Documents/sop-mini/rpa-demo-api-wecom-sidebar`（session CWD），前端在 `/Users/chenyan/Documents/sop-mini/rpa-demo-web-wecom-sidebar`（T9）。
- 每个任务：写测试 → 跑失败 → 实现 → 跑通过 → commit（TDD 五步）。
- 后端测试命令：`./.venv/bin/python -m pytest tests/ -q`；前端：`pnpm build`。
- 禁止：修改 `app/routers/callback.py`/`monitor.py`/`service.py` 等 RPA 现有行为（T2 仅提取不改变行为）。
- 验收对照：AC1↔T1-T8/T10，AC8↔T3/T5，AC5↔T8，AC6↔T7/T8，AC7↔T11。
