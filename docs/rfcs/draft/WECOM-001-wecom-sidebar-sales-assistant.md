---
title: WECOM-001 企微侧边栏·智能销售话术助手
id: WECOM-001
created: 2026-09-02
source: inbox/2026-09-02-wechat-sidebar-sales-assistant.md
status: Draft
---

# WECOM-001 企微侧边栏·智能销售话术助手

## Goals

销售在与客户单聊中打开企微侧边栏，系统自动识别当前客户，基于「客户画像 + 历史聊天上下文」实时生成 1 条贴合场景的销售话术，支持一键复制与「换一条」重生。具体目标：

1. **自动识别客户**：侧边栏打开后无需手动搜索，通过 `wx.qy.getCurExternalContact` 自动获取当前客户 userid。
2. **上下文注入**：服务端自动注入客户画像（企微 externalcontact 资料/标签）与历史聊天（会话存档落库后的最近 N 条），作为 LLM 上下文。
3. **实时生成**：LLM（OpenAI 兼容直连）输出 1 条 ≤200 字话术；销售可点「换一条」重新生成。
4. **一键复制**：话术复制到剪贴板，销售自行粘贴发送（不代发）。
5. **先自建后 SaaS**：自建应用模式跑通 MVP，架构预留多租户演进空间（配置集中、数据带 corp 维度）。
6. **消息同步后台任务**：会话存档消息通过后台同步任务实时（秒级轮询）落 SQLite，侧边栏打开时直接 SQL 查询，低延迟。

## Background

### 业务背景

智能销售话术助手产品的核心能力是「AI 生成销售话术」，但该能力目前不在销售的工作现场。销售在企微与客户聊天时需切换工具、手动描述场景，生成的话术缺乏客户上下文。企微聊天工具栏（侧边栏）是话术出现在工作现场的唯一合规入口。

### 平台调研结论（2026-09-02）

- 侧边栏形态：自建应用 H5 配置到「聊天工具栏」，单聊/客户群点开右侧弹出（工具栏上限 50 个）。
- 鉴权链路：服务端 access_token → **企业 jsapi_ticket（wx.config）与应用 jsapi_ticket（wx.agentConfig）两套 ticket，算法相同但接口不同，均 7200s 有效需缓存** → 签名（nonceStr/timestamp/url 排序拼接 SHA1）→ 前端先 `wx.config` 后 `wx.agentConfig` → `wx.qy.login` → `wx.qy.getCurExternalContact`。
- 可信域名**必须有 ICP 备案**且管理端验证归属（根目录放校验文件）；localhost/IP 不可直接用。
- 本地联调：**Whistle 代理**方案（企微流量代理到本地 dev server）是社区主流；或内网穿透公网域名回源。
- 会话存档（msgaudit）：**无 HTTP 接口**，官方 C SDK（libWeWorkFinanceSdk）+ Python ctypes 封装；拉取 `GetChatData(seq, limit≤1000)` → `encrypt_random_key` RSA(PKCS1) 私钥解密得 secret_key → `encrypt_chat_msg` AES-256-CBC 解密；**消息留存 5 天**，需持续增量拉取（seq 递增）。前提：企业购买会话存档服务 + 管理后台配置 RSA 公钥 + 合规审批。
- 侧边栏页面**读不到聊天原文**，聊天上下文只能走会话存档。

### 项目现状

- 复用基座：`rpa-demo-api`（FastAPI）已有 `llm.py`（`generate_reply(customer_message, customer_name)`，OpenAI 兼容直连，思维链模型兼容）、`config.py`（frozen dataclass 环境变量集中读取）、`db.py`（SQLite WAL + threading mutex 串行写 + team_id 隔离范式）、`routers/__init__.py`（APIRouter 聚合 `/api/v1/rpa`）；`rpa-demo-web`（React18 + TS + Vite + pnpm）。
- 竞品形态：卫瓴小微AI（画像+聊天记录生成话术）、纷享销客（知识库+话术库推荐）、探迹（素材库）、句子互动（AI 员工）——画像+聊天上下文实时生成是主流高配形态。

## Design

### 总体架构

