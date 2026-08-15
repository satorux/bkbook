# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux

"""端末の対話インタフェース: ``python -m bkbook tui``。

打てば打つだけ見出し語の一覧が絞られ、選んでいるものの項目がその下に出る。
辞書は何冊でも同時に開いておける。キーを打つたびに全部が検索され、結果には
どの辞書から来たかの印がつく。

標準ライブラリの ``curses`` の上に作ってあるので、ここでも入れるものはない。
日本語があると配置の計算は自明でなくなる。1 文字が端末の 2 桁を占めることも
あれば、結合文字なら 0 桁のこともある。だから幅の測定と切り詰めはすべて
:func:`display_width` を通す。
"""

from __future__ import annotations

import locale
import sys
from dataclasses import dataclass, field

from .layout import char_width, display_width, pad, truncate, wrap
from .search import (
    Hit,
    NoSuchSearchError,
    parse_pattern,
    search,
    search_keyword,
)
from .setword import WordError
from .text import PlainTextRenderer, read_heading, read_text
from .zio import EbError

#: キーを 1 打するごとに、各辞書から取るヒット数。
HITS_PER_SOURCE = 40

#: 最初の 2 ヒットが対になっている辞書。
#:
#: リーダーズ＋プラス は 1 つのサブブックを共有する 2 冊の辞書である。両方に
#: 載っている語は、リーダーズ の下に 1 回、さらに [プラス] を冠して
#: リーダーズ・プラス の下にもう 1 回収められている。1 巡につき 1 ヒットだと
#: 片割れが 1 巡分あとに回り——20 数冊も開いていれば一覧の枠の外に出て——
#: 対が並んで現れることがなくなる。
#:
#: 全部の辞書から 2 つずつ取ればこれは直るが、その費用を他のすべての辞書が
#: 払うことになる。3 つ目のヒットが、20 数冊が 2 つ目を出し終えるまで
#: 待たされるのだ。広辞苑 が 羅生門 と 羅生門河岸 を先頭に出しながら
#: 羅生門蔓 を 13 行下に置いたのはそれが理由だった。この形で 2 冊を
#: 詰め込んでいる本は他にないので、例外として名指ししてある。
PAIRED_TITLES = {"リーダーズ＋プラス英和辞典"}

#: 検索語が始まる桁。カーソルもここにいる。
QUERY_COLUMN = 8

#: 上下に分けたとき、見出し語一覧が取る画面の割合。
LIST_HEIGHT_SHARE = 0.45

#: 辞書名の列が取ってよい幅の上限。
LABEL_SHARE = 0.4

HELP = (
    "^P/^N select   TAB kind   ^K keyword   ^U/^D scroll   ^W delete word   ESC quit"
)

#: まだ何も打っていないとき、項目が出る場所に表示する文。検索について
#: 唯一推測できないのがワイルドカードなので、下端のキー一覧を混ませる
#: 代わりにここで言う。
EMPTY_HINT = "type to search   —   *ization matches the end of a headword"

#: ^K で見出し語ではなく本文を検索する側に切り替えたときに、代わりに出す文。
#: 入力欄の見た目では 2 つの問いが区別できないからである。
KEYWORD_HINT = (
    "keyword search: entries that use every word   —   ^K for headwords again"
)


@dataclass
class Source:
    """開いている辞書 1 冊。"""

    label: str
    subbook: object
    title: str = ""
    category: str = ""
    gaiji: dict = field(default_factory=dict)
    hits_per_round: int = 1

    def __post_init__(self):
        if not self.title:
            self.title = self.label
        if self.title in PAIRED_TITLES:
            self.hits_per_round = 2


@dataclass
class Entry:
    """検索結果 1 件と、それが指す本文。"""

    source: Source
    hit: Hit
    heading: str


