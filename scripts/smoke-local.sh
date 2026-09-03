#!/usr/bin/env bash
# 本地降级链路 smoke 测试:
#   1) /healthz           → 服务存活
#   2) /sign              → 企微凭据链路(code==2000=PASS;60020=IP 未加白,PENDING)
#   3) /profile(无 cookie) → 预期 401 且顶层 code==4001(会话校验生效)
# 用法: bash scripts/smoke-local.sh   (需在仓库根目录,且 .env 已配置企微凭证)
set -euo pipefail

cd "$(dirname "$0")/.."

BASE_URL="${BASE_URL:-http://localhost:8000}"
SIGN_URL="${SIGN_URL:-https://wecom.nonoai.com.cn/sidebar.html}"
LOG_FILE="/tmp/wecom-api-smoke.log"

# 1. 加载企微凭证(fail-fast:无 .env 直接退出)
set -a
source .env
set +a

# 2. 清理 8000 端口残留进程
if [ -n "$(lsof -ti:8000 2>/dev/null)" ]; then
  echo "[setup] 清理 8000 端口残留进程: $(lsof -ti:8000)"
  lsof -ti:8000 | xargs kill
  sleep 1
fi

# 3. 后台启动 uvicorn,等待就绪
echo "[setup] 启动 uvicorn (日志: ${LOG_FILE})"
nohup ./.venv/bin/uvicorn app.main:app --port 8000 > "${LOG_FILE}" 2>&1 &
UVICORN_PID=$!
trap 'kill "${UVICORN_PID}" 2>/dev/null || true' EXIT

sleep 3
if ! kill -0 "${UVICORN_PID}" 2>/dev/null; then
  echo "[FAIL] uvicorn 启动失败,日志尾部:"
  tail -20 "${LOG_FILE}"
  exit 1
fi

FAILURES=0

# --- 1) /healthz ---
echo
echo "== [1/3] GET /healthz =="
HEALTHZ_BODY="$(curl -sf "${BASE_URL}/healthz")"
echo "  body: ${HEALTHZ_BODY}"
if echo "${HEALTHZ_BODY}" | grep -q '"status" *: *"ok"'; then
  echo "  PASS"
else
  echo "  FAIL (预期 {\"status\":\"ok\"})"
  FAILURES=$((FAILURES + 1))
fi

# --- 2) /sign ---
echo
echo "== [2/3] GET /api/v1/wecom/sidebar/sign?url=${SIGN_URL} =="
SIGN_BODY="$(curl -s "${BASE_URL}/api/v1/wecom/sidebar/sign?url=${SIGN_URL}")"
echo "  body: ${SIGN_BODY}"
SIGN_CODE="$(echo "${SIGN_BODY}" | ./.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin).get("code",""))' 2>/dev/null || echo "parse-error")"
if [ "${SIGN_CODE}" = "2000" ]; then
  echo "  PASS (code=2000, 企微凭据链路正常)"
elif [ "${SIGN_CODE}" = "4004" ] && echo "${SIGN_BODY}" | grep -q "60020"; then
  echo "  PENDING (errcode=60020: 需在企微后台加 IP 白名单后重测)"
else
  echo "  FAIL (code=${SIGN_CODE}, 非预期结果)"
  FAILURES=$((FAILURES + 1))
fi

# --- 3) /profile 无 cookie ---
echo
echo "== [3/3] GET /api/v1/wecom/sidebar/profile?userid=woxxx (无 cookie) =="
PROFILE_BODY="$(curl -s -w $'\n%{http_code}' "${BASE_URL}/api/v1/wecom/sidebar/profile?userid=woxxx")"
PROFILE_HTTP="$(echo "${PROFILE_BODY}" | tail -1)"
PROFILE_JSON="$(echo "${PROFILE_BODY}" | sed '$d')"
echo "  http: ${PROFILE_HTTP}  body: ${PROFILE_JSON}"
PROFILE_CODE="$(echo "${PROFILE_JSON}" | ./.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin).get("code",""))' 2>/dev/null || echo "parse-error")"
if [ "${PROFILE_HTTP}" = "401" ] && [ "${PROFILE_CODE}" = "4001" ]; then
  echo "  PASS (401 + code=4001, 会话校验正确拒绝)"
else
  echo "  FAIL (预期 401 + code=4001, 实际 ${PROFILE_HTTP} + code=${PROFILE_CODE})"
  FAILURES=$((FAILURES + 1))
fi

# --- 汇总 ---
echo
echo "== 汇总: ${FAILURES} 个 FAIL =="
exit "${FAILURES}"
