# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux
# Copyright (c) 1997-2006 Motoyuki Kasahara

"""検索語とインデックス項目を比べる関数群。

どの関数も、正規化済みの検索語 *word* をインデックスの *pattern*
（索引項目に入っているキー）と比べ、B 木の走査を導く値を返す:

* ``0``  — 一致。ヒットとして記録する。
* ``> 0`` — 語のほうが後ろに並ぶ。前へ走査を続ける。
* ``< 0`` — 語のほうが前に並ぶ。検索はここで終わり。

引数はどちらも JIS X 0208（または Latin-1）の生バイト列。libeb では語が
NUL 終端、パターンは長さを明示していたが、ここでは ``bytes`` の終端が
その両方を兼ねる。「終端の先」を読むと 0 が返るのは、C 版が NUL 終端から
読んでいた 0 と同じ意味になる。
"""

from __future__ import annotations

from ._tables import HIRAGANA_ROW, KATAKANA_ROW


def _at(data: bytes, index: int) -> int:
    """``index`` のバイト。終端より先なら 0——C の NUL 終端に相当する。"""
    return data[index] if index < len(data) else 0


def match_word(word: bytes, pattern: bytes) -> int:
    """前方一致。語で始まる項目をヒットとする。"""
    length = len(pattern)
    i = 0
    while True:
        if length <= i:
            return _at(word, i)
        if i >= len(word):
            return 0
        if word[i] != pattern[i]:
            return word[i] - pattern[i]
        i += 1


def pre_match_word(word: bytes, pattern: bytes) -> int:
    """中間ノードで子ページを選ぶときに使う前方一致の比較。"""
    length = len(pattern)
    i = 0
    while True:
        if length <= i or i >= len(word):
            return 0
        if word[i] != pattern[i]:
            return word[i] - pattern[i]
        i += 1


def exact_match_word_jis(word: bytes, pattern: bytes) -> int:
    """完全一致。項目末尾の NUL の詰め物は無視する。"""
    length = len(pattern)
    i = 0
    while True:
        if length <= i:
            return _at(word, i)
        if i >= len(word):
            while i < length and pattern[i] == 0:
                i += 1
            return i - length
        if word[i] != pattern[i]:
            return word[i] - pattern[i]
        i += 1


def exact_pre_match_word_jis(word: bytes, pattern: bytes) -> int:
    """完全一致検索の、中間ノード用の比較。"""
    length = len(pattern)
    i = 0
    while True:
        if length <= i:
            return 0
        if i >= len(word):
            while i < length and pattern[i] == 0:
                i += 1
            return i - length
        if word[i] != pattern[i]:
            return word[i] - pattern[i]
        i += 1


def exact_match_word_latin(word: bytes, pattern: bytes) -> int:
    """ISO 8859-1 のディスク用の完全一致。末尾の空白は詰め物。"""
    length = len(pattern)
    i = 0
    while True:
        if length <= i:
            return _at(word, i)
        if i >= len(word):
            while i < length and pattern[i] == 0x20:
                i += 1
            return i - length
        if word[i] != pattern[i]:
            return word[i] - pattern[i]
        i += 1


def exact_pre_match_word_latin(word: bytes, pattern: bytes) -> int:
    """Latin-1 のディスクでの完全一致検索の、中間ノード用の比較。"""
    length = len(pattern)
    i = 0
    while True:
        if length <= i:
            return 0
        if i >= len(word):
            while i < length and pattern[i] == 0x20:
                i += 1
            return i - length
        if word[i] != pattern[i]:
            return word[i] - pattern[i]
        i += 1


def _is_kana_row(byte: int) -> bool:
    return byte == HIRAGANA_ROW or byte == KATAKANA_ROW


def _kana_compare(
    word: bytes,
    pattern: bytes,
    *,
    exact: bool,
    fold_rows: bool,
) -> int:
    """かなを見る 4 つの比較関数の共通部分。

    どちらの型も、点のバイトが同じひらがなとカタカナを等しいものとして
    扱う。つまり ``カ`` は ``か`` に一致する。違うのは、点が食い違った
    ときに報告する順序のほう。``fold_rows`` なら点のバイトだけで比べ、
    ``か == カ < が == ガ`` になる。そうでなければ 2 バイト全体で決まり、
    ``か < が < カ < ガ`` になる。
    """
    length = len(pattern)
    i = 0
    while True:
        if length <= i:
            return _at(word, i)
        if i >= len(word):
            return -_at(pattern, i) if exact else 0
        if length <= i + 1 or i + 1 >= len(word):
            return word[i] - _at(pattern, i)

        wc0, wc1 = word[i], word[i + 1]
        pc0, pc1 = pattern[i], pattern[i + 1]

        if _is_kana_row(wc0) and _is_kana_row(pc0):
            if wc1 != pc1:
                if fold_rows:
                    return wc1 - pc1
                return ((wc0 << 8) + wc1) - ((pc0 << 8) + pc1)
        elif wc0 != pc0 or wc1 != pc1:
            return ((wc0 << 8) + wc1) - ((pc0 << 8) + pc1)
        i += 2


def match_word_kana_group(word: bytes, pattern: bytes) -> int:
    return _kana_compare(word, pattern, exact=False, fold_rows=False)


def match_word_kana_single(word: bytes, pattern: bytes) -> int:
    return _kana_compare(word, pattern, exact=False, fold_rows=True)


def exact_match_word_kana_group(word: bytes, pattern: bytes) -> int:
    return _kana_compare(word, pattern, exact=True, fold_rows=False)


def exact_match_word_kana_single(word: bytes, pattern: bytes) -> int:
    return _kana_compare(word, pattern, exact=True, fold_rows=True)