```
企微客户端(单聊侧边栏)                FastAPI(rpa-demo-api 扩展)
┌──────────────────┐   HTTPS    ┌─────────────────────────────────┐
│ sidebar H5       │──────────→│ /wecom/sign        签名+票证      │
│ wx.config        │           │ /wecom/profile     客户画像代理   │
│ wx.agentConfig   │           │ /wecom/history     历史上下文     │
│ getCurExternal   │           │ /wecom/generate    话术生成       │
│ Contact          │           │                                 │
└──────────────────┘           │  msgaudit_sync (后台任务)         │
                               │  GetChatData→RSA→AES→SQLite     │
                               └───────────┬─────────────────────┘
                                           │ httpx
                                           ▼
                                    企微开放平台 API / LLM(OpenAI 兼容)
```

### 模块设计（后端，新增 `app/wecom/` 包）

| 模块 | 职责 | 关键点 |
|------|------|--------|
| `app/wecom/config.py` | 企微配置 | 继承 frozen dataclass 范式：`WECOM_CORP_ID` / `WECOM_APP_SECRET` / `WECOM_AGENT_ID` / `WECOM_SID_RSA_PRIVATE_KEY`（env 或文件路径）/ `WECOM_SID_ENABLED` 开关 |
| `app/wecom/token.py` | access_token + 双 ticket 缓存 | 内存缓存 + 过期前 300s 主动刷新；企业 ticket 与应用 ticket 分开存 |
| `app/wecom/signature.py` | 签名生成 | `sha1(jsapi_ticket=..&noncestr=..&timestamp=..&url=..)`，url 去除 # 后内容 |
| `app/wecom/auth.py` | 员工身份兑换 | `POST /cgi-bin/auth/getuserinfo`：前端 `wx.qy.login` 的 code 兑换当前员工 userid，写入短期签名 cookie（HttpOnly，2h）作为后续请求身份锚点 |
| `app/wecom/contact.py` | 客户画像代理 | `GET /cgi-bin/externalcontact/get`，输出精简画像 dict（昵称/备注/公司/标签）；读写 `wecom_profile_cache`（TTL 10 分钟） |
| `app/wecom/msgaudit.py` | 会话存档拉取+解密 | ctypes 封装官方 C SDK（可选用 pypi `wecom-audit`，锁定 fallback 为自封装）；`GetChatData(seq)` → RSA PKCS1 → AES-256-CBC；文本消息优先，媒体消息存元数据不下载 |
| `app/wecom/sync.py` | 同步后台任务 | asyncio task 秒级轮询（可配 `WECOM_SID_POLL_INTERVAL`），seq 持久化到 SQLite `sync_state` 表；单聊文本消息写入 `wecom_chat_history` 表；落库走 `asyncio.to_thread` 分批写入避免阻塞事件循环；单条解密失败跳过并告警日志，不中断轮询 |
| `app/wecom/context.py` | 上下文组装 | 画像 dict + 最近 N 条聊天（`wecom_chat_history` 按 external_userid 倒序）拼成 LLM prompt 输入 |
| `app/wecom/generate.py` | 话术生成 | 新建侧边栏专属 SYSTEM_PROMPT（输出 1 条 ≤200 字话术），复用 `llm.py` 的 httpx 调用与思维链兼容逻辑（提取共享函数，`llm.py` 原 RPA 路径行为不变） |
| `app/wecom/router.py` | 路由 | 4 个端点（见下），挂载到 `api_router` |

### API 设计

所有端点走 `POST`/`GET` JSON，统一 `{code: 2000, data, message}` 信封（沿用 demo 约定）。

| 端点 | 方法 | 入参 | 出参 |
|------|------|------|------|
| `/api/v1/wecom/sidebar/sign` | GET | `?url=<当前页面URL>` | `{corp_id, agent_id, config_sig, agent_config_sig, nonce_str, timestamp}`（双签名一次返回） |
| `/api/v1/wecom/sidebar/login` | POST | `{code}`（`wx.qy.login` 返回） | 兑换员工 userid，`Set-Cookie` 签名会话（HttpOnly 2h） |
| `/api/v1/wecom/sidebar/profile` | GET | `?userid=<external_userid>` | 精简画像（name/remark/company/tags/desc） |
| `/api/v1/wecom/sidebar/history` | GET | `?userid=<external_userid>&limit=20` | `[{role, content, ts}]` |
| `/api/v1/wecom/sidebar/generate` | POST | `{userid, scenario?}` | `{script}`（1 条话术） |

