"""小分组前向纠错（协议 §8）。

每组最多 32 个定长数据分片，发送两个校验分片：

* P0 = d0 ⊕ d1 ⊕ ...
* P1 = 1·d0 ⊕ 2·d1 ⊕ ...（系数在 GF(256) 中运算）

缺 1 个分片时由 P0 恢复，缺 2 个分片时联立 P0/P1 恢复。数据分片不足
CHUNK_SIZE 的末帧按零补齐，恢复后由元数据 compressedSize 再截断。
"""

from __future__ import annotations


def gf_mul(a: int, b: int) -> int:
    """GF(256) 乘法，生成多项式 x^8+x^4+x^3+x^2+1（0x11D）。"""
    out = 0
    while b:
        if b & 1:
            out ^= a
        b >>= 1
        a = (((a << 1) ^ 0x11D) if a & 0x80 else (a << 1)) & 0xFF
    return out & 0xFF


def gf_pow(a: int, n: int) -> int:
    out = 1
    while n:
        if n & 1:
            out = gf_mul(out, a)
        a = gf_mul(a, a)
        n >>= 1
    return out


def gf_inv(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("GF(256) 中 0 不可求逆")
    return gf_pow(a, 254)


def recover_one(parity0: bytes, known: list[bytes | None]) -> bytes:
    """P0 + 已知数据恢复一个缺失分片。"""
    out = bytearray(parity0)
    for part in known:
        if part is not None:
            for i, value in enumerate(part):
                out[i] ^= value
    return bytes(out)


def recover_two(parity0: bytes, parity1: bytes,
                known: list[bytes | None], missing: tuple[int, int]) -> tuple[bytes, bytes]:
    """P0/P1 联立恢复两个缺失分片。"""
    a_idx, b_idx = missing
    s0 = bytearray(parity0)
    s1 = bytearray(parity1)
    for idx, part in enumerate(known):
        if part is None:
            continue
        coeff = idx + 1
        for i, value in enumerate(part):
            s0[i] ^= value
            s1[i] ^= gf_mul(coeff, value)

    ca, cb = a_idx + 1, b_idx + 1
    inv = gf_inv(ca ^ cb)
    a = bytearray(len(s0))
    b = bytearray(len(s0))
    for i in range(len(s0)):
        # ca*A + cb*B = S1；A + B = S0
        a[i] = gf_mul(s1[i] ^ gf_mul(cb, s0[i]), inv)
        b[i] = s0[i] ^ a[i]
    return bytes(a), bytes(b)
