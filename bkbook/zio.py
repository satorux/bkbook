# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux
# Copyright (c) 1997-2006 Motoyuki Kasahara

"""EB / EPWING のデータファイルのブロック入出力。展開は透過的に行う。

libeb の ``zio`` 層にあたる部分を純 Python で書いたもの。データファイルは
無圧縮のこともあれば何らかの方式で圧縮されていることもあるが、呼び出し側は
バイト範囲を要求するだけで、展開済みのデータが返る。

EB のデータファイルは **ページ**（ブロックとも呼ぶ）単位で番地づけされる。
1 ページはつねに 2048 バイト、ページ番号は 1 起点で、これは本自身の索引表に
入っている番号と同じ数え方である。

いま対応している形式:

``plain``
    無圧縮。
``ebzip1``
    libeb 独自の形式（``*.ebz``）。ヘッダ、スライスの位置表、そのあとに
    スライスごとに独立して zlib 圧縮されたデータが続く。
"""

from __future__ import annotations

import os
import zlib
from collections import OrderedDict

PAGE_SIZE = 2048

EBZIP_HEADER_SIZE = 22
EBZIP_MAX_LEVEL = 5

#: 「このファイルは ebzip 圧縮」を意味するファイル名の拡張子。
EBZIP_SUFFIX = ".ebz"


class EbError(Exception):
    """bkbook が投げる例外すべての基底クラス。"""


class ZioError(EbError):
    """データファイルが壊れているか、途中で切れているか、未対応の形式。"""


def _be(data: bytes) -> int:
    """任意の幅のビッグエンディアン符号なし整数を読む。"""
    return int.from_bytes(data, "big")


def _normalize_entry(name: str) -> str:
    """実際のファイル名を、照合に使う形に均す。

    ISO 9660 で焼かれた本のファイル名は ``START.;1`` や ``HONMON.EBZ;1``
    のようになっていて、ディスクにコピーしたあと大文字小文字がどうなるかは
    まったく当てにならない。バージョン部分と末尾のドットを落とし、
    小文字に揃える。
    """
    stem = name
    version = stem.rfind(";")
    if version >= 0:
        stem = stem[:version]
    if stem.endswith("."):
        stem = stem[:-1]
    return stem.lower()


def find_file(directory: str, basename: str) -> str | None:
    """本のファイルを探す。大文字小文字、``;1``、``.ebz`` を吸収する。

    無圧縮のファイルがあればそのパスを、なければ ebzip 版を、
    それもなければ ``None`` を返す。
    """
    try:
        entries = os.listdir(directory)
    except OSError:
        return None

    wanted = basename.lower()
    plain = None
    zipped = None
    for entry in entries:
        normalized = _normalize_entry(entry)
        if normalized == wanted:
            plain = plain or os.path.join(directory, entry)
        elif normalized == wanted + EBZIP_SUFFIX:
            zipped = zipped or os.path.join(directory, entry)
    return plain or zipped


def find_directory(parent: str, name: str) -> str | None:
    """catalog に書かれたサブディレクトリ名を、大文字小文字を無視して解決する。"""
    if not name:
        return None
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


class Zio:
    """読み出し可能な EB データファイル。展開は透過的に行われる。"""

    def __init__(self, path: str, cache_slices: int = 64):
        self.path = path
        self._file = open(path, "rb")
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._cache_max = max(1, cache_slices)

        try:
            header = self._file.read(EBZIP_HEADER_SIZE)
            if header[:5] == b"EBZip":
                self._init_ebzip(header)
            else:
                self.code = "plain"
                self.slice_size = PAGE_SIZE
                self.file_size = os.fstat(self._file.fileno()).st_size
                self.index_width = 0
        except Exception:
            self._file.close()
            raise

    def _init_ebzip(self, header: bytes) -> None:
        if len(header) < EBZIP_HEADER_SIZE:
            raise ZioError(f"{self.path}: truncated ebzip header")

        mode = header[5] >> 4
        level = header[5] & 0x0F
        if mode not in (1, 2):
            raise ZioError(f"{self.path}: unsupported ebzip mode {mode}")
        if level > EBZIP_MAX_LEVEL:
            raise ZioError(f"{self.path}: unsupported ebzip level {level}")

        self.code = "ebzip1"
        self.zip_level = level
        self.slice_size = PAGE_SIZE << level
        self.file_size = _be(header[9:14])
        self.adler32 = _be(header[14:18])
        self.mtime = _be(header[18:22])

        size = self.file_size
        if size < 1 << 16:
            self.index_width = 2
        elif size < 1 << 24:
            self.index_width = 3
        elif size < 1 << 32:
            self.index_width = 4
        else:
            self.index_width = 5

    # -- reading ---------------------------------------------------------

    def read(self, location: int, length: int) -> bytes:
        """バイト位置 ``location`` から ``length`` バイト読む。

        展開後のファイル末尾を越えた読み出しは、素の ``read(2)`` と同じく
        短く返る。
        """
        if location < 0:
            raise ValueError("location must not be negative")
        if length <= 0:
            return b""

        end = min(location + length, self.file_size)
        if end <= location:
            return b""

        if self.code == "plain":
            self._file.seek(location)
            return self._file.read(end - location)

        out = bytearray()
        position = location
        while position < end:
            data = self._slice(position // self.slice_size)
            offset = position % self.slice_size
            count = min(self.slice_size - offset, end - position)
            chunk = data[offset : offset + count]
            out += chunk
            position += count
            if len(chunk) < count:
                # スライスが短い。ファイルは実際より多くのデータを名乗っている。
                break
        return bytes(out)

    def read_page(self, page: int, count: int = 1) -> bytes:
        """1 起点のページ番号 ``page`` から ``count`` ページ読む。"""
        if page < 1:
            raise ValueError("page numbers are 1-origin")
        return self.read((page - 1) * PAGE_SIZE, count * PAGE_SIZE)

    def _slice(self, index: int) -> bytes:
        cached = self._cache.get(index)
        if cached is not None:
            self._cache.move_to_end(index)
            return cached

        width = self.index_width
        self._file.seek(EBZIP_HEADER_SIZE + index * width)
        entry = self._file.read(width * 2)
        if len(entry) != width * 2:
            raise ZioError(f"{self.path}: truncated slice index at {index}")

        start = _be(entry[:width])
        nxt = _be(entry[width:])
        size = nxt - start
        if size <= 0 or size > self.slice_size:
            raise ZioError(f"{self.path}: bad slice size {size} at {index}")

        self._file.seek(start)
        raw = self._file.read(size)
        if len(raw) != size:
            raise ZioError(f"{self.path}: truncated slice {index}")

        if size == self.slice_size:
            # 圧縮できなかったスライスはそのまま入っている。
            data = raw
        else:
            data = zlib.decompressobj().decompress(raw, self.slice_size)

        self._cache[index] = data
        if len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)
        return data

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self._cache.clear()
        self._file.close()

    def __enter__(self) -> "Zio":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"<Zio {os.path.basename(self.path)} code={self.code} "
            f"size={self.file_size}>"
        )
