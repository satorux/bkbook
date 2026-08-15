# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux

"""日本語文字コードの変換。

EB のディスクは日本語を JIS X 0208 の符号位置そのままで持っている。
1 文字 2 バイト、どちらも 0x21..0x7E、エスケープシーケンスもなければ
最上位ビットも立っていない。これはつまり最上位ビットを落とした EUC-JP
そのものなので、Unicode への変換はビットを立て直して ``euc_jp`` コーデックに
渡すだけで済む。
"""

from __future__ import annotations

#: ディスクの ``language`` ファイルに入っている文字コードの値。
CHARCODE_ISO8859_1 = 1
CHARCODE_JISX0208 = 2
CHARCODE_JISX0208_GB2312 = 3

CHARCODE_NAMES = {
    CHARCODE_ISO8859_1: "iso8859-1",
    CHARCODE_JISX0208: "jisx0208",
    CHARCODE_JISX0208_GB2312: "jisx0208+gb2312",
}


def jisx0208_to_euc(data: bytes) -> bytes:
    """全バイトの最上位ビットを立て、JIS X 0208 を EUC-JP にする。"""
    return bytes(byte | 0x80 for byte in data)


def decode_jisx0208(data: bytes, errors: str = "replace") -> str:
    """JIS X 0208 の生バイト列を ``str`` に復号する。

    末尾の NUL と空白は詰め物であって中身ではない。0x20 が単独で
    JIS X 0208 の 2 バイトの一方になることはない（どちらも 0x21..0x7E）ので、
    削っても文字を割ってしまう心配はない。
    """
    end = data.find(b"\0")
    if end >= 0:
        data = data[:end]
    data = data.rstrip(b" ")
    if not data:
        return ""
    return jisx0208_to_euc(data).decode("euc_jp", errors=errors)


def decode_title(data: bytes, character_code: int) -> str:
    """catalog のタイトル欄を、そのディスクの文字コードに従って復号する。"""
    if character_code == CHARCODE_ISO8859_1:
        end = data.find(b"\0")
        if end >= 0:
            data = data[:end]
        return data.rstrip(b" ").decode("iso8859-1", errors="replace")
    return decode_jisx0208(data)


def decode_jisx0208_char(code: int) -> str:
    """JIS X 0208 の符号位置 1 つ（例: 0x256A）を 1 文字に復号する。"""
    raw = bytes(((code >> 8) & 0xFF, code & 0xFF))
    return jisx0208_to_euc(raw).decode("euc_jp", errors="replace")
