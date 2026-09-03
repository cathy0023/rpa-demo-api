"""同源部署下 /healthz 不被静态挂载遮蔽。

回归背景:mount_frontend_dist 原先在 /healthz 路由之前注册,
Mount("/") 按顺序匹配拦截一切,dist 存在时 /healthz 404
(单测全绿是因为 CI 环境无 dist 目录,挂载被跳过)。
修复:healthz 先注册,mount 兜底其余路径。
"""
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_reachable_when_dist_exists(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "sidebar.html").write_text("<html>sidebar</html>", encoding="utf-8")

    from app.main import mount_frontend_dist

    assert mount_frontend_dist(app, dist) is True
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/sidebar.html").status_code == 200
    assert client.get("/api/v1/wecom/sidebar/profile").status_code == 401
