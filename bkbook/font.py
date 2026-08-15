# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux
# Copyright (c) 1997-2006 Motoyuki Kasahara

"""JIS X 0208 にない文字（**外字**）のための、ディスク内蔵のビットマップ
フォント。

標準の文字コードにない文字——発音記号、合字、ロゴ——が必要になったディスクは、
その字のビットマップを持ち、ディスク固有の符号で参照する。Unicode の符号位置は
どこにも出てこないので、その字をテキストとして出すには、呼び出し側から対応表を
もらうか、ビットマップそのものを見るかしかない。

字形は 16 バイトのヘッダのあとに平らに並ぶ。この配列は 1024 バイトの塊単位で
番地づけされるため、1 つの字形が塊の境目をまたぐことはない。塊の末尾に余った
分は詰め物である。

libeb の ``narwfont.c`` と ``widefont.c`` にあたる。
"""

from __future__ import annotations

from dataclasses import dataclass

from .jacode import CHARCODE_ISO8859_1
from .subbook import FONT_HEIGHTS, Font, SubBook
from .zio import PAGE_SIZE, EbError

#: 字形 1 つあたりのバイト数。フォントの高さ別、半角・全角別。
NARROW_SIZES = {16: 16, 24: 48, 30: 60, 48: 144}
WIDE_SIZES = {16: 32, 24: 72, 30: 120, 48: 288}

NARROW_WIDTHS = {16: 8, 24: 16, 30: 16, 48: 24}
WIDE_WIDTHS = {16: 16, 24: 24, 30: 32, 48: 48}

_CHUNK = 1024


class FontError(EbError):
    """求められたフォント、または文字のビットマップがない。"""


@dataclass
class Bitmap:
    """字形 1 つ。1 画素 1 ビット、行優先、最上位ビットが左端。"""

    code: int
    width: int
    height: int
    data: bytes

    @property
    def row_bytes(self) -> int:
        return (self.width + 7) // 8

    def pixel(self, x: int, y: int) -> bool:
        byte = self.data[y * self.row_bytes + (x >> 3)]
        return bool(byte & (0x80 >> (x & 7)))

    def to_text(self, on: str = "█", off: str = "·") -> str:
        """1 画素 1 文字でテキストに描く。"""
        return "\n".join(
            "".join(on if self.pixel(x, y) else off for x in range(self.width))
            for y in range(self.height)
        )

    def to_pbm(self) -> bytes:
        """バイナリ PBM (P4) 画像として書き出す。"""
        header = f"P4\n{self.width} {self.height}\n".encode("ascii")
        return header + self.data


class FontSet:
    """あるサブブックの半角または全角フォント、1 サイズ分。"""

    def __init__(self, subbook: SubBook, font: Font, narrow: bool):
        self.subbook = subbook
        self.font = font
        self.narrow = narrow
        self.height = FONT_HEIGHTS[font.font_code]
        self.glyph_size = (
            NARROW_SIZES[self.height] if narrow else WIDE_SIZES[self.height]
        )
        self.width = (
            NARROW_WIDTHS[self.height] if narrow else WIDE_WIDTHS[self.height]
        )

        if font.page == 0:
            raise FontError(
                f"{subbook.title!r}: no built-in "
                f"{'narrow' if narrow else 'wide'} font"
            )

        self.zio = subbook.font_zio(font)
        header = self.zio.read_page(font.page)
        if len(header) < 16:
            raise FontError(f"{subbook.title!r}: truncated font header")

        self.count = int.from_bytes(header[12:14], "big")
        self.start = int.from_bytes(header[10:12], "big")
        if self.count == 0:
            raise FontError(f"{subbook.title!r}: font holds no characters")

        latin = subbook.book.character_code == CHARCODE_ISO8859_1
        row_length = 0xFE if latin else 0x5E
        self.end = (
            self.start + ((self.count // row_length) << 8)
            + (self.count % row_length) - 1
        )
        # 符号は行と行の隙間を飛ばすので、行からあふれた分は繰り上がる。
        if latin:
            if 0xFE < (self.end & 0xFF):
                self.end += 3
        elif 0x7E < (self.end & 0xFF):
            self.end += 0xA3

        self._row_length = row_length
        self._latin = latin
        self._cell_first = 0x01 if latin else 0x21

    def __contains__(self, code: int) -> bool:
        cell = code & 0xFF
        return (
            self.start <= code <= self.end
            and self._cell_first <= cell <= (0xFE if self._latin else 0x7E)
        )

    def bitmap(self, code: int) -> Bitmap:
        """ディスク固有の文字符号に対応する字形を返す。"""
        if code not in self:
            raise FontError(
                f"0x{code:04x} is outside this font "
                f"(0x{self.start:04x}..0x{self.end:04x})"
            )

        index = ((code >> 8) - (self.start >> 8)) * self._row_length + (
            (code & 0xFF) - (self.start & 0xFF)
        )
        per_chunk = _CHUNK // self.glyph_size
        offset = (index // per_chunk) * _CHUNK + (index % per_chunk) * self.glyph_size

        # 字形はヘッダのページの次のページから始まる。
        data = self.zio.read(self.font.page * PAGE_SIZE + offset, self.glyph_size)
        if len(data) != self.glyph_size:
            raise FontError(f"0x{code:04x}: truncated glyph")
        return Bitmap(code=code, width=self.width, height=self.height, data=data)

    def codes(self):
        """このフォントが定義している文字符号をすべて列挙する。"""
        code = self.start
        last_cell = 0xFE if self._latin else 0x7E
        for _ in range(self.count):
            yield code
            if (code & 0xFF) >= last_cell:
                code = (code & ~0xFF) + 0x100 + self._cell_first
            else:
                code += 1

    def __repr__(self) -> str:
        kind = "narrow" if self.narrow else "wide"
        return (
            f"<FontSet {kind} {self.width}x{self.height} "
            f"0x{self.start:04x}..0x{self.end:04x} count={self.count}>"
        )


def font_set(subbook: SubBook, narrow: bool, height: int = 16) -> FontSet:
    """サブブックの半角／全角フォントを、指定の高さで開く。"""
    subbook.load()
    fonts = subbook.narrow_fonts if narrow else subbook.wide_fonts
    for font_code, font in sorted(fonts.items()):
        if FONT_HEIGHTS[font_code] == height:
            return FontSet(subbook, font, narrow)
    raise FontError(
        f"{subbook.title!r}: no {'narrow' if narrow else 'wide'} "
        f"{height}px font (have {sorted(FONT_HEIGHTS[c] for c in fonts)})"
    )
