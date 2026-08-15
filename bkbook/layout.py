# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux

"""文字列を端末の桁数に合わせて測り、詰める。

日本語があるとこれは自明ではない。1 文字が 2 桁を占めることもあれば、
結合文字なら 0 桁のこともある。だからここでは幅を文字数から数えず、
必ず測る。
"""

from __future__ import annotations

import unicodedata


def char_width(character: str) -> int:
    """1 文字が占める端末の桁数。"""
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(character) for character in text)


def truncate(text: str, columns: int) -> str:
    """``text`` を ``columns`` 桁に収まるよう切る。全角文字は割らない。"""
    if columns <= 0:
        return ""
    used = 0
    for index, character in enumerate(text):
        width = char_width(character)
        if used + width > columns:
            return text[:index]
        used += width
    return text


def pad(text: str, columns: int) -> str:
    """``text`` をちょうど ``columns`` 桁にする。

    詰め物も数えるのではなく測る必要がある。そうしないと、日本語の辞書名の
    並ぶ列と欧文の並ぶ列が揃わない。
    """
    text = truncate(text, columns)
    return text + " " * (columns - display_width(text))


def wrap(text: str, columns: int) -> list[str]:
    """``columns`` 桁で折り返す。文字数ではなく幅で折る。

    欧文の単語は収まる限り分割しない。日本語には区切りの空白がないので、
    行が埋まったところで折る。
    """
    if columns <= 0:
        return [""]

    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue

        current = ""
        used = 0
        pending = ""  # まだ行に確定していない、空白を含まない一続き。
        pending_width = 0

        def flush_pending():
            nonlocal current, used, pending, pending_width
            current += pending
            used += pending_width
            pending = ""
            pending_width = 0

        for character in paragraph:
            width = char_width(character)
            if character == " ":
                flush_pending()
                if used + width <= columns:
                    current += character
                    used += width
                else:
                    lines.append(current)
                    current, used = "", 0
                continue

            # 全角文字はどこでも折れる。欧文の連なりはまとめておく。
            if width == 2:
                flush_pending()

            if used + pending_width + width > columns:
                if pending and used > 0:
                    lines.append(current)
                    current, used = "", 0
                    if pending_width + width > columns:
                        flush_pending()
                        lines.append(current)
                        current, used = "", 0
                else:
                    flush_pending()
                    lines.append(current)
                    current, used = "", 0

            pending += character
            pending_width += width
            if width == 2:
                flush_pending()

        flush_pending()
        lines.append(current)
    return lines or [""]
