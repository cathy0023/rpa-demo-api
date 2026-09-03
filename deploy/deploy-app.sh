#!/usr/bin/env bash
# =============================================================================
# deploy-app.sh — 阶段 2: 在服务器上部署应用 (后端代码 + 前端 dist + systemd 服务)
#
# 使用方式: 以 root 在服务器上执行  bash deploy-app.sh
#
# 前置条件 (必须先由用户 scp 上传, 否则脚本开头即报错退出):
#   - 后端代码已上传到 /opt/wecom-sidebar   (app/ requirements.txt 等)
#   - 前端构建产物已上传到 /opt/wecom-sidebar/dist/  (sidebar.html index.html assets/)
#   - 443 证书已由 setup-server.sh 签发好 (本脚本不依赖证书, 但上线需要)
#
# 脚本流程 (幂等, 可重复执行):
#   1. 检查后端代码与前端 dist 是否就位
#   2. 创建 python3 venv 并安装 requirements.txt
#   3. 生成 .env (已存在则不覆盖; WECOM_SID_COOKIE_SECRET 为空则自动生成)
#   4. 写入 systemd unit 并启动 wecom-sidebar 服务
#   5. 冒烟验证 /healthz
# =============================================================================
set -euo pipefail

APP_DIR="/opt/wecom-sidebar"
ENV_FILE="${APP_DIR}/.env"
UNIT_FILE="/etc/systemd/system/wecom-sidebar.service"
SERVICE="wecom-sidebar"

echo "==> [0/5] 部署目标: ${APP_DIR}"

# -----------------------------------------------------------------------------
# 步骤 1: 检查上传产物是否就位 (不就位则打印 scp 示例后退出)
# -----------------------------------------------------------------------------
echo "==> [1/5] 检查上传产物"
if [ ! -f "${APP_DIR}/app/main.py" ]; then
    echo "错误: 未找到 ${APP_DIR}/app/main.py — 后端代码未上传"
    echo "请先在本机执行 (示例):"
    echo "  ssh root@47.89.150.106 'mkdir -p /opt/wecom-sidebar'"
    echo "  rsync -avz --exclude .venv --exclude .env --exclude '*.db' --exclude dist \\"
    echo "       /Users/chenyan/Documents/sop-mini/rpa-demo-api-wecom-sidebar/ \\"
    echo "       root@47.89.150.106:/opt/wecom-sidebar/"
    echo "  scp -r /Users/chenyan/Documents/sop-mini/rpa-demo-web-wecom-sidebar/dist \\"
    echo "       root@47.89.150.106:/opt/wecom-sidebar/"
    exit 1
fi
if [ ! -f "${APP_DIR}/dist/index.html" ]; then
    echo "错误: 未找到 ${APP_DIR}/dist/index.html — 前端 dist 未上传"
    echo "请先在本机执行 (示例):"
    echo "  scp -r /Users/chenyan/Documents/sop-mini/rpa-demo-web-wecom-sidebar/dist \\"
    echo "       root@47.89.150.106:/opt/wecom-sidebar/"
    echo "提示: 企微域名校验文件 WW_verify_*.txt 也一并放进 dist/ 再上传"
    exit 1
fi
echo "    后端代码 OK: ${APP_DIR}/app/main.py"
echo "    前端 dist OK: ${APP_DIR}/dist/index.html"

# -----------------------------------------------------------------------------
# 步骤 2: 创建 venv 并安装依赖
# -----------------------------------------------------------------------------
echo "==> [2/5] 创建 venv 并安装依赖"
cd "${APP_DIR}"
if [ ! -x ".venv/bin/python" ]; then
    python3 -m venv .venv
    echo "    venv 已创建"
else
    echo "    venv 已存在, 跳过创建 (幂等)"
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "    依赖安装完成: $( .venv/bin/pip list 2>/dev/null | grep -ciE 'fastapi|uvicorn|httpx' ) 个核心包已就位"

# -----------------------------------------------------------------------------
# 步骤 3: 生成 .env (已存在则保留, 绝不覆盖线上配置)
# -----------------------------------------------------------------------------
echo "==> [3/5] 处理 ${ENV_FILE}"
if [ -f "${ENV_FILE}" ]; then
    echo "    .env 已存在, 保留现有配置 (幂等, 不覆盖)"
else
    echo "    .env 不存在, 生成模板 (请手工填入 WECOM_APP_SECRET 真实值!)"
    # 先生成随机 cookie secret, 避免模板留空
    GENERATED_COOKIE_SECRET=$(openssl rand -hex 32)
    cat > "${ENV_FILE}" <<ENV_TEMPLATE