鉴权（身份锚点设计）：`login` 端点将 `wx.qy.login` 的 code 兑换为员工 userid，作为后续请求的身份锚点；`profile`/`history`/`generate` 三个端点要求有效会话 cookie 才能调用，防止任意访客枚举拉取客户会话存档（敏感数据暴露面控制）。`sign` 端点校验 url 必须属于已配置可信域名（`WECOM_SID_TRUSTED_DOMAIN`）。该设计为轻量会话锚点而非完整 OAuth 登录体系，不违背「MVP 不做登录态」的决策本意；SaaS 阶段升级为标准 OAuth（见 Non-Goals）。

### 数据模型（SQLite，新增 3 表）

```sql
CREATE TABLE IF NOT EXISTS wecom_chat_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    corp_id         TEXT NOT NULL,
    external_userid TEXT NOT NULL,
    sender_userid   TEXT NOT NULL,      -- 发送者：员工 userid 或 external_userid
    from_role       TEXT NOT NULL,      -- 'staff' | 'customer'（sender==external_userid 判定为 customer）
    content         TEXT NOT NULL,
    msg_ts          INTEGER NOT NULL,
    seq             INTEGER NOT NULL UNIQUE  -- 幂等：seq 唯一
);
CREATE INDEX IF NOT EXISTS idx_wecom_hist_user ON wecom_chat_history(corp_id, external_userid, msg_ts DESC);
CREATE TABLE IF NOT EXISTS sync_state (
    corp_id  TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wecom_profile_cache (
    corp_id         TEXT NOT NULL,
    external_userid TEXT NOT NULL,
    profile_json    TEXT NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (corp_id, external_userid)
);
```

### 前端设计（rpa-demo-web 扩展）

- 新增 `src/sidebar/` 独立入口（`sidebar.html` + `src/sidebar/main.tsx`），与监控页互不干扰；Vite 多页构建。
- `src/sidebar/auth.ts`：加载签名 → `wx.config` → `wx.agentConfig` → `getCurExternalContact` 全链路封装，失败逐步骤报错展示。
- `src/sidebar/Assistant.tsx`：三态 UI（加载中 / 话术卡片 / 错误重试）；话术卡片含「复制」「换一条」；顶部显示客户名+标签。
- 样式：窄幅侧边栏适配（企微 PC 侧边栏约 360-400px 宽）。
- 本地联调：Whistle 代理方案，`sidebar.html` 在可信域名下可访问。

### 关键技术决策

1. **SDK 选型**：接口抽象 + 自封装 ctypes 调用官方 C SDK 为默认实现（`libWeWorkFinanceSdk_C.so`/`.dylib`，仅 Init/GetChatData/Destroy 3 个函数）；pypi `wecom-audit` 为备选实现（驳回为默认的理由：维护活跃度未知、平台二进制兼容风险）。
2. **上下文窗口**：最近 20 条聊天（约 1-2k token）+ 画像 ≤300 token，一次生成总输入 ≤3k token，成本可控；不做 LLM 预摘要（MVP）。
3. **降级路径**：`WECOM_SID_ENABLED=false`（会话存档未开通）时 `/history` 返回空、生成仅基于画像+scenario 输入，功能不中断。
4. **同步任务生命周期**：FastAPI lifespan 启动 asyncio task，`WECOM_SID_ENABLED=false` 时不启动。
5. **部署与联调拓扑**：侧边栏 H5 与 API 同源部署（FastAPI 托管前端构建产物 `sidebar.html` 静态资源），避免 CORS；本地开发用 Vite dev server + Whistle 将可信域名代理到本地，API 走 Vite proxy 转发到 uvicorn，`main.py` CORS 白名单补充 dev origin。
6. **scenario 参数语义**：MVP 阶段 `scenario` 为可选自由文本（销售手动补充的临时场景说明，如「客户在比价」），不暴露固定意图分档（开场/逼单等）——意图分档待销售团队反馈后在后续迭代定义。
7. **重生随机性**：`generate` 支持 `exclude` 入参（上一条话术文本），prompt 中注入避免重复指令，temperature 维持 0.7。

## Implementation

### 阶段 1：后端侧边栏模块（不含会话存档）

