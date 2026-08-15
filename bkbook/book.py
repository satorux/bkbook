# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux
# Copyright (c) 1997-2006 Motoyuki Kasahara

"""本——``catalog`` / ``catalogs`` ファイルと、そこに並ぶサブブック。

**本**（book）はディレクトリツリー 1 つ、たいていは CD-ROM 1 枚にあたる。
その中に **サブブック** が 1 つ以上あり、それぞれが独自の索引と本文を持つ
独立した辞書である。ディスク上の配置は 2 通り:

``EB``（電子ブック）
    直下に ``catalog``、サブブックのデータは ``<dir>/start``。
``EPWING``
    直下に ``catalogs``、サブブックのデータは ``<dir>/data/honmon``、
    フォントは ``<dir>/gaiji/``。
"""

from __future__ import annotations

import os

from .jacode import (
    CHARCODE_ISO8859_1,
    CHARCODE_JISX0208,
    CHARCODE_JISX0208_GB2312,
    CHARCODE_NAMES,
    decode_title,
)
from .subbook import SubBook
from .zio import EbError, Zio, find_file

DISC_EB = "eb"
DISC_EPWING = "epwing"

CATALOG_HEADER_SIZE = 16
EB_CATALOG_ENTRY_SIZE = 40
EPWING_CATALOG_ENTRY_SIZE = 164

EB_TITLE_LENGTH = 30
EPWING_TITLE_LENGTH = 80
DIRECTORY_NAME_LENGTH = 8

MAX_SUBBOOKS = 50
MAX_FONTS = 4


class BookError(EbError):
    """本を開けないか、catalog が壊れている。"""


def _trimmed_name(data: bytes) -> str:
    """固定長 ASCII の名前欄を復号する。NUL か空白で切る。"""
    text = data.decode("ascii", errors="replace")
    for terminator in ("\0", " "):
        index = text.find(terminator)
        if index >= 0:
            text = text[:index]
    return text


def is_book(path: str) -> bool:
    """``path`` に本があるか——つまり catalog があるか。"""
    return os.path.isdir(path) and bool(
        find_file(path, "catalog") or find_file(path, "catalogs")
    )


def find_books(path: str) -> list[str]:
    """``path`` にある本、または ``path`` の直下にある本すべて。

    コレクションをまとめて指定できるようにするためのもの。ディスクをコピーした
    ディレクトリを指せば、1 冊ずつ並べなくても全部が返る。
    """
    if is_book(path):
        return [path]
    if not os.path.isdir(path):
        return []
    return [
        entry_path
        for entry in sorted(os.listdir(path))
        for entry_path in [os.path.join(path, entry)]
        if is_book(entry_path)
    ]


