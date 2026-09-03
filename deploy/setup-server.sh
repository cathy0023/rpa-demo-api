#!/usr/bin/env bash
# =============================================================================
# setup-server.sh — 阶段 1: 在 47.89.150.106 上为 wecom.nonoai.com.cn 签发证书
#                    并部署完整 nginx 443 反代配置
#
# 使用方式: 以 root 在服务器上执行  bash setup-server.sh
#
# 前置条件:
#   - DNS: wecom.nonoai.com.cn A 记录 -> 47.89.150.106 已生效
#   - nginx/1.20.1 已运行, 已有 mgv.nonoai.com.cn vhost (certbot 流程该机已跑通过)
#   - 443 端口尚无 wecom 的 vhost/证书 (本脚本负责创建)
#
# 脚本流程 (幂等, 可重复执行):
#   1. 检查/安装 certbot
#   2. 创建 ACME webroot 目录 /var/www/certbot
#   3. 部署「80-only 版」nginx 配置 (仅 ACME 验证 + 301), reload
#   4. certbot webroot 模式签发证书
#   5. 部署「完整 443 版」nginx 配置, reload
#   6. 检查证书续期机制 (cron / systemd timer)
# =============================================================================
set -euo pipefail

DOMAIN="wecom.nonoai.com.cn"
NGINX_CONF="/etc/nginx/conf.d/${DOMAIN}.conf"
WEBROOT="/var/www/certbot"

echo "==> [0/6] 环境确认"
echo "    域名        : ${DOMAIN}"
echo "    nginx 配置  : ${NGINX_CONF}"
echo "    ACME webroot: ${WEBROOT}"

# -----------------------------------------------------------------------------
# 步骤 1: 检查/安装 certbot
# -----------------------------------------------------------------------------
echo "==> [1/6] 检查 certbot"
if command -v certbot >/dev/null 2>&1; then
    echo "    certbot 已安装: $(certbot --version 2>&1)"
else
    echo "    certbot 未安装, 通过 yum 安装..."
    yum install -y certbot
    echo "    安装完成: $(certbot --version 2>&1)"
fi

# -----------------------------------------------------------------------------
# 步骤 2: 创建 ACME webroot 目录
# -----------------------------------------------------------------------------
echo "==> [2/6] 创建 webroot 目录 ${WEBROOT}"
mkdir -p "${WEBROOT}"

# -----------------------------------------------------------------------------
# 步骤 3: 部署 80-only 版 nginx 配置 (签证书前不能引用尚不存在的证书文件)
# -----------------------------------------------------------------------------
echo "==> [3/6] 写入 80-only 版 nginx 配置 -> ${NGINX_CONF}"
cat > "${NGINX_CONF}" <<'NGINX_HTTP_ONLY'
# wecom.nonoai.com.cn — 阶段1临时配置(仅80端口)
# 用途: certbot http-01 验证 + HTTP->HTTPS 跳转
# 证书签发成功后会被 setup-server.sh 覆盖为完整 443 版本
server {
    listen 80;
    server_name wecom.nonoai.com.cn;

    # ACME http-01 验证路径, 必须直出文件系统
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    # 其余请求跳转 HTTPS
    location / {
        return 301 https://$host$request_uri;
    }
}
NGINX_HTTP_ONLY

echo "    语法检查: nginx -t"
nginx -t
echo "    重载 nginx"
nginx -s reload
echo "    80-only 配置已生效"

# -----------------------------------------------------------------------------
# 步骤 4: certbot webroot 模式签发证书
# -----------------------------------------------------------------------------
echo "==> [4/6] certbot 签发证书 (webroot 模式)"
if [ -d "/etc/letsencrypt/live/${DOMAIN}" ]; then
    echo "    检测到 ${DOMAIN} 证书已存在, 跳过签发 (幂等)"
    echo "    如需强制重签请手动执行:"
    echo "      certbot certonly --webroot -w ${WEBROOT} -d ${DOMAIN} --force-renewal"
