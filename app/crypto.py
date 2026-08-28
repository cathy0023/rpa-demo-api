"""七小饱(企销宝)回调加解密 —— 与 mgvopen MR215 最新 Go 实现逐语义对齐

流程:
- 接收侧 decrypt_rpa_body: 验签 → AES-CTR 解密 → zstd 解压
- 发送侧 encrypt_rpa_body: 明文 JSON → zstd 压缩 → AES-CTR 加密 → 签名

算法契约(qixiaobaoapidoc.md + MR215 真实联调修正):
1. 验签: str = timestamp + "\\n" + nonce + "\\n" + java_utf8_replace(body密文)
   X-Sign = Base64(HmacSHA256(appSecret, str))
   !important 企销宝服务端为 Java,签名前执行 new String(body, UTF_8):
   密文中非法 UTF-8 序列被替换为 U+FFFD 后再 getBytes(UTF-8) 参与签名。
   因此签名输入 ≠ 原始密文字节,必须先用 java_utf8_replace 精确复刻
   Java CharsetDecoder(UTF-8, REPLACE) 语义(实测 Python 内置
   bytes.decode(errors="replace") 与 Java 行为不一致,不可替代)。
2. 解密: AES/CTR/NoPadding, key = SHA-256(aesKey), 固定 16 字节全零 IV
3. 压缩: zstd(解压加 1MB 上限防炸弹)
"""
import base64
import hashlib
import hmac
import json

try:
    import zstandard as zstd

    _ZSTD_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ZSTD_AVAILABLE = False

# zstd 解压输出上限(对齐 MR215 防解压炸弹)
MAX_DECOMPRESSED = 1 << 20


class ZstdUnavailableError(RuntimeError):
    """zstandard 未安装,无法处理七小饱 zstd 压缩报文"""


def _require_zstd() -> None:
    if not _ZSTD_AVAILABLE:
        raise ZstdUnavailableError(
            "zstandard 未安装,无法处理七小饱 zstd 压缩报文。请执行: pip install zstandard"
        )


def _zstd_compress(data: bytes) -> bytes:
    _require_zstd()
    return zstd.ZstdCompressor().compress(data)


def _zstd_decompress(data: bytes) -> bytes:
    _require_zstd()
    out = zstd.ZstdDecompressor().decompress(data, max_output_size=MAX_DECOMPRESSED)
    if len(out) > MAX_DECOMPRESSED:
        raise ValueError(f"decompressed size {len(out)} exceeds limit {MAX_DECOMPRESSED}")
    return out