class Book:
    """開かれた EB または EPWING の本。"""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        if not os.path.isdir(self.path):
            raise BookError(f"{path}: not a directory")

        self.character_code = self._load_language()
        self.disc_code, catalog_path = self._find_catalog()
        self.epwing_version = 0
        self.subbooks: list[SubBook] = []
        self._load_catalog(catalog_path)

    # -- catalog ---------------------------------------------------------

    def _find_catalog(self) -> tuple[str, str]:
        catalog = find_file(self.path, "catalog")
        if catalog is not None:
            return DISC_EB, catalog
        catalog = find_file(self.path, "catalogs")
        if catalog is not None:
            return DISC_EPWING, catalog
        raise BookError(f"{self.path}: no catalog or catalogs file")

    def _load_language(self) -> int:
        """``language`` からその本の文字コードを読む。

        このファイルは EB のみ、しかも任意。規格上の既定は JIS X 0208 で、
        libeb もそう仮定している。
        """
        path = find_file(self.path, "language")
        if path is None:
            return CHARCODE_JISX0208
        try:
            with Zio(path) as zio:
                header = zio.read(0, CATALOG_HEADER_SIZE)
        except EbError:
            return CHARCODE_JISX0208
        if len(header) < 2:
            return CHARCODE_JISX0208
        code = int.from_bytes(header[0:2], "big")
        if code not in (
            CHARCODE_ISO8859_1,
            CHARCODE_JISX0208,
            CHARCODE_JISX0208_GB2312,
        ):
            return CHARCODE_JISX0208
        return code

    def _load_catalog(self, catalog_path: str) -> None:
        is_eb = self.disc_code == DISC_EB
        entry_size = EB_CATALOG_ENTRY_SIZE if is_eb else EPWING_CATALOG_ENTRY_SIZE
        title_length = EB_TITLE_LENGTH if is_eb else EPWING_TITLE_LENGTH

        with Zio(catalog_path) as zio:
            header = zio.read(0, CATALOG_HEADER_SIZE)
            if len(header) < CATALOG_HEADER_SIZE:
                raise BookError(f"{catalog_path}: truncated catalog")

            count = int.from_bytes(header[0:2], "big")
            if count == 0:
                raise BookError(f"{catalog_path}: catalog lists no subbooks")
            count = min(count, MAX_SUBBOOKS)

            if not is_eb:
                self.epwing_version = int.from_bytes(header[2:4], "big")

            for index in range(count):
                offset = CATALOG_HEADER_SIZE + index * entry_size
                entry = zio.read(offset, entry_size)
                if len(entry) < entry_size:
                    raise BookError(
                        f"{catalog_path}: truncated entry for subbook {index}"
                    )
                self.subbooks.append(self._parse_entry(index, entry, title_length))

    def _parse_entry(self, index: int, entry: bytes, title_length: int) -> SubBook:
        title = decode_title(entry[2 : 2 + title_length], self.character_code)
        name_at = 2 + title_length
        directory_name = _trimmed_name(
            entry[name_at : name_at + DIRECTORY_NAME_LENGTH]
        )

        if self.disc_code == DISC_EB:
            index_page = 1
            narrow_fonts: list[str] = []
            wide_fonts: list[str] = []
        else:
            index_page = int.from_bytes(
                entry[name_at + DIRECTORY_NAME_LENGTH + 4 :][:2], "big"
            )
            # フォントのファイル名はタイトルから固定の位置にある。全角が
            # +18、半角が +50、それぞれ 8 バイトの枠が 4 つずつ。
            wide_fonts = _font_names(entry, 2 + title_length + 18)
            narrow_fonts = _font_names(entry, 2 + title_length + 50)

        return SubBook(
            book=self,
            code=index,
            title=title,
            directory_name=directory_name,
            index_page=index_page,
            narrow_font_names=narrow_fonts,
            wide_font_names=wide_fonts,
        )

    # -- convenience -----------------------------------------------------

    @property
    def character_code_name(self) -> str:
        return CHARCODE_NAMES.get(self.character_code, "unknown")

    def subbook(self, which: int | str) -> SubBook:
        """サブブックを番号、ディレクトリ名、または完全なタイトルで引く。"""
        if isinstance(which, int):
            return self.subbooks[which]
        lowered = which.lower()
        for subbook in self.subbooks:
            if subbook.directory_name.lower() == lowered or subbook.title == which:
                return subbook
        raise KeyError(which)

    def close(self) -> None:
        for subbook in self.subbooks:
            subbook.close()

    def __enter__(self) -> "Book":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"<Book {self.path!r} {self.disc_code} "
            f"{self.character_code_name} subbooks={len(self.subbooks)}>"
        )


def _font_names(entry: bytes, offset: int) -> list[str]:
    """8 バイトのフォント名の枠を 4 つ読む。空の枠は飛ばす。"""
    names = []
    for index in range(MAX_FONTS):
        at = offset + index * DIRECTORY_NAME_LENGTH
        slot = entry[at : at + DIRECTORY_NAME_LENGTH]
        if not slot or slot[0] == 0 or slot[0] >= 0x80:
            names.append("")
            continue
        names.append(_trimmed_name(slot))
    return names
