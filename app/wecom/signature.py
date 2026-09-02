"""JS-SDK 签名算法:sha1("jsapi_ticket=..&noncestr=..&timestamp=..&url=..") hex 小写。"""
import hashlib


def jsapi_signature(ticket: str, nonce_str: str, timestamp: str, url: str) -> str:
    """企微 JS-SDK 签名。url 含 # 时截断 # 及其后内容(官方要求,前端路由常见)"""
    url = url.split("#", 1)[0]
    raw = f"jsapi_ticket={ticket}&noncestr={nonce_str}&timestamp={timestamp}&url={url}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