# =============================================================================
# wecom-sidebar 生产环境变量 (由 deploy-app.sh 生成)
# 注意: KEY=VALUE 格式与 systemd EnvironmentFile 兼容, 勿加 export 前缀
# =============================================================================

# --- 企业微信基础凭证 ---
WECOM_CORP_ID=ww5bc9cd87ff71ada4
WECOM_AGENT_ID=1000004
# TODO(必填): 从本地 .env 复制真实 WECOM_APP_SECRET 到这里, 服务才能调用企微 API
WECOM_APP_SECRET=<在此粘贴WECOM_APP_SECRET真实值>

# --- 侧边栏会话 Cookie ---
# 信任域名 (企微 OAuth 回调校验用)
WECOM_SID_TRUSTED_DOMAIN=wecom.nonoai.com.cn
# Cookie 签名密钥: 首次生成已自动填入; 如需轮换请改为新随机值
WECOM_SID_COOKIE_SECRET=${GENERATED_COOKIE_SECRET}
# 生产走 HTTPS, Cookie 必须带 Secure
WECOM_SID_COOKIE_SECURE=true
# 会话存档未开通, 关闭 (降级运行, 不影响侧边栏基础功能)
WECOM_SID_ENABLED=false

# --- LLM 配置 (RPA 使用; 留空则 RPA 功能不可用) ---
RPA_DEMO_LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
# TODO(可选): 如需 RPA 功能, 填入 LLM API Key
RPA_DEMO_LLM_API_KEY=
RPA_DEMO_LLM_MODEL=

# --- 存储 ---
RPA_DEMO_SQLITE_PATH=/opt/wecom-sidebar/rpa_demo.db
ENV_TEMPLATE
    chmod 600 "${ENV_FILE}"
    echo "    模板已生成 (权限 600), cookie secret 已随机写入"
fi

# 幂等兜底: 若 .env 存在但 cookie secret 为空, 自动生成并写回
if grep -qE '^WECOM_SID_COOKIE_SECRET=$' "${ENV_FILE}"; then
    EMPTY_SECRET=$(openssl rand -hex 32)
    sed -i "s|^WECOM_SID_COOKIE_SECRET=$|WECOM_SID_COOKIE_SECRET=${EMPTY_SECRET}|" "${ENV_FILE}"
    echo "    检测到 WECOM_SID_COOKIE_SECRET 为空, 已自动生成并写回"
fi

# -----------------------------------------------------------------------------
# 步骤 4: 写入 systemd unit 并启动
# -----------------------------------------------------------------------------
echo "==> [4/5] 写入 systemd unit -> ${UNIT_FILE}"
cat > "${UNIT_FILE}" <<'UNIT_TEMPLATE'
# =============================================================================
# wecom-sidebar — FastAPI 服务 (uvicorn)
# 由 deploy-app.sh 生成
# 说明: .env 采用 KEY=VALUE 格式, 与 systemd EnvironmentFile 原生兼容
# =============================================================================
[Unit]
Description=Wecom Sidebar FastAPI app (uvicorn on 127.0.0.1:8000)
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/wecom-sidebar
EnvironmentFile=/opt/wecom-sidebar/.env
ExecStart=/opt/wecom-sidebar/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT_TEMPLATE

systemctl daemon-reload
systemctl enable --now "${SERVICE}" >/dev/null 2>&1 || systemctl restart "${SERVICE}"
sleep 2
echo "---- systemctl status (摘要) ----"
systemctl --no-pager --lines=0 status "${SERVICE}" || true

# -----------------------------------------------------------------------------
# 步骤 5: 冒烟验证
# -----------------------------------------------------------------------------
echo "==> [5/5] 冒烟验证 /healthz"
HEALTH=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/healthz || true)
if [ "${HEALTH}" = "200" ]; then
    echo "    http://127.0.0.1:8000/healthz -> HTTP 200, 服务正常"
    echo "    经 nginx 的完整链路: curl -s https://wecom.nonoai.com.cn/healthz"
else
    echo "    !! /healthz 返回 HTTP ${HEALTH:-无响应}, 服务可能未正常启动"
    echo "    排查命令:"
    echo "      journalctl -u ${SERVICE} -n 30 --no-pager"
    echo "    常见原因:"
    echo "      1) .env 中 WECOM_APP_SECRET 未填 -> 启动时校验失败"
    echo "      2) requirements.txt 安装失败 -> 手动执行 .venv/bin/pip install -r requirements.txt 查看"
    echo "      3) 8000 端口被占用 -> ss -ltnp | grep 8000"
    exit 1
fi

echo "==> deploy-app.sh 完成"
echo "    验证清单:"
echo "      curl -s https://wecom.nonoai.com.cn/healthz"
echo "      浏览器打开 https://wecom.nonoai.com.cn/sidebar.html"