else
    # --register-unsafely-without-email: 不注册邮箱(错过续期不会有邮件提醒)
    # 注意: 若企微后台/安全策略要求真实邮箱, 可替换为:
    #   --email you@example.com --agree-tos --no-eff-email
    certbot certonly \
        --webroot -w "${WEBROOT}" \
        -d "${DOMAIN}" \
        --non-interactive \
        --agree-tos \
        --register-unsafely-without-email
    echo "    证书已签发 -> /etc/letsencrypt/live/${DOMAIN}/"
fi

# -----------------------------------------------------------------------------
# 步骤 5: 部署完整 443 配置并 reload
# -----------------------------------------------------------------------------
echo "==> [5/6] 写入完整 443 nginx 配置 -> ${NGINX_CONF}"
cat > "${NGINX_CONF}" <<'NGINX_FULL'
# =============================================================================
# wecom.nonoai.com.cn — 完整版 (80 ACME+301 / 443 TLS 反代 127.0.0.1:8000)
# 由 setup-server.sh 自动生成
# =============================================================================
server {
    listen 80;
    server_name wecom.nonoai.com.cn;

    # 保留 ACME 验证路径, 供续期使用
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    # nginx 1.20.1 不支持 `http2 on;` 新语法(1.25.1+), 使用旧写法
    listen 443 ssl http2;
    server_name wecom.nonoai.com.cn;

    ssl_certificate     /etc/letsencrypt/live/wecom.nonoai.com.cn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/wecom.nonoai.com.cn/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        # SSE 流式响应必须关闭缓冲
        proxy_buffering off;
        # RPA 长任务放宽超时
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
NGINX_FULL

echo "    语法检查: nginx -t"
nginx -t
echo "    重载 nginx"
nginx -s reload
echo "    443 配置已生效, HTTPS 站点已上线"

# -----------------------------------------------------------------------------
# 步骤 6: 证书续期机制检查
# -----------------------------------------------------------------------------
echo "==> [6/6] 检查证书自动续期机制"

# 6a. certbot 自带的 systemd timer (CentOS 8+/RHEL 常见)
if systemctl list-timers --all 2>/dev/null | grep -q certbot; then
    echo "    检测到 certbot.timer (systemd), 自动续期已就绪"
# 6b. crontab 中已有的续期任务 (该机 mgv.nonoai.com.cn 可能已配置)
elif crontab -l 2>/dev/null | grep -q certbot; then
    echo "    检测到 crontab 中已有 certbot 续期任务 (大概率是 mgv 域名的), 复用即可"
    echo "    建议确认该任务带 reload hook, 若无建议改为:"
    echo '      certbot renew --deploy-hook "nginx -s reload"'
else
    echo "    未检测到任何续期机制, 新增 cron (每天 03:17 尝试续期, 仅临期才真正签发):"
    ( crontab -l 2>/dev/null; \
      echo '17 3 * * * certbot renew --deploy-hook "nginx -s reload" --quiet' ) \
      | crontab -
    echo "    已写入 crontab:"
    crontab -l | grep certbot || true
fi

# -----------------------------------------------------------------------------
# 冒烟验证
# -----------------------------------------------------------------------------
echo "==> 冒烟验证"
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://${DOMAIN}/" || true)
HTTPS_CODE=$(curl -s -o /dev/null -w '%{http_code}' "https://${DOMAIN}/healthz" || true)
echo "    http://${DOMAIN}/            -> HTTP ${HTTP_CODE} (期望 301)"
echo "    https://${DOMAIN}/healthz    -> HTTP ${HTTPS_CODE} (502=正常,后端未部署; 000=TLS异常)"

echo "==> setup-server.sh 完成"
echo "    下一步: 按 deploy/README.md 阶段2 上传应用代码并执行 deploy-app.sh"
