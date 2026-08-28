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
