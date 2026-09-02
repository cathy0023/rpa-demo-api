# rpa-demo-api

通过七小饱 RPA 平台给客户发消息的独立 demo 后端(FastAPI)。

完整闭环:客户消息 → RPA 回调(验签+AES-CTR+zstd) → AI 话术 → SQLite 落库 → SSE 监控推送 → 回发客户。

## 运行模式

- **mock(默认)**:内置模拟 RPA 控制台触发,mock 密钥加解密自洽,出站不出网。无七小饱密钥即可完整演示。
- **real**:真实回调 + 出站发送(待拿到七小饱密钥后接线,见 service.py 中标注处)。

## 启动

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| RPA_DEMO_MODE | mock | mock / real |
| RPA_DEMO_MOCK_APP_SECRET | mock-app-secret | mock 回调验签密钥 |
| RPA_DEMO_MOCK_AESKEY | mock-aes-key | mock AES 解密密钥 |
| RPA_DEMO_LLM_BASE_URL | https://api.openai.com/v1 | OpenAI 兼容接口 |
| RPA_DEMO_LLM_API_KEY | (空) | LLM key,未配置时链路优雅降级 |
| RPA_DEMO_LLM_MODEL | gpt-4o-mini | 模型名 |
| RPA_DEMO_SQLITE_PATH | rpa_demo.db | SQLite 路径 |

## API(前缀 /api/v1/rpa-demo)

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /callback | 七小饱回调接收(X-Sign/X-Nonce/X-Timestamp + 密文 body) |
| POST | /mock/send-customer-message | 模拟客户发消息 {vid, sender, content} |
| GET | /monitor/events | SSE 实时监控事件 |
| GET | /monitor/conversations | 会话列表 |
| GET | /monitor/conversations/{id}/messages | 单会话消息 |
| GET | /monitor/customers | 客户列表 |

## 测试

```bash
python -m pytest tests/ -v
```

## 企微侧边栏配置

企微侧边栏（`/api/v1/wecom/sidebar/*` + 前端 `sidebar.html`）提供「打开侧边栏 → 识别客户 → 画像+历史 → 生成话术」闭环。变量名以 `app/wecom/config.py` / `app/wecom/sync.py` 实际读取为准，占位模板见 `.env.example`。密钥（app_secret / cookie_secret / msgaudit 私钥）只走环境变量，绝不入库。

### 一、企微管理后台准备

1. **创建自建应用**：管理后台 → 应用管理 → 自建 → 创建应用，记录 `CorpID`（我的企业页）→ `WECOM_CORP_ID`、`AgentId` → `WECOM_AGENT_ID`、`Secret` → `WECOM_APP_SECRET`。
2. **网页授权及 JS-SDK 可信域名**：应用详情 → 开发者接口 → 网页授权及 JS-SDK → 配置可信域名。要求：
   - 域名须通过 ICP 备案；
   - 下载「域名归属验证」文件放到站点根目录（后端需能响应 `https://<域名>/WW_verify_xxxx.txt`）。
3. **配置到聊天工具栏**：应用详情 → 配置到聊天工具栏，页面地址填 `https://<域名>/sidebar.html`。
4. **可见范围**：配置应用可见范围，仅侧边栏目标使用者的部门/成员可见。

### 二、会话存档（可选）

未开通会话存档时保持 `WECOM_SID_ENABLED=false`（默认），应用照常可用，仅 `/history` 走降级返回空数组。开通步骤：

1. 管理后台 → 安全与管理 → 管理工具 → 会话存档，开通并配置 **RSA 公钥**（本地生成 RSA 密钥对，公钥填后台，私钥全文放 `WECOM_SID_MSGAUDIT_PRIVATE_KEY`）。
2. 下载官方 **会话存档 C SDK**（`libWeWorkFinanceSdk_C.so`/`.dylib`），将其绝对路径填 `WECOM_SID_SDK_PATH`。
3. 设 `WECOM_SID_ENABLED=true`，重启后 lifespan 自动启动后台同步任务（轮询间隔 `WECOM_SID_POLL_INTERVAL`，默认 5 秒）。

### 三、本地联调（Whistle 代理）

可信域名必须是备案域名，本地无法直接通过企微校验，用 Whistle 把可信域名代理到本机：

```
# whistle 规则(默认端口 8899)
<你的备案域名>/sidebar http://localhost:5274/sidebar.html
<你的备案域名>/api http://localhost:8000/api
```

- 企微客户端配代理指向 Whistle 后，打开聊天工具栏页面即命中本地 vite（5274）与 uvicorn（8000）。
- `WECOM_SID_TRUSTED_DOMAIN` 填该备案域名（与 JS-SDK 签名 url 域名一致）；本地纯 dev 联调可留空，留空 = `/sign` 放行任意域名（仅限开发）。
- 页面地址仍填 `https://<域名>/sidebar.html`。

### 四、启动

```bash
# 后端(8000):WECOM_SID_ENABLED=false 时会话存档同步不启动(降级),其余端点正常
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 前端 sidebar(vite 5274,/api 代理到 8000)
pnpm dev
# 访问 http://localhost:5274/sidebar.html
```

降级开关：`WECOM_SID_ENABLED=false`（或缺省）→ 同步任务不启动、`/history` 返回空数组，画像/生成不受影响；`true` 但缺 SDK 或私钥 → 同步任务降级为 Disabled 并记日志，不阻断应用启动。
