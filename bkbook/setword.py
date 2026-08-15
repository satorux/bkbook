# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux
# Copyright (c) 1997-2006 Motoyuki Kasahara

"""利用者の検索文字列を、索引が実際に持っているバイト列に変える。

変換は 2 段階ある。まず入力を JIS X 0208 の生バイトに符号化しなおす。
1 文字が 2 バイトになり、ASCII は全角に写される——見出し語がそう入って
いるからだ。次に索引ごとの正規化規則を適用する。カタカナをひらがなに畳む、
記号や空白を落とす、長音符を母音に開く、など。索引はこの正規化した形で
整列され、鍵づけられている。

出てくるバイト列は 2 本。``canonicalized`` は完全に正規化した形で、
B 木の走査が比較に使うのはこちら。``word`` はたいていの索引では同じものだが、
かなの索引（ID 0x70 と 0x90）では正規化前の形を残す。グループ内の項目どうしを
区別できるようにするためである。

後方一致の索引は見出し語を 1 つ残らず逆順で持っている。「で始まる」に答える
のと同じ整列済み B 木が「で終わる」にも答えられるようにするためだ。そこを
検索するには問い合わせも同じように逆順にする必要があり、それがここの最後の
処理になる。

libeb の ``setword.c`` と ``word.c`` にあたる。正規化の規則そのものは
あちらから写した（:mod:`bkbook._tables` を参照）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ._tables import (
    ASCII_TO_JISX0208,
    HIRAGANA_ROW,
    JISX0201_TO_JISX0208,
    KANA_FIRST,
    KANA_LAST,
    KATAKANA_ROW,
    LONG_VOWEL,
    VOICED_CONSONANT,
)
from .jacode import CHARCODE_ISO8859_1
from .subbook import STYLE_CONVERT, STYLE_DELETE, Search
from .zio import EbError

WORD_ALPHABET = "alphabet"
WORD_KANA = "kana"
WORD_OTHER = "other"

MAX_WORD_LENGTH = 255

JIS_SPACE = b"\x21\x21"
JIS_LONG_VOWEL = b"\x21\x3c"

#: 0x21 区にある、落とすべき「記号」とみなす点のバイト。
#: 中黒、長音符、波ダッシュなど。
MARK_CELLS = frozenset({0x26, 0x3E, 0x47, 0x5D})


class WordError(EbError):
    """検索語が空か、長すぎるか、表現できない文字を含む。"""


@dataclass
class Query:
    """ある 1 つの索引向けに用意した検索語。"""

    word: bytes
    canonicalized: bytes
    word_code: str
    search: Search


# -- 第 1 段階: JIS X 0208 の生バイトにする ------------------------------


def convert_to_jisx0208(text: str) -> tuple[bytearray, str]:
    """``text`` を JIS X 0208 の生バイトに符号化し、どんな語かを判定する。

    バイト列と、``WORD_ALPHABET`` / ``WORD_KANA`` / ``WORD_OTHER`` のどれかを
    返す。この判定が、その本のアルファベット索引・かな索引・そのまま索引の
    どれを引くかを決める。
    """
    try:
        data = text.encode("euc_jp")
    except UnicodeEncodeError as exc:
        raise WordError(
            f"{text!r}: contains characters outside JIS X 0208"
        ) from exc

    data = _strip_padding(data)

    out = bytearray()
    alphabet = kana = kanji = 0
    position = 0
    while position < len(data):
        if MAX_WORD_LENGTH < len(out) + 2:
            raise WordError(f"{text!r}: search word is too long")

        byte = data[position]
        position += 1
        if byte == 0x09:
            byte = 0x20

        if 0x20 <= byte <= 0x7E:
            code = ASCII_TO_JISX0208[byte - 0x20]
            c1, c2 = code >> 8, code & 0xFF
        elif byte == 0x8E:
            # SS2。このあとに半角カタカナが 1 バイト続く。
            if position >= len(data):
                raise WordError(f"{text!r}: truncated half-width katakana")
            c2 = data[position]
            position += 1
            if not 0xA1 <= c2 <= 0xDF:
                raise WordError(f"{text!r}: bad half-width katakana")
            code = JISX0201_TO_JISX0208[c2 - 0xA0]
            c1, c2 = code >> 8, code & 0xFF
        elif 0xA1 <= byte <= 0xFE:
            if position >= len(data):
                raise WordError(f"{text!r}: truncated multibyte character")
            c2 = data[position]
            position += 1
            if 0xA1 <= c2 <= 0xFE:
                c1, c2 = byte & 0x7F, c2 & 0x7F
            elif 0x20 <= c2 <= 0x7E:
                # ディスク固有の文字。2 バイトをそのまま通す。
                c1 = byte
            else:
                raise WordError(f"{text!r}: bad multibyte character")
        else:
            raise WordError(f"{text!r}: unsupported character")

        out += bytes((c1, c2))
        if c1 == 0x23:
            alphabet += 1
        elif c1 in (HIRAGANA_ROW, KATAKANA_ROW):
            kana += 1
        elif c1 != 0x21:
            kanji += 1

    if not out:
        raise WordError("empty search word")

    if alphabet == 0 and kana != 0 and kanji == 0:
        word_code = WORD_KANA
    elif alphabet != 0 and kana == 0 and kanji == 0:
        word_code = WORD_ALPHABET
    else:
        word_code = WORD_OTHER
    return out, word_code


def _strip_padding(data: bytes) -> bytes:
    """前後の空白を落とす。ASCII の空白と全角空白の両方。"""
    start, end = 0, len(data)
    while start < end:
        if data[start] in (0x20, 0x09):
            start += 1
        elif data[start : start + 2] == b"\xa1\xa1":
            start += 2
        else:
            break
    while start < end:
        if data[end - 1] in (0x20, 0x09):
            end -= 1
        elif data[end - 2 : end] == b"\xa1\xa1":
            end -= 2
        else:
            break
    return data[start:end]


def convert_to_latin(text: str) -> tuple[bytearray, str]:
    """ISO 8859-1 の本向けに ``text`` を符号化する。"""
    try:
        data = text.encode("iso8859-1")
    except UnicodeEncodeError as exc:
        raise WordError(f"{text!r}: contains non Latin-1 characters") from exc
    data = data.strip(b" \t")
    if not data:
        raise WordError("empty search word")
    if MAX_WORD_LENGTH < len(data):
        raise WordError(f"{text!r}: search word is too long")
    return bytearray(data), WORD_ALPHABET


# -- 第 2 段階: 索引ごとの正規化規則を適用する ---------------------------


def fix_word(
    search: Search,
    word: bytearray,
    character_code: int,
    word_code: str,
    backward: bool = False,
) -> Query:
    """``search`` の正規化規則を適用し、最終的な Query を組み立てる。

    ``backward`` を指定すると結果を 1 文字ずつ逆順にする。後方一致の索引が
    鍵にしているのはこの形である。
    """
    canonicalized = bytearray(word)

    if character_code == CHARCODE_ISO8859_1:
        if search.space == STYLE_DELETE:
            canonicalized = _delete_bytes_latin(canonicalized, b" ")
        if search.lower == STYLE_CONVERT:
            canonicalized = _upper_latin(canonicalized)
    else:
        if search.space == STYLE_DELETE:
            canonicalized = _delete_pairs(canonicalized, {JIS_SPACE})
        if search.katakana == STYLE_CONVERT:
            canonicalized = _fold_kana(canonicalized, KATAKANA_ROW, HIRAGANA_ROW)
        elif search.katakana == STYLE_DELETE:
            # 2 は「逆向きの変換」でもある。ひらがな → カタカナ。
            canonicalized = _fold_kana(canonicalized, HIRAGANA_ROW, KATAKANA_ROW)

        if search.lower == STYLE_CONVERT:
            canonicalized = _upper_jis(canonicalized)
        if search.mark == STYLE_DELETE:
            canonicalized = _delete_marks(canonicalized)
        if search.long_vowel == STYLE_CONVERT:
            canonicalized = _convert_long_vowels(canonicalized)
        elif search.long_vowel == STYLE_DELETE:
            canonicalized = _delete_pairs(canonicalized, {JIS_LONG_VOWEL})
        if search.double_consonant == STYLE_CONVERT:
            canonicalized = _map_kana_cells(canonicalized, {0x43: 0x44})
        if search.contracted_sound == STYLE_CONVERT:
            canonicalized = _map_kana_cells(
                canonicalized,
                {0x63: 0x64, 0x65: 0x66, 0x67: 0x68, 0x6E: 0x6F,
                 0x75: 0x2B, 0x76: 0x31},
            )
        if search.small_vowel == STYLE_CONVERT:
            canonicalized = _map_kana_cells(
                canonicalized,
                {0x21: 0x22, 0x23: 0x24, 0x25: 0x26, 0x27: 0x28, 0x29: 0x2A},
            )
        if search.voiced_consonant == STYLE_CONVERT:
            canonicalized = _table_kana_cells(canonicalized, VOICED_CONSONANT)
        if search.p_sound == STYLE_CONVERT:
            canonicalized = _map_kana_cells(
                canonicalized,
                {0x51: 0x4F, 0x54: 0x52, 0x57: 0x55, 0x5A: 0x58, 0x5D: 0x5B},
            )

    # かなの索引では、グループの構成要素は正規化前の語と比べる。
    if search.index_id in (0x70, 0x90):
        fixed = bytes(word)
    else:
        fixed = bytes(canonicalized)
    canonical = bytes(canonicalized)

    if backward:
        reverse = (
            _reverse_latin
            if character_code == CHARCODE_ISO8859_1
            else _reverse_jisx0208
        )
        fixed = reverse(fixed)
        canonical = reverse(canonical)

    return Query(
        word=fixed,
        canonicalized=canonical,
        word_code=word_code,
        search=search,
    )


def _reverse_latin(data: bytes) -> bytes:
    return data[::-1]


def _reverse_jisx0208(data: bytes) -> bytes:
    """2 バイト——1 文字——単位で逆順にする。"""
    return b"".join(bytes(pair) for pair in reversed(list(_pairs(data))))


def _pairs(data: bytes):
    """バイト列を 2 バイトずつ取り出す。"""
    for i in range(0, len(data) - 1, 2):
        yield data[i], data[i + 1]


def _delete_pairs(data: bytearray, pairs: set[bytes]) -> bytearray:
    out = bytearray()
    for c1, c2 in _pairs(data):
        if bytes((c1, c2)) not in pairs:
            out += bytes((c1, c2))
    return out


def _delete_marks(data: bytearray) -> bytearray:
    out = bytearray()
    for c1, c2 in _pairs(data):
        if c1 != 0x21 or c2 not in MARK_CELLS:
            out += bytes((c1, c2))
    return out


def _fold_kana(data: bytearray, from_row: int, to_row: int) -> bytearray:
    out = bytearray()
    for c1, c2 in _pairs(data):
        if c1 == from_row and KANA_FIRST <= c2 <= KANA_LAST:
            c1 = to_row
        out += bytes((c1, c2))
    return out


def _upper_jis(data: bytearray) -> bytearray:
    out = bytearray()
    for c1, c2 in _pairs(data):
        if c1 == 0x23 and 0x61 <= c2 <= 0x7A:
            c2 -= 0x20
        out += bytes((c1, c2))
    return out


def _map_kana_cells(data: bytearray, mapping: dict[int, int]) -> bytearray:
    """かなの点のバイトを ``mapping`` に従って書き換える。区は触らない。"""
    out = bytearray()
    for c1, c2 in _pairs(data):
        if c1 in (HIRAGANA_ROW, KATAKANA_ROW):
            c2 = mapping.get(c2, c2)
        out += bytes((c1, c2))
    return out


def _table_kana_cells(data: bytearray, table) -> bytearray:
    out = bytearray()
    for c1, c2 in _pairs(data):
        if c1 in (HIRAGANA_ROW, KATAKANA_ROW) and KANA_FIRST <= c2 <= KANA_LAST:
            c2 = table[c2 - KANA_FIRST]
        out += bytes((c1, c2))
    return out


def _convert_long_vowels(data: bytearray) -> bytearray:
    """ー を、直前のかなの母音に置き換える。"""
    out = bytearray()
    previous = (0, 0)
    for c1, c2 in _pairs(data):
        if (c1, c2) == (0x21, 0x3C):
            p1, p2 = previous
            if p1 in (HIRAGANA_ROW, KATAKANA_ROW) and KANA_FIRST <= p2 <= KANA_LAST:
                c1, c2 = p1, LONG_VOWEL[p2 - KANA_FIRST]
        previous = (c1, c2)
        out += bytes((c1, c2))
    return out


def _delete_bytes_latin(data: bytearray, drop: bytes) -> bytearray:
    return bytearray(b for b in data if b not in drop)


def _upper_latin(data: bytearray) -> bytearray:
    out = bytearray()
    for byte in data:
        if 0x61 <= byte <= 0x7A or 0xE0 <= byte <= 0xF6 or 0xF8 <= byte <= 0xFE:
            byte -= 0x20
        out.append(byte)
    return out
