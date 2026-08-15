# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux
# Copyright (c) 1997-2006 Motoyuki Kasahara

"""サブブック——データファイルの先頭にある索引表。

``start`` / ``honmon`` の第 1 ページは、そのサブブック全体の目次になっている。
16 バイトの項目 1 つが 1 つの **索引** を指す。内容はページの範囲と、検索
できる索引ならば、検索語を引く前にどう正規化するかの規則である。項目の ID が
その範囲に何が入っているかを示す。本文そのもの、著作権表示、ある種類の
検索索引、あるいはフォント。

libeb の ``subbook.c`` にあたる。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .zio import EbError, Zio, find_directory, find_file

INDEX_TABLE_ENTRY_SIZE = 16

# 索引を引く前に、語をどう変形するか。
STYLE_CONVERT = 0
STYLE_ASIS = 1
STYLE_DELETE = 2

# フォントの種類。EB の索引 ID 0xf1..0xf8 が参照する順に並べてある。
FONT_16 = 0
FONT_24 = 1
FONT_30 = 2
FONT_48 = 3

FONT_HEIGHTS = {FONT_16: 16, FONT_24: 24, FONT_30: 30, FONT_48: 48}

#: 検索方式を表す索引 ID と、それを保持する属性名の対応。
SEARCH_INDEX_IDS = {
    0x00: "text",
    0x01: "menu",
    0x02: "copyright",
    0x10: "image_menu",
    0x70: "endword_kana",
    0x71: "endword_asis",
    0x72: "endword_alphabet",
    0x80: "keyword",
    0x81: "cross",
    0x90: "word_kana",
    0x91: "word_asis",
    0x92: "word_alphabet",
    0xD8: "sound",
}

#: multi 検索の表が、欄の語索引として使う索引 ID。ここに載る他のもの
#: （手元のディスクでは 0x01、0x05、0x0d）は本文のページである——
#: 入力できる値のメニューか、見出しを読み出す一覧のどちらか。
MULTI_INDEX_IDS = frozenset({0x71, 0x72, 0x91, 0x92, 0xA1, 0xA2, 0xB1, 0xB2})

#: そのうち、かなで鍵づけられているもの。0x70/0x90 の索引と同じように畳む。
MULTI_KANA_INDEX_IDS = frozenset({0xA1, 0xB1})

#: 内蔵フォントの開始ページを示す索引 ID。EB のみ。
FONT_INDEX_IDS = {
    0xF1: ("wide", FONT_16),
    0xF2: ("narrow", FONT_16),
    0xF3: ("wide", FONT_24),
    0xF4: ("narrow", FONT_24),
    0xF5: ("wide", FONT_30),
    0xF6: ("narrow", FONT_30),
    0xF7: ("wide", FONT_48),
    0xF8: ("narrow", FONT_48),
}


class _Unset:
    """遅延して求める値の印。``None`` を「答えなし」の意味に使えるようにする。"""


_UNSET = _Unset()


class SubBookError(EbError):
    """サブブックのデータファイルがないか、索引表が壊れている。"""


@dataclass
class Search:
    """索引 1 つ。どこにあるかと、そこ向けに語をどう正規化するか。"""

    index_id: int
    start_page: int
    end_page: int

    katakana: int = STYLE_ASIS
    lower: int = STYLE_CONVERT
    mark: int = STYLE_ASIS
    long_vowel: int = STYLE_ASIS
    double_consonant: int = STYLE_ASIS
    contracted_sound: int = STYLE_ASIS
    small_vowel: int = STYLE_ASIS
    voiced_consonant: int = STYLE_ASIS
    p_sound: int = STYLE_ASIS
    space: int = STYLE_DELETE

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1


#: 欄に入力できる値を候補の木として並べた補助ページ。
MULTI_MENU_INDEX_ID = 0x01


@dataclass
class MultiEntry:
    """multi 検索の欄 1 つ。ラベルと、それに答える索引。"""

    label: str
    index: Search | None = None
    candidates: list[Search] = field(default_factory=list)

    @property
    def menu(self) -> Search | None:
        """入力できる値の木。本が用意している場合のみ。

        ことわざ の 事項 欄はまず 16 の見出しに分かれ、その各々がさらに
        10 ほどに分かれる。その木の葉が、この欄に実際に指定できる語である
        ——人生、一生。欄が挙げている他のページ（0x05、0x0d）はメニューでは
        なく、ヒットの見出しを読み出す一続きの並びである。
        """
        for search in self.candidates:
            if search.index_id == MULTI_MENU_INDEX_ID:
                return search
        return None


@dataclass
class MultiSearch:
    """語 1 つではなく、欄を 1 つずつ埋めて行う検索。

    語索引が「この見出し語に何が収められているか」に答えるのに対し、
    multi 検索は複数の問いを同時に立てる——ことわざ は 事項 と 作者 を、
    日本大百科 は 索引語 を求める——そしてそのすべてを満たす項目を返す。
    欄ごとに専用の索引があり、ヒットが指す項目の上で AND を取る。
    """

    search: Search
    entries: list[MultiEntry] = field(default_factory=list)

    @property
    def label(self) -> str:
        return " / ".join(entry.label for entry in self.entries if entry.label)


@dataclass
class Font:
    """内蔵フォント。ビットマップの開始位置と大きさ。"""

    font_code: int
    page: int = 0
    file_name: str = ""

    @property
    def height(self) -> int:
        return FONT_HEIGHTS[self.font_code]


class SubBook:
    """本の中の辞書 1 つ。"""

    def __init__(
        self,
        book,
        code: int,
        title: str,
        directory_name: str,
        index_page: int,
        narrow_font_names: list[str] | None = None,
        wide_font_names: list[str] | None = None,
    ):
        self.book = book
        self.code = code
        self.title = title
        self.directory_name = directory_name
        self.index_page = index_page

        self.searches: dict[str, Search] = {}
        self.multi_searches: list[Search] = []
        self.narrow_fonts: dict[int, Font] = {}
        self.wide_fonts: dict[int, Font] = {}
        self.search_title_page = 0

        self._narrow_font_names = narrow_font_names or []
        self._wide_font_names = wide_font_names or []
        self._zio: Zio | None = None
        self._font_zios: dict[str, Zio] = {}
        self._loaded = False
        self._multis: list[MultiSearch] | None = None
        self._stop_code: tuple[int, int] | None | _Unset = _UNSET

    # -- データファイル ----------------------------------------------------

    @property
    def directory_path(self) -> str:
        path = find_directory(self.book.path, self.directory_name)
        if path is None:
            raise SubBookError(
                f"{self.book.path}: no directory {self.directory_name!r} "
                f"for subbook {self.title!r}"
            )
        return path

    def _find_text_file(self) -> str:
        from .book import DISC_EB

        directory = self.directory_path
        if self.book.disc_code == DISC_EB:
            path = find_file(directory, "start")
            if path is None:
                raise SubBookError(f"{directory}: no start file")
            return path

        data_directory = find_directory(directory, "data") or directory
        for name in ("honmon", "honmon2"):
            path = find_file(data_directory, name)
            if path is not None:
                return path
        raise SubBookError(f"{data_directory}: no honmon file")

    @property
    def zio(self) -> Zio:
        """本文と索引のデータファイル。最初に使うときに開く。"""
        if self._zio is None:
            self._zio = Zio(self._find_text_file())
        return self._zio

    # -- 索引表 ------------------------------------------------------------

    def load(self) -> "SubBook":
        """索引表を解析する。何度呼んでもよい。必要なら自動的に呼ばれる。"""
        if self._loaded:
            return self

        page = self.zio.read_page(self.index_page)
        if len(page) < INDEX_TABLE_ENTRY_SIZE:
            raise SubBookError(f"{self.title}: truncated index table")

        count = page[1]
        if count == 0 or count >= len(page) // INDEX_TABLE_ENTRY_SIZE:
            raise SubBookError(f"{self.title}: bad index count {count}")

        global_availability = page[4]
        if global_availability > 0x02:
            global_availability = 0

        for i in range(count):
            offset = (i + 1) * INDEX_TABLE_ENTRY_SIZE
            self._parse_index_entry(
                page[offset : offset + INDEX_TABLE_ENTRY_SIZE], global_availability
            )

        self._attach_font_files()
        self._loaded = True
        return self

    def _search_from_entry(
        self, entry: bytes, global_availability: int
    ) -> Search | None:
        """16 バイトの索引表レコード 1 つから Search を組み立てる。

        multi 検索の表は、この同じ形のレコードを欄ごとに内部で繰り返している。
        そのレコードが **何であるか** の判定と分けてあるのはそのためである。
        """
        from .jacode import CHARCODE_ISO8859_1

        index_id = entry[0]
        start_page = int.from_bytes(entry[2:6], "big")
        block_count = int.from_bytes(entry[6:10], "big")
        if start_page == 0 or block_count == 0:
            return None

        search = Search(
            index_id=index_id,
            start_page=start_page,
            end_page=start_page + block_count - 1,
        )

        availability = entry[10]
        if (global_availability == 0x00 and availability == 0x02) or (
            global_availability == 0x02
        ):
            self._apply_style_flags(search, int.from_bytes(entry[11:14], "big"))
        elif index_id in (0x70, 0x90) or index_id in MULTI_KANA_INDEX_IDS:
            # かなの索引は、既定ですべてを畳む。
            search.katakana = STYLE_CONVERT
            search.lower = STYLE_CONVERT
            search.mark = STYLE_DELETE
            search.long_vowel = STYLE_CONVERT
            search.double_consonant = STYLE_CONVERT
            search.contracted_sound = STYLE_CONVERT
            search.small_vowel = STYLE_CONVERT
            search.voiced_consonant = STYLE_CONVERT
            search.p_sound = STYLE_CONVERT

        if self.book.character_code == CHARCODE_ISO8859_1 or index_id in (0x72, 0x92):
            search.space = STYLE_ASIS
        else:
            search.space = STYLE_DELETE
        return search

    def _parse_index_entry(self, entry: bytes, global_availability: int) -> None:
        from .book import DISC_EB

        search = self._search_from_entry(entry, global_availability)
        if search is None:
            return
        index_id = search.index_id
        start_page = search.start_page

        name = SEARCH_INDEX_IDS.get(index_id)
        if name is not None:
            self.searches[name] = search
        elif index_id == 0xFF:
            self.multi_searches.append(search)
        elif index_id == 0x16:
            if self.book.disc_code != DISC_EB:
                self.search_title_page = start_page
        elif index_id in FONT_INDEX_IDS and self.book.disc_code == DISC_EB:
            width, font_code = FONT_INDEX_IDS[index_id]
            fonts = self.wide_fonts if width == "wide" else self.narrow_fonts
            fonts[font_code] = Font(font_code=font_code, page=start_page)

    # -- multi 検索 --------------------------------------------------------

    @property
    def multis(self) -> list[MultiSearch]:
        """このサブブックの multi 検索。欄まで読み出したもの。

        索引表の 0xff レコードは索引を指しておらず、それ自身の小さな表を
        指している。中身はこうなっている: 2 バイトの欄数、14 バイトの空き、
        続いて欄ごとに、1 バイトの索引数、詰め物 1 バイト、30 バイトの
        ラベル、そしてその数だけの 16 バイト索引レコード——本体の索引表と
        同じ形のレコードである。そのうち 1 つがその欄の語索引で、残りは
        本文のページ（入力できる値のメニューと、ヒットの見出しを読み出す
        一覧）。
        """
        self.load()
        if self._multis is None:
            self._multis = [self._read_multi(s) for s in self.multi_searches]
        return self._multis

    def _read_multi(self, search: Search) -> MultiSearch:
        from .jacode import decode_jisx0208

        page = self.zio.read_page(search.start_page)
        count = int.from_bytes(page[0:2], "big")
        entries: list[MultiEntry] = []
        offset = 16
        for _field in range(count):
            if offset + 32 > len(page):
                raise SubBookError(f"{self.title}: multi search table overruns its page")
            index_count = page[offset]
            label_bytes = page[offset + 2 : offset + 32].split(b"\0")[0]
            entry = MultiEntry(label=decode_jisx0208(label_bytes))
            offset += 32
            for _index in range(index_count):
                record = page[offset : offset + INDEX_TABLE_ENTRY_SIZE]
                offset += INDEX_TABLE_ENTRY_SIZE
                index = self._search_from_entry(record, 0)
                if index is None:
                    continue
                if index.index_id in MULTI_INDEX_IDS:
                    entry.index = index
                else:
                    entry.candidates.append(index)
            entries.append(entry)
        return MultiSearch(search=search, entries=entries)

    @staticmethod
    def _apply_style_flags(search: Search, flags: int) -> None:
        """24 ビットの正規化フラグを Search に展開する。"""
        search.katakana = (flags & 0xC00000) >> 22
        search.lower = (flags & 0x300000) >> 20
        # この欄だけ反転している。0 が「記号を落とす」の意味。
        search.mark = STYLE_DELETE if (flags & 0x0C0000) >> 18 == 0 else STYLE_ASIS
        search.long_vowel = (flags & 0x030000) >> 16
        search.double_consonant = (flags & 0x00C000) >> 14
        search.contracted_sound = (flags & 0x003000) >> 12
        search.small_vowel = (flags & 0x000C00) >> 10
        search.voiced_consonant = (flags & 0x000300) >> 8
        search.p_sound = (flags & 0x0000C0) >> 6

    def _attach_font_files(self) -> None:
        """EPWING はフォントを、catalog に名前の書かれた別ファイルに持つ。

        そのファイルにはフォントが 1 つだけ入っているので、ヘッダは必ず
        第 1 ページにある。EB の本とは違う——あちらでは索引表が、共有の
        本文ファイル内で各フォントが始まるページを示している。
        """
        for fonts, names in (
            (self.narrow_fonts, self._narrow_font_names),
            (self.wide_fonts, self._wide_font_names),
        ):
            for font_code, name in enumerate(names):
                if not name:
                    continue
                font = fonts.setdefault(font_code, Font(font_code))
                font.file_name = name
                font.page = 1

    def font_zio(self, font: Font) -> Zio:
        """``font`` の字形が入っているデータファイル。"""
        if not font.file_name:
            return self.zio

        cached = self._font_zios.get(font.file_name)
        if cached is not None:
            return cached

        directory = find_directory(self.directory_path, "gaiji") or self.directory_path
        path = find_file(directory, font.file_name)
        if path is None:
            raise SubBookError(f"{directory}: no font file {font.file_name!r}")
        zio = Zio(path)
        self._font_zios[font.file_name] = zio
        return zio

    # -- 取り出し口 --------------------------------------------------------

    def search(self, name: str) -> Search | None:
        """名前で索引（``word_asis``、``text`` など）を返す。なければ None。"""
        self.load()
        return self.searches.get(name)

    # -- 項目がどこで終わるか ----------------------------------------------

    @property
    def stop_code(self) -> tuple[int, int] | None:
        """この辞書で項目を終わらせるエスケープシーケンス。

        最初に問われたときに本自身の索引から割り出す。1 秒ほどかかる。
        公開されている appendix のように答えが分かっているなら、代入して
        与えてもよい。
        """
        if isinstance(self._stop_code, _Unset):
            from . import stopcode

            self._stop_code = None  # 失敗を項目ごとに繰り返さないため
            try:
                self._stop_code = stopcode.infer(self)
            except EbError:
                pass
        return self._stop_code

    @stop_code.setter
    def stop_code(self, value: tuple[int, int] | None) -> None:
        self._stop_code = value

    @property
    def available_searches(self) -> list[str]:
        self.load()
        names = sorted(self.searches)
        if self.multi_searches:
            names.append(f"multi({len(self.multi_searches)})")
        return names

    def close(self) -> None:
        for zio in self._font_zios.values():
            zio.close()
        self._font_zios.clear()
        if self._zio is not None:
            self._zio.close()
            self._zio = None

    def __repr__(self) -> str:
        return f"<SubBook {self.code} {self.title!r} dir={self.directory_name!r}>"