1. `app/wecom/config.py`（含 `WECOM_SID_TRUSTED_DOMAIN`）+ `token.py` + `signature.py` + 单测（token 缓存刷新逻辑 mock httpx、签名算法已知向量验证）
2. `app/wecom/auth.py` + `/sign` `/login` 端点（会话 cookie 签发与校验）+ 单测
3. `app/wecom/contact.py`（画像 + profile_cache 读写 TTL 10min）+ `/profile` 端点 + 单测（mock 企微 API）
4. `app/wecom/generate.py`（侧边栏专属 prompt，提取 llm.py 共享调用函数，原 RPA 路径不动）+ `/generate` 端点（含 exclude 去重）+ 单测（mock LLM）
5. `app/wecom/router.py` 挂载 + `routers/__init__.py` 注册 + CORS dev origin

### 阶段 2：会话存档链路

6. `db.py` 新增 3 表（`wecom_chat_history` 含 sender_userid / `sync_state` / `wecom_profile_cache`）+ 迁移兼容测试
7. `app/wecom/msgaudit.py` ctypes 封装 + RSA/AES 解密 + 单测（cryptography 库自构造密文向量验证解密链路）
8. `app/wecom/sync.py` 轮询任务（to_thread 分批落库、解密失败跳过+告警）+ `/history` 端点 + 单测（mock SDK 返回加密消息）

### 阶段 3：前端侧边栏 H5

9. Vite 多页配置 + `sidebar.html`（引入企微 jweixin SDK script）+ `src/sidebar/auth.ts`（wx.config→agentConfig→login→getCurExternalContact，逐步骤报错）+ `Assistant.tsx`（三态 + 复制/换一条/exclude 传参；生成超时阈值 15s 独立于后端 30s）
10. 前后端联调：pnpm build 通过 + Vite proxy + 本地 mock 企微 JS-SDK 的冒烟路径；FastAPI 托管 sidebar 静态资源验证同源部署

### 阶段 4：配置与文档

11. `.env.example` 更新（全部 `WECOM_*` 变量）+ README 侧边栏配置指引 + docs 联调说明

## Acceptance Criteria

> 每条标注验收环境：**[本地]** = 无真实企微环境可验证；**[联调]** = 需真实企微环境。

1. **[本地] 单测全绿**：`pytest tests/` 覆盖签名算法（已知向量）、token 缓存刷新、login 会话 cookie 签发与校验、RSA/AES 解密往返（自构造向量）、sync 幂等（同 seq 不重复落库）、generate 降级路径；前端 `pnpm build` 通过。
2. **[联调] 鉴权链路可用**：前置条件=可信域名（ICP 备案）+企微后台配置+重启企微；配好真实 corp_id/secret 后，`/sign` 返回双签名，侧边栏 H5 在企微客户端内完成 `wx.config`→`wx.agentConfig`→`login`（拿到员工身份）→`getCurExternalContact`，拿到真实 external_userid。
3. **[联调+本地] 画像与历史注入**：[联调] `/profile` 返回真实精简画像；[本地] `/history` 在 mock SDK 环境下返回该客户最近消息；会话存档未开通时 `/history` 返回 `code:2000, data:[]`（降级语义）。
4. **[本地] 话术生成**：`/generate` 输入 userid（需有效会话 cookie）后返回 1 条 ≤200 字、基于画像+历史上下文的中文话术；LLM 未配 key 时返回明确错误信封（code≠2000）。
5. **[本地] 幂等与留存**：同一 seq 的会话存档消息重复拉取不重复落库；重启后从 `sync_state.last_seq` 续拉；解密失败消息跳过且不中断轮询（告警日志可查）。
6. **[本地] 降级可用**：`WECOM_SID_ENABLED=false` 时服务正常启动，`/generate` 仅用画像+scenario 出话术。
7. **[本地] 密钥零入库**：全部 `WECOM_*` 密钥走环境变量，代码/测试/文档中无真实密钥。
8. **[本地] 访问控制**：无有效会话 cookie 访问 `profile`/`history`/`generate` 返回 401 语义错误信封。

## Notes

### Risks

- **R1 会话存档开通状态未知（最大风险）**：需企业购买+合规审批。缓解：`WECOM_SID_ENABLED` 开关 + 降级路径（仅画像生成），未开通也不阻塞阶段 1/3。
- **R2 C SDK 平台兼容**：开发机 macOS(arm64)、部署 Linux(x64)，二进制不同。缓解：SDK 路径可配置，单测不依赖真实 .so（mock SDK 层）。
- **R3 可信域名需 ICP 备案**：开发联调靠 Whistle 代理绕过公网要求，但企微后台配置的域名仍需备案域名；若公司无现成备案域名需提前申请（外部依赖，跨阶段跟踪）。
- **R5 scenario/prompt 风格待产品拍板**：MVP 以自由文本 scenario + 通用销售话术 prompt 承接，正式意图分档与风格基调待销售团队/产品反馈后迭代（brainstorm 待解决问题 #4 的承接）。
- **R4 SaaS 化演进**：MVP 数据模型已带 corp_id 维度、配置集中，代开发模式升级时凭证从环境变量变为按租户存储（超出本 RFC 范围）。

