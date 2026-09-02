"""同源部署静态托管单测:./dist 存在时挂载 StaticFiles(html=True),API 优先。

- mount 时机:include_router 之后注册;FastAPI 按注册顺序匹配,API 前缀命中优先,
  静态兜底 —— /api/v1/... 与 /sidebar.html 必须同时可达
- dist 不存在 → 跳过挂载(本地 dev 不受影响)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("WECOM_SID_ENABLED", "false")

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import mount_frontend_dist
from app.wecom import router as wecom_router
from app.wecom.deps import WecomAuthError, wecom_auth_error_response
from app.wecom.token import WecomTokenClient


def test_mount_skipped_when_dist_missing(tmp_path):
    app = FastAPI()
    mounted = mount_frontend_dist(app, dist_dir=tmp_path / "nope")
    assert mounted is False


def test_mount_serves_sidebar_html_and_api_still_works(tmp_path):
    """dist 存在:挂载后 /sidebar.html 200;/api/v1 路径不被静态兜底覆盖"""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "sidebar.html").write_text("<html>sidebar</html>", encoding="utf-8")

    app = FastAPI()
    app.add_exception_handler(WecomAuthError, lambda r, e: wecom_auth_error_response(e))
    app.include_router(wecom_router.api_router)
    assert mount_frontend_dist(app, dist_dir=dist) is True

    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, json={"errcode": 0, "access_token": "AT-1", "expires_in": 7200}))
    wecom_router.configure(
        cfg=__import__("app.wecom.config", fromlist=["WecomConfig"]).WecomConfig(
            corp_id="c", app_secret="s", cookie_secret="0123456789abcdef"),
        token_client_factory=lambda: WecomTokenClient(
            corp_id="c", app_secret="s", transport=transport),
        http_transport=transport,
    )
    with TestClient(app) as client:
        # API 路径仍命中 API 路由(守卫 401 证明进入的是 wecom 路由而非静态)
        resp = client.get("/api/v1/wecom/sidebar/profile", params={"userid": "cust"})
        assert resp.status_code == 401
        assert resp.json()["code"] == 4001
        # 静态文件可达
        resp = client.get("/sidebar.html")
        assert resp.status_code == 200
        assert "sidebar" in resp.text
        # 未知路径走静态 404(而非 API)
        assert client.get("/no-such-asset.js").status_code == 404