def create_sign(app_secret: str, body: bytes, timestamp: str, nonce: str) -> str:
    """生成 X-Sign。body 为密文字节,签名前先做 Java UTF-8 替换(见模块 docstring)"""
    str_to_sign = (
        timestamp.encode("utf-8")
        + b"\n"
        + nonce.encode("utf-8")
        + b"\n"
        + java_utf8_replace(body)
    )
    mac = hmac.new(app_secret.encode("utf-8"), str_to_sign, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def verify_sign(app_secret: str, body: bytes, timestamp: str, nonce: str, sign: str) -> bool:
    """常量时间比对签名,防时序侧信道"""
    expected = create_sign(app_secret, body, timestamp, nonce)
    return hmac.compare_digest(expected.encode("utf-8"), sign.encode("utf-8"))


def _aes_ctr_transform(data: bytes, aes_key: str) -> bytes:
    """AES/CTR/NoPadding,key=SHA-256(aesKey),全零 IV。CTR 加解密同一操作(XOR key stream)"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key_bytes = hashlib.sha256(aes_key.encode("utf-8")).digest()
    cipher = Cipher(algorithms.AES(key_bytes), modes.CTR(b"\x00" * 16))
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


def encrypt_rpa_body(payload: dict, aes_key: str, app_secret: str, timestamp: str, nonce: str) -> tuple[bytes, str]:
    """发送侧:明文 JSON → zstd 压缩 → AES-CTR 加密 → 签名。返回 (密文 body, X-Sign)"""
    plain_json = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    encrypted = _aes_ctr_transform(_zstd_compress(plain_json), aes_key)
    sign = create_sign(app_secret, encrypted, timestamp, nonce)
    return encrypted, sign


def decrypt_rpa_body(body: bytes, aes_key: str, app_secret: str, timestamp: str, nonce: str, sign: str) -> bytes:
    """接收侧:验签 → AES-CTR 解密 → zstd 解压 → 明文 JSON 字节。验签失败抛 ValueError"""
    if not verify_sign(app_secret, body, timestamp, nonce, sign):
        raise ValueError("rpa callback sign mismatch")
    return _zstd_decompress(_aes_ctr_transform(body, aes_key))


def java_utf8_replace(body: bytes) -> bytes:
    """精确复刻 Java CharsetDecoder(UTF-8, REPLACE) 的 new String(byte[]) 语义,
    再按 UTF-8 编码回字节(等价 Java str.getBytes(UTF_8))。

    Java 对 malformed 序列的替换规则(摘自 MR215, 已用企销宝真实回调验证):
      - overlong(C0/C1、E0 80-9F、F0 80-8F)/超范围(F4 90-BF)/非法 leading(F5-FF):
        逐个字节替换(每字节 1 个 U+FFFD)
      - surrogate(ED A0-BF): 整个序列替换 1 个 U+FFFD
      - 不完整序列(leading + 部分 continuation 后缺): 合并替换 1 个(消费已有前缀)
      - leading + 非 continuation: 替换 leading 1 个,后续字节重新扫描
      - 孤立 continuation(0x80-0xBF): 逐个替换
    """
    out = bytearray()
    replacement = b"\xef\xbf\xbd"  # U+FFFD
    b = body
    n = len(b)
    i = 0

    def is_cont(x: int) -> bool:
        return 0x80 <= x <= 0xBF

    while i < n:
        c = b[i]
        if c < 0x80:
            out.append(c)
            i += 1
        elif c in (0xC0, 0xC1):
            # overlong 2字节: 逐个字节替换
            out += replacement
            i += 1
        elif 0xC2 <= c <= 0xDF:
            if i + 1 < n and is_cont(b[i + 1]):
                out += b[i:i + 2]
                i += 2
            else:
                out += replacement
                i += 1
        elif 0xE0 <= c <= 0xEF:
            if i + 2 < n and is_cont(b[i + 1]) and is_cont(b[i + 2]):
                if (c == 0xE0 and b[i + 1] < 0xA0) or (c == 0xED and b[i + 1] > 0x9F):
                    if c == 0xE0:
                        # overlong 3字节: 逐个替换
                        out += replacement * 3
                    else:
                        # surrogate: 整个替换 1 个
                        out += replacement
                    i += 3
                else:
                    out += b[i:i + 3]
                    i += 3
            elif i + 1 < n and is_cont(b[i + 1]):
                out += replacement  # 1 cont 后缺: 合并替换
                i += 2
            else:
                out += replacement
                i += 1
        elif 0xF0 <= c <= 0xF4:
            if i + 3 < n and is_cont(b[i + 1]) and is_cont(b[i + 2]) and is_cont(b[i + 3]):
                if (c == 0xF0 and b[i + 1] < 0x90) or (c == 0xF4 and b[i + 1] > 0x8F):
                    # overlong/超范围 4字节: 逐个替换
                    out += replacement * 4
                    i += 4
                else:
                    out += b[i:i + 4]
                    i += 4
            elif i + 2 < n and is_cont(b[i + 1]) and is_cont(b[i + 2]):
                out += replacement  # 2 cont 后缺: 合并替换
                i += 3
            elif i + 1 < n and is_cont(b[i + 1]):
                out += replacement  # 1 cont 后缺: 合并替换
                i += 2
            else:
                out += replacement
                i += 1
        else:
            # 孤立 continuation(0x80-0xBF)/ 非法 leading(F5-FF): 逐个替换
            out += replacement
            i += 1
    return bytes(out)