### Alternatives（已驳回）

- **方案 A 扩展现有服务直接改**（无独立 wecom 包）：耦合 RPA demo 逻辑，驳回。本 RFC 实质承接 brainstorm 选定的方案 C（会话存档深度方案），A/B 为 brainstorm 中已讨论未选的备选。
- **方案 B 独立 sidebar-api 服务**：边界干净但验证期运维翻倍，作为 SaaS 阶段演进形态保留。
- **LLM 预摘要历史上下文**：MVP 输入 ≤3k token 无需摘要，成本不敏感后再考虑。
- **pypi wecom-audit 作为默认 SDK**：维护活跃度未知 + 平台二进制兼容风险，驳回为默认（保留为备选实现，见关键技术决策 #1）。
- **固定意图分档（开场/逼单/异议）作为前端选项**：意图体系需销售团队定义，MVP 先以自由文本 scenario 承接，待反馈后迭代。

### Non-Goals

群聊场景 / 一键发送到会话 / 管理后台 / 使用数据统计 / 生成记录落库与展示（MVP 裁剪：生成即用即走，留存待效果回流需求出现再做）/ SaaS 多租户与代开发上架（含标准 OAuth 登录体系，MVP 仅用轻量会话锚点）/ 移动端企微侧边栏适配（MVP 仅适配 PC 端 360-400px 宽度）/ RPA demo 能力整合。

### 边界场景处理

- **聊天对象非外部联系人**（同事间单聊）：`getCurExternalContact` 无返回 → 前端 auth.ts 明确提示「当前会话非客户单聊」，不进入助手主界面。
- **会话存档解密失败**（RSA 密钥配置错误/轮换）：单条跳过 + 告警日志，轮询不中断；连续失败率超阈值时日志升级为 ERROR 提示人工介入。
- **同事间会话数据**：GetChatData 返回全企业流，仅落库 `from_role` 可判定且对端为 external_userid（外部联系人）的单聊消息，内部单聊不入库。

### 调研来源

- [聊天工具栏接口-官方](https://developer.work.weixin.qq.com/document/path/91789) / [getCurExternalContact-官方](https://developer.work.weixin.qq.com/document/path/93592) / [获取会话内容-官方](https://developer.work.weixin.qq.com/document/path/91774) / [JS-SDK 开始使用-官方](https://developer.work.weixin.qq.com/document/path/90514) / [签名算法-官方](https://developer.work.weixin.qq.com/document/path/90506) / [常见错误排查-官方](https://developer.work.weixin.qq.com/document/path/90542) / [调试模式-官方](https://developer.work.weixin.qq.com/document/path/90315)
- [JS-SDK开发企微侧边栏-掘金](https://juejin.cn/post/7550230336647544866) / [企微侧边栏本地开发调试-掘金](https://juejin.cn/post/7532773597849452579) / [企微H5的一些坑-掘金](https://juejin.cn/post/7236541021122150456) / [wecom-sidebar 文档](https://wecom-sidebar.github.io/wecom-sidebar-docs/pre_work/config_sidebar.html)
- [PyWeWorkFinance](https://github.com/911061873/PyWeWorkFinance) / [wecom-audit(PyPI)](https://pypi.org/project/wecom-audit/) / [Python调用C库实战](https://feiyu.co/articles/Python%E8%B0%83%E7%94%A8C%E5%BA%93%E8%8E%B7%E5%8F%96%E4%BC%9A%E8%AF%9D%E5%AD%98%E6%A1%A3/) / [会话内容存档笔记](https://wener.me/notes/platform/wecom/archive)
- 竞品：[卫瓴科技](https://www.weiling.cn/) / [纷享销客话术库](https://www.fxiaoke.com/crm/information/crm-xitong-information-3-83746.html) / [探迹企微助手](https://www.tungee.com/product/wecom/) / [句子互动](https://juzibot.com/)