class Browser:
    def __init__(self, screen, sources: list[Source], readable):
        self.screen = screen
        self.sources = sources
        self.readable = readable

        self.query = ""
        self.keyword = False
        self.entries: list[Entry] = []
        self.selected = 0
        self.scroll = 0        # 画面に出ている最初の見出し語の行
        self.scroll_body = 0   # 画面に出ている最初の本文の行
        self.status = ""
        self._body_cache: dict[tuple[int, int, int], list[str]] = {}
        self._body_width = 0

    # -- 検索 --------------------------------------------------------------

    def refresh_results(self) -> None:
        self.entries = []
        self.selected = 0
        self.scroll = 0
        self._body_cache.clear()

        if self.keyword:
            words = self.query.split()
            word, backward = " ".join(words), False
        else:
            try:
                word, backward = parse_pattern(self.query)
            except WordError as error:
                self.status = str(error)
                return
        if not word:
            self.status = ""
            return

        errors = []
        per_source = []
        for source in self.sources:
            try:
                if self.keyword:
                    hits = search_keyword(
                        source.subbook, words, limit=HITS_PER_SOURCE
                    )
                else:
                    hits = search(
                        source.subbook, word, limit=HITS_PER_SOURCE, backward=backward
                    )
            except (WordError, NoSuchSearchError):
                # この辞書はその問い合わせを表現も索引もできない——英語
                # だけの本に日本語の語を投げた、など。これは結果がないと
                # いうことであって、報告すべき失敗ではない。
                continue
            except EbError as error:
                errors.append(f"{source.label}: {error}")
                continue
            per_source.append((source, [Entry(source, hit, "") for hit in hits]))

        # 交互に並べて、1 冊が画面を埋め尽くさないようにする。1 巡につき
        # 1 つずつ。ヒットを対で求める本だけは例外。
        cursors = [0] * len(per_source)
        while True:
            took = False
            for index, (source, hits) in enumerate(per_source):
                start = cursors[index]
                if start >= len(hits):
                    continue
                self.entries.extend(hits[start : start + source.hits_per_round])
                cursors[index] = start + source.hits_per_round
                took = True
            if not took:
                break
        self.status = "; ".join(errors)

    def heading_of(self, entry: Entry) -> str:
        if not entry.heading:
            try:
                result = read_heading(
                    entry.source.subbook,
                    entry.hit.heading,
                    renderer=PlainTextRenderer(gaiji=entry.source.gaiji),
                )
                entry.heading = self.readable(result.text).strip() or "(no heading)"
            except EbError as error:
                entry.heading = f"(error: {error})"
        return entry.heading

    def body_of(self, entry: Entry, columns: int) -> list[str]:
        key = (id(entry.source), entry.hit.text.page, entry.hit.text.offset)
        if columns != self._body_width:
            self._body_cache.clear()
            self._body_width = columns
        cached = self._body_cache.get(key)
        if cached is not None:
            return cached

        try:
            result = read_text(
                entry.source.subbook,
                entry.hit.text,
                renderer=PlainTextRenderer(gaiji=entry.source.gaiji),
            )
            lines = wrap(self.readable(result.text), columns)
        except EbError as error:
            lines = [f"(error: {error})"]
        self._body_cache[key] = lines
        return lines

    # -- 描画 --------------------------------------------------------------

    def _write(self, row: int, column: int, text: str, attribute: int = 0) -> None:
        """右下隅と画面の狭さに耐える addstr。"""
        height, width = self.screen.getmaxyx()
        if not 0 <= row < height:
            return
        text = truncate(text, max(0, width - column - 1))
        if not text:
            return
        try:
            self.screen.addstr(row, column, text, attribute)
        except Exception:
            pass

    def draw(self) -> None:
        import curses

        self.screen.erase()
        height, width = self.screen.getmaxyx()

        count = f"{self.selected + 1}/{len(self.entries)}" if self.entries else "0"
        self._write(0, 1, "word" if not self.keyword else "kwrd", curses.A_BOLD)
        self._write(0, 6, "|")
        self._write(0, QUERY_COLUMN, self.query, curses.A_BOLD)

        # 選択中の項目がどの辞書から来たかを示す。一覧には略称分の幅しか
        # ないので、完全なタイトルはここに出す。
        right = count
        if self.entries:
            title = self.entries[self.selected].source.title
            room = width - display_width(count) - display_width(self.query) - 14
            if display_width(title) <= room:
                right = f"{title}   {count}"
        self._write(0, max(9, width - display_width(right) - 2), right, curses.A_DIM)
        self._write(1, 0, "─" * (width - 1), curses.A_DIM)

        top = 2
        available = max(2, height - top - 2)

        # Lookup と同じ配置。一覧を横幅いっぱいに、その下に項目。こうすれば
        # 辞書名を略さずに収められる。
        list_rows = max(3, min(available - 3, int(available * LIST_HEIGHT_SHARE)))
        body_rows = available - list_rows - 1

        self._draw_list(top, list_rows, 0, width - 1)
        self._write(top + list_rows, 0, "─" * (width - 1), curses.A_DIM)
        self._draw_body(top + list_rows + 1, body_rows, 0, width - 1)

        self._write(height - 2, 0, "─" * (width - 1), curses.A_DIM)
        self._write(height - 1, 1, self.status or HELP, curses.A_DIM)

        # カーソルは入力位置に残す。入力メソッドは変換中の文字をカーソルの
        # ところに描くので、描画がたまたま終わった場所に置きっぱなしにすると、
        # 打ちかけのかなが最下行に出てしまう。
        try:
            self.screen.move(0, min(width - 1, QUERY_COLUMN + display_width(self.query)))
        except curses.error:
            pass
        self.screen.refresh()

    def _label_width(self, columns: int) -> int:
        """辞書名の列の幅。実際に使われている名前に合わせる。"""
        if len(self.sources) < 2:
            return 0
        widest = max(display_width(source.label) for source in self.sources)
        return min(widest, max(8, int(columns * LABEL_SHARE)))

    def _draw_list(self, top: int, rows: int, column: int, columns: int) -> None:
        import curses

        # 選択行を画面内に保つ。
        if self.selected < self.scroll:
            self.scroll = self.selected
        elif self.selected >= self.scroll + rows:
            self.scroll = self.selected - rows + 1

        label_width = self._label_width(columns)
        category_width = max(
            (display_width(source.category) for source in self.sources), default=0
        )
        for index in range(rows):
            position = self.scroll + index
            if position >= len(self.entries):
                break
            entry = self.entries[position]
            chosen = position == self.selected
            attribute = curses.A_REVERSE if chosen else 0
            row = top + index
            at = column

            self._write(row, at, "▸ " if chosen else "  ", attribute)
            at += 2
            room = columns - 2

            # 種類の表示は目のための足場であって中身ではない。薄く出して、
            # 行を分けはしても見出し語と張り合わないようにする。
            if category_width:
                self._write(
                    row,
                    at,
                    pad(entry.source.category, category_width) + " ",
                    attribute | curses.A_DIM,
                )
                at += category_width + 1
                room -= category_width + 1

            text = self.heading_of(entry)
            if label_width:
                text = f"{pad(entry.source.label, label_width)} {text}"
            self._write(row, at, truncate(text, room), attribute)

    def _draw_body(self, top: int, rows: int, column: int, columns: int) -> None:
        import curses

        if not self.entries:
            # ワイルドカードしかない問い合わせには、まだ語が入っていない。
            typed = self.query.strip(" 　*")
            empty = KEYWORD_HINT if self.keyword else EMPTY_HINT
            message = "no match" if typed else empty
            self._write(top, column, message, curses.A_DIM)
            return

        lines = self.body_of(self.entries[self.selected], columns)
        for index in range(rows):
            position = self.scroll_body + index
            if position >= len(lines):
                break
            self._write(top + index, column, lines[position])

    # -- 入力 --------------------------------------------------------------

    def move(self, delta: int) -> None:
        if not self.entries:
            return
        self.selected = max(0, min(len(self.entries) - 1, self.selected + delta))
        self.scroll_body = 0

    def move_category(self, direction: int = 1) -> None:
        """種類の違う辞書からのヒットへ飛ぶ。

        結果は順位で交互に並ぶので、複数の種類が答えられる問い合わせでは
        英 のヒットの連なり、次に 国、次に 百 という並びになる。これは連なりを
        1 つずつたどる代わりに、次の連なりの先頭へ飛ぶ。
        """
        if not self.entries:
            return
        current = self.entries[self.selected].source.category
        count = len(self.entries)
        for step in range(1, count + 1):
            index = (self.selected + direction * step) % count
            if self.entries[index].source.category != current:
                self.selected = index
                self.scroll_body = 0
                return

    def run(self) -> None:
        import curses

        # カーソルは見えるままにしておく。挿入位置を示すものであり、
        # 入力メソッドが変換中の文字を描く場所も要るからだ。
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        self.screen.keypad(True)

        while True:
            self.draw()
            try:
                key = self.screen.get_wch()
            except KeyboardInterrupt:
                return
            except curses.error:
                continue

            if key in ("\x1b", "\x03"):  # ESC, Ctrl-C
                return
            if key == curses.KEY_RESIZE:
                self._body_cache.clear()
                continue
            if key in (curses.KEY_UP, "\x10"):  # Ctrl-P
                self.move(-1)
            elif key in (curses.KEY_DOWN, "\x0e"):  # Ctrl-N
                self.move(1)
            elif key == "\t":
                self.move_category(1)
            elif key == curses.KEY_BTAB:
                self.move_category(-1)
            elif key == curses.KEY_PPAGE:
                self.move(-10)
            elif key == curses.KEY_NPAGE:
                self.move(10)
            elif key == "\x15":  # Ctrl-U
                self.scroll_body = max(0, self.scroll_body - 10)
            elif key == "\x04":  # Ctrl-D
                self.scroll_body += 10
            elif key == "\x0b":  # Ctrl-K
                self.keyword = not self.keyword
                self.refresh_results()
            elif key == "\x0c":  # Ctrl-L
                self._body_cache.clear()
                self.screen.clearok(True)
            elif key == "\x17":  # Ctrl-W
                self.query = self.query.rstrip()
                cut = max(self.query.rfind(" "), self.query.rfind("\t"))
                self.query = self.query[: cut + 1] if cut >= 0 else ""
                self.refresh_results()
            elif key in ("\x7f", "\x08", curses.KEY_BACKSPACE):
                self.query = self.query[:-1]
                self.refresh_results()
            elif isinstance(key, str) and key.isprintable():
                self.query += key
                self.refresh_results()


class NotATerminal(EbError):
    """TUI には描画先の端末が必要。"""


def browse(sources: list[Source], readable) -> None:
    """利用者が終了するまで TUI を動かす。"""
    import curses

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise NotATerminal(
            "tui needs a terminal; use `search` or `show` when redirecting"
        )

    def start(screen):
        # curses.wrapper は色機能を初期化し、その結果 color pair 0 が
        # 「黒地に白」の意味になる——これは画面全体をその 2 色で塗り直し、
        # 端末に設定されているテーマを捨ててしまう。use_default_colors は
        # pair 0 を「端末がすでに使っているもの」に戻すので、TUI は
        # 利用者自身の色で描き、変えるのは属性——太字、淡色、反転——だけになる。
        try:
            curses.use_default_colors()
        except curses.error:
            pass  # 色に対応していない端末なら、直すものもない
        Browser(screen, sources, readable).run()

    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(start)
