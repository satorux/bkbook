# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux
# Copyright (c) 1997-2006 Motoyuki Kasahara

"""appendix ファイル——辞書に添えて配布される外字表。

**appendix** はディスク自身が持てないものを補う。ディスク固有の文字それぞれに
対する置き換えテキストと、項目の終わりを示すエスケープシーケンスである。
Motoyuki Kasahara 氏——libeb の作者——が公開したものは日本の CD-ROM 辞書の
ほとんどを覆っていて、ある外字符号が何を表すかの答え合わせに使える。

このモジュールは人が読める ``*.app`` のソース形式を読む:

    character-code  jisx0208
    stop-code       0x1f09 0x0001

    begin narrow
            range-start     0xa121
            range-end       0xa369

            0xa121  /
            0xa122  ~
    end

    begin wide
            ...
    end

置き換えは意図して素の ASCII になっている。当時の端末に出せるのはそれだけ
だったからだ。置き換えが空なら、その文字は何も出力しない。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .zio import EbError

#: 試す文字コード。順に試す。Motoyuki Kasahara 氏が公開したファイルは
#: ISO-2022-JP。
ENCODINGS = ("iso2022_jp", "euc_jp", "utf-8")


class AppendixError(EbError):
    """appendix ファイルが壊れているか、どれを使うべきかが定まらない。"""


class AppendixNotFoundError(AppendixError):
    """このサブブック用の appendix がここにはない。

    そもそも appendix が公開されなかった辞書のほうが多いので、共通の
    appendix ディレクトリを持つコレクションを開けば大半の本でこれが起きる。
    まとめて何冊も読む側は「appendix なし」として黙って進むのが正しい。
    1 冊を名指しした側は報告すべき。
    """


@dataclass
class Appendix:
    """サブブック 1 つ分の appendix。"""

    character_code: str = "jisx0208"
    stop_code: tuple[int, int] | None = None
    narrow: dict[int, str] = field(default_factory=dict)
    wide: dict[int, str] = field(default_factory=dict)
    path: str = ""

    def as_gaiji_table(self) -> dict[tuple[bool, int], str]:
        """レンダラが受け取る ``{(narrow, code): text}`` 形式に変換する。"""
        table: dict[tuple[bool, int], str] = {}
        for code, text in self.narrow.items():
            table[(True, code)] = text
        for code, text in self.wide.items():
            table[(False, code)] = text
        return table

    def __repr__(self) -> str:
        return (
            f"<Appendix {os.path.basename(self.path) or '?'} "
            f"narrow={len(self.narrow)} wide={len(self.wide)} "
            f"stop_code={self.stop_code}>"
        )


def parse(text: str, path: str = "") -> Appendix:
    """``*.app`` ファイルの中身を解析する。"""
    appendix = Appendix(path=path)
    block: str | None = None

    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.split("#", 1)[0] if raw_line.lstrip().startswith("#") else raw_line
        if not line.strip():
            continue

        stripped = line.strip()
        parts = stripped.split(None, 1)
        keyword = parts[0]
        value = parts[1] if len(parts) > 1 else ""

        if keyword == "begin":
            if value not in ("narrow", "wide"):
                raise AppendixError(f"{path}:{number}: unknown block {value!r}")
            block = value
        elif keyword == "end":
            block = None
        elif keyword == "character-code":
            appendix.character_code = value
        elif keyword == "stop-code":
            appendix.stop_code = _parse_stop_code(value, path, number)
        elif keyword in ("range-start", "range-end"):
            pass  # コンパイラのための宣言。符号そのものを見れば足りる。
        elif keyword.startswith("0x"):
            if block is None:
                raise AppendixError(f"{path}:{number}: character outside a block")
            try:
                code = int(keyword, 16)
            except ValueError:
                raise AppendixError(f"{path}:{number}: bad code {keyword!r}") from None
            target = appendix.narrow if block == "narrow" else appendix.wide
            target[code] = value
        else:
            raise AppendixError(f"{path}:{number}: unknown directive {keyword!r}")

    return appendix


def _parse_stop_code(value: str, path: str, number: int) -> tuple[int, int]:
    parts = value.split()
    if len(parts) != 2:
        raise AppendixError(f"{path}:{number}: stop-code needs two values")
    try:
        return int(parts[0], 0), int(parts[1], 0)
    except ValueError:
        raise AppendixError(f"{path}:{number}: bad stop-code {value!r}") from None


def load(path: str) -> Appendix:
    """``*.app`` ファイルを、文字コードを判定しながら読む。"""
    with open(path, "rb") as handle:
        raw = handle.read()

    for encoding in ENCODINGS:
        try:
            return parse(raw.decode(encoding), path)
        except UnicodeDecodeError:
            continue
    raise AppendixError(f"{path}: could not decode as any of {', '.join(ENCODINGS)}")


def candidates(where: str, subbook) -> list[str]:
    """``subbook`` のものでありうる ``*.app`` を列挙する。

    ``where`` はファイルそのものでも、その本の appendix ディレクトリでも、
    何冊分もの appendix を収めたディレクトリでもよい。

    appendix ディレクトリの中では、ファイル名は **サブブック** の
    ディレクトリ名でつく——``PLUS`` にあるサブブックなら ``plus.app``。
    この名前は無関係な辞書どうしで重複するので、コレクションの中では
    appendix ディレクトリのほうを **本** のディレクトリ名で探す。
    ``eb/genius`` は ``eb/appendix/genius`` と、``eb/genius2`` は
    ``eb/appendix/genius2`` と対になる。サブブック名だけでは決して
    区別できない対応である。

    最後の手段として全体を走査すると複数見つかることもある。その場合は
    全部返し、選ぶのは呼び出し側に任せる。
    """
    if os.path.isfile(where):
        return [where]
    if not os.path.isdir(where):
        return []

    wanted = f"{subbook.directory_name.lower()}.app"

    here = _match_file(where, wanted)
    if here:
        return [here]

    book_directory = os.path.basename(os.path.normpath(subbook.book.path))
    paired = _match_directory(where, book_directory)
    if paired:
        match = _match_file(paired, wanted)
        if match:
            return [match]

    found = []
    for entry in sorted(os.listdir(where)):
        nested = os.path.join(where, entry)
        if os.path.isdir(nested):
            match = _match_file(nested, wanted)
            if match:
                found.append(match)
    return found


def find(where: str, subbook) -> str | None:
    """サブブックの ``*.app`` を 1 つ特定する。決まらなければ ``None``。"""
    found = candidates(where, subbook)
    return found[0] if len(found) == 1 else None


def _match_directory(parent: str, name: str) -> str | None:
    try:
        entries = os.listdir(parent)
    except OSError:
        return None
    wanted = name.lower()
    for entry in entries:
        if entry.lower() == wanted:
            path = os.path.join(parent, entry)
            if os.path.isdir(path):
                return path
    return None


def _match_file(directory: str, wanted: str) -> str | None:
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    for entry in entries:
        if entry.lower() == wanted:
            path = os.path.join(directory, entry)
            if os.path.isfile(path):
                return path
    return None


def for_subbook(subbook, where: str) -> Appendix:
    """``where`` から ``subbook`` の appendix を読み込む。

    サブブックのディレクトリ名を共有する appendix が複数あるときは、推測を
    拒む。取り違えれば、黙って誤ったテキストが出てくることになる。
    """
    found = candidates(where, subbook)
    if not found:
        raise AppendixNotFoundError(
            f"{where}: no {subbook.directory_name.lower()}.app "
            f"for 「{subbook.title}」"
        )
    if len(found) > 1:
        listed = "\n  ".join(found)
        raise AppendixError(
            f"{where}: several appendices are named after the "
            f"{subbook.directory_name!r} directory; pass the right one "
            f"directly:\n  {listed}"
        )
    return load(found[0])
