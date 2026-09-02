"""企微外部联系人画像代理单测:mock httpx(MockTransport 注入)+ 隔离 SQLite。

覆盖:
1. externalcontact/get 成功 → 精简映射(external_contact 的 name/remark/remark_company/
   tags[].tag_name/description → dict)并写入 wecom_profile_cache
2. 企微 errcode!=0 → 抛 WecomContactError(含 errcode),失败不写缓存
3. 缓存命中:10min 内二次调用不重复发请求
4. 缓存过期:updated_at+600<now 惰性重取,并更新表(profile_json/updated_at)
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import pytest

from app.wecom import contact
from app.wecom.contact import WecomContactError, get_contact_profile

CORP_ID = "corp_test_id"
EXTERNAL_USERID = "wo_customer1"


def _contact_json(name: str = "张三丰") -> dict:
    """externalcontact/get 成功返回体(精简映射的输入)"""
    return {
        "errcode": 0,
        "external_contact": {
            "external_userid": EXTERNAL_USERID,
            "name": name,
            "position": "采购总监",
            "remark": "王经理-大客户",
            "remark_company": "武当科技",
            "description": "意向A产品",
            "tags": [
                {"group_name": "意向度", "tag_name": "高意向", "type": 1},
                {"group_name": "等级", "tag_name": "VIP", "type": 1},
            ],
        },
    }


def _contact_handler(calls: list[httpx.Request], bodies: list[dict]):
    """externalcontact/get mock:按请求次序依次返回 bodies(最后一个复用);其余端点拒绝"""
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "externalcontact/get" in str(request.url):
            body = bodies[min(seen["n"], len(bodies) - 1)]
            seen["n"] += 1
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"errcode": 40029, "errmsg": "invalid code"})

    return handler


@pytest.fixture()
def conn(tmp_path):
    """每个测试独立 SQLite 文件;结束还原模块级连接,避免污染其他测试"""
    c = sqlite3.connect(str(tmp_path / "test_wecom.db"))
    c.row_factory = sqlite3.Row
    contact.set_conn(c)
    yield c
    contact.set_conn(None)
    c.close()


def test_fetch_profile_maps_condensed_fields(conn):
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(_contact_handler(calls, [_contact_json()]))
    profile = get_contact_profile("AT-1", EXTERNAL_USERID, transport=transport, corp_id=CORP_ID)
    # 精简映射:仅 name/remark/remark_company→company/tags[].tag_name/description
    assert profile == {
        "userid": EXTERNAL_USERID,
        "name": "张三丰",
        "remark": "王经理-大客户",
        "company": "武当科技",
        "tags": ["高意向", "VIP"],
        "description": "意向A产品",
    }
    # 请求参数:externalcontact/get 带 access_token 与 userid
    assert len(calls) == 1
    assert "externalcontact/get" in str(calls[0].url)
    assert calls[0].url.params["access_token"] == "AT-1"
    assert calls[0].url.params["userid"] == EXTERNAL_USERID
    # 成功后写入 wecom_profile_cache 表
    row = conn.execute(
        "SELECT profile_json, updated_at FROM wecom_profile_cache WHERE corp_id=? AND external_userid=?",
        (CORP_ID, EXTERNAL_USERID),
    ).fetchone()
    assert row is not None
    assert json.loads(row["profile_json"]) == profile
    assert abs(row["updated_at"] - time.time()) < 10


def test_wecom_errcode_raises(conn):
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(_contact_handler(
        calls, [{"errcode": 84061, "errmsg": "external contact not exists"}]))
    with pytest.raises(WecomContactError, match="84061"):
        get_contact_profile("AT-1", EXTERNAL_USERID, transport=transport, corp_id=CORP_ID)
    # 失败不写缓存
    row = conn.execute(
        "SELECT * FROM wecom_profile_cache WHERE corp_id=? AND external_userid=?",
        (CORP_ID, EXTERNAL_USERID),
    ).fetchone()
    assert row is None


def test_cache_hit_within_ttl_skips_request(conn):
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(_contact_handler(calls, [_contact_json()]))
    first = get_contact_profile("AT-1", EXTERNAL_USERID, transport=transport, corp_id=CORP_ID)
    second = get_contact_profile("AT-1", EXTERNAL_USERID, transport=transport, corp_id=CORP_ID)
    assert second == first
    assert len(calls) == 1  # 二次调用走缓存,未重复发请求


def test_expired_cache_refetches_and_updates_table(conn):
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(_contact_handler(
        calls, [_contact_json(name="张三丰"), _contact_json(name="张三丰改")]))
    first = get_contact_profile("AT-1", EXTERNAL_USERID, transport=transport, corp_id=CORP_ID)
    assert first["name"] == "张三丰"
    # 手动把缓存置为过期(updated_at = now-601,超过 TTL 600s)
    conn.execute(
        "UPDATE wecom_profile_cache SET updated_at=? WHERE corp_id=? AND external_userid=?",
        (int(time.time()) - 601, CORP_ID, EXTERNAL_USERID),
    )
    conn.commit()
    second = get_contact_profile("AT-1", EXTERNAL_USERID, transport=transport, corp_id=CORP_ID)
    # 过期后重取,拿到企微新数据并更新表
    assert second["name"] == "张三丰改"
    assert len(calls) == 2
    row = conn.execute(
        "SELECT profile_json, updated_at FROM wecom_profile_cache WHERE corp_id=? AND external_userid=?",
        (CORP_ID, EXTERNAL_USERID),
    ).fetchone()
    assert json.loads(row["profile_json"]) == second
    assert abs(row["updated_at"] - time.time()) < 10  # updated_at 已刷新
