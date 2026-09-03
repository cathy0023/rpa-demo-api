# wecom.nonoai.com.cn 部署手册

目标服务器：**47.89.150.106**（阿里云 ECS，root 登录，nginx/1.20.1，已有 mgv.nonoai.com.cn vhost 与 certbot 经验）
应用形态：FastAPI（`app.main:app`，uvicorn，127.0.0.1:8000），`mount_frontend_dist(app,"dist")` 在 `./dist` 存在时自动托管前端静态文件（API 优先、静态兜底，`/healthz` 在 mount 前注册，不受影响）。

## 本目录文件

| 文件 | 用途 |
|---|---|
| `nginx-wecom.nonoai.com.cn.conf` | 完整 nginx 配置（80+443），供参考/回滚对照；实际由 setup-server.sh 内嵌部署 |
| `setup-server.sh` | 阶段 1：签发证书 + 部署 nginx 443 反代 |
| `deploy-app.sh` | 阶段 2：部署应用（venv + .env + systemd + 冒烟） |
| `wecom-admin-checklist.md` | 给企业管理员的企微后台配置清单 |
| `README.md` | 本手册 |

## 阶段 1：服务器初始化与证书签发

本机 SSH 被服务器拒绝，以下命令请由你复制粘贴到服务器终端执行。

```bash
# 1. 上传脚本到服务器
scp /Users/chenyan/Documents/sop-mini/rpa-demo-api-wecom-sidebar/deploy/setup-server.sh root@47.89.150.106:/root/

# 2. 在服务器上执行
ssh root@47.89.150.106   # 或用控制台 Web 终端 / 密码登录
bash /root/setup-server.sh
```

脚本会依次：装 certbot（如无）→ 建 `/var/www/certbot` → 写 80-only 配置 → `certbot certonly --webroot` 签发 `wecom.nonoai.com.cn` 证书 → 覆盖为完整 443 配置 → 检查续期机制 → 冒烟。

**阶段 1 成功判据**：
- `curl -si http://wecom.nonoai.com.cn/ | head -1` → `HTTP/1.1 301`
- `curl -si https://wecom.nonoai.com.cn/healthz` → TLS 握手成功，HTTP **502**（后端还没部署，502 即正常）

**证书续期**：若服务器上 mgv 域名已有续期 cron/timer，直接复用，建议命令带 reload hook：
`certbot renew --deploy-hook "nginx -s reload"`（setup-server.sh 已自动检测并在缺失时写入 cron）。

## 阶段 2：上传应用并启动

### 2.1 本地发布前检查清单（先在本地 smoke 通过）

- [ ] 本地 `pytest` 全绿（后端单测）
- [ ] 本地 `uvicorn app.main:app` + 把前端 dist 拷到后端目录的 `./dist`，浏览器访问 `http://127.0.0.1:8000/sidebar.html` 正常打开、能调 API
- [ ] `scripts/` 下 smoke 脚本（如 `smoke_sidebar.py`）通过
- [ ] 本地 `.env` 已填真实 `WECOM_APP_SECRET`（部署时手动复制，**不要**把 .env 传进 git）
- [ ] 企微 `WW_verify_*.txt` 校验文件已拿到，放进前端 dist 目录（见下文）

### 2.2 上传代码

```bash
# 后端代码（排除本地垃圾）+ 前端 dist（含 WW_verify 文件）
ssh root@47.89.150.106 'mkdir -p /opt/wecom-sidebar'
rsync -avz --exclude .venv --exclude .env --exclude '*.db' --exclude dist \
     /Users/chenyan/Documents/sop-mini/rpa-demo-api-wecom-sidebar/ \
     root@47.89.150.106:/opt/wecom-sidebar/

# 前端 dist（确认已包含 WW_verify_*.txt 再传）
scp -r /Users/chenyan/Documents/sop-mini/rpa-demo-web-wecom-sidebar/dist \
     root@47.89.150.106:/opt/wecom-sidebar/

# 上传部署脚本
scp /Users/chenyan/Documents/sop-mini/rpa-demo-api-wecom-sidebar/deploy/deploy-app.sh root@47.89.150.106:/root/
```

### 2.3 服务器执行部署脚本

```bash
bash /root/deploy-app.sh
```

脚本会：检查产物 → 建 venv 装 requirements.txt → 生成 `.env`（**已存在则不覆盖**；`WECOM_SID_COOKIE_SECRET` 为空会自动 `openssl rand -hex 32` 生成）→ 写 systemd unit `wecom-sidebar.service` → 启动 → 冒烟 `/healthz`。

### 2.4 手工补密钥（首次必做）

```bash
vi /opt/wecom-sidebar/.env
# 把 WECOM_APP_SECRET=<在此粘贴WECOM_APP_SECRET真实值> 替换为本地 .env 中的真实值
systemctl restart wecom-sidebar
```

（也可在首次 scp 后端代码时把本地 `.env` 一并传上去覆盖模板，但注意 `.env` 含密钥，传输后确认服务器上权限 600。）

### 2.5 端到端验证

```bash
curl -s https://wecom.nonoai.com.cn/healthz                 # 期望 200 {"status":"ok"}
curl -sI https://wecom.nonoai.com.cn/sidebar.html | head -1 # 期望 200
```

### 2.6 日常运维

```bash
systemctl status wecom-sidebar          # 服务状态
journalctl -u wecom-sidebar -f          # 实时日志
systemctl restart wecom-sidebar         # 重启
# 更新代码：重复 2.2 的 rsync/scp，然后 systemctl restart wecom-sidebar
# nginx 配置改动：/etc/nginx/conf.d/wecom.nonoai.com.cn.conf，改后 nginx -t && nginx -s reload
```

## WW_verify 域名校验文件放置说明

企微后台配置「可信域名」时，会要求下载一个 `WW_verify_*.txt` 文件证明域名归属：

1. 在企微后台下载该 txt 文件；
2. 放到前端构建产物目录 `/Users/chenyan/Documents/sop-mini/rpa-demo-web-wecom-sidebar/dist/` 下（与 `index.html` 同级）；
3. 随 dist 一起 scp 到 `/opt/wecom-sidebar/dist/`；
4. 后端 `mount_frontend_dist` 挂载的 StaticFiles 会自动响应
   `https://wecom.nonoai.com.cn/WW_verify_*.txt`，无需额外配置；
5. 回企微后台点击「验证」，通过后再保存可信域名。

> 注意：每次重新构建前端 dist 后，记得把 WW_verify 文件重新放回 dist 再上传。

## 与企微后台的配合

`wecom-admin-checklist.md` 需要企业管理员配合完成（可信域名、可信 IP、可见范围、聊天工具栏页面地址），建议在阶段 1 完成后、阶段 2 验证前同步推进。
