# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux

"""コマンドラインインタフェース: ``python -m bkbook``。"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import NamedTuple

from . import appendix as appendix_module
from . import gaiji as gaiji_module
from . import stopcode as stopcode_module
from .book import Book, find_books, is_book
from .font import FontError, font_set
from .search import (
    Position,
    iter_index,
    search_keyword,
    search_multi,
    search_pattern,
)
from .text import PlainTextRenderer, read_heading, read_text
from .zio import EbError


#: コレクション自身がどうしても教えてくれない唯一のこと——どの辞書から先に
#: 聞きたいか。それ以外——ディスクの在り処、どの appendix がどれと組になるか、
#: それぞれがどんな種類の本か——はすべてディスクから読める。これは好みであり、
#: 好みは書いておくしかない。
#:
#: 置き場所は ``~/.config/bkbook/books.config``。利用者ごとの設定なので、
#: リポジトリに追跡させると各人の編集が ``git pull`` のたびに衝突する。
ORDER_FILE_NAME = "books.config"


def config_home() -> str:
    """XDG の設定ディレクトリ。``$XDG_CONFIG_HOME`` があればそれに従う。"""
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )


def order_file() -> str:
    """順序ファイルの場所。"""
    return os.path.join(config_home(), "bkbook", ORDER_FILE_NAME)


#: タイトルに出てくると辞書の種類が分かる語。
CATEGORY_HINTS = (
    ("百", ("百科", "ペディア", "ブリタニカ", "encyclop")),
    ("英", ("英和", "和英", "英英", "英語", "thesaurus", "dictionary", "roget")),
)
DEFAULT_CATEGORY = "国"

#: TUI が種類をまとめる順。
CATEGORY_ORDER = ("英", "国", "百")


def guess_category(title: str) -> str:
    """タイトルから辞書を 英 / 国 / 百 に振り分ける。分からなければ 国。

    先にタイトルを半角に畳む。自分の名を Ｒｏｇｅｔ’ｓ と書く本と Roget's と
    書く本は、同じ語を意味しているからだ。
    """
    lowered = title.translate(FULL_WIDTH).lower()
    for category, hints in CATEGORY_HINTS:
        if any(hint in lowered for hint in hints):
            return category
    return DEFAULT_CATEGORY


def categorise(subbook) -> str:
    """辞書を 英 / 国 / 百 に振り分ける。タイトルが黙っているときは中身を読む。

    たいていのタイトルは自分が何の本かを言っているが、英語の辞書が日本語の
    書名で出ていることもある——惡魔の辭典 は Ambrose Bierce のものだ——
    そしてタイトルをいくら読んでもそれは分からない。見出し語なら分かる。
    LOVE と LAWYER は 国語辞典 の項目ではない。だからタイトルが何も明かさない
    ときは、最初の数語の見出しを取って、どの文字で書かれているかを見る。
    """
    guess = guess_category(subbook.title or "")
    if guess != DEFAULT_CATEGORY:
        return guess
    try:
        headwords = [
            read_heading(subbook, hit.heading).text
            for _, hit in zip(range(8), iter_index(subbook))
        ]
    except EbError:
        return guess

    latin = 0
    for word in headwords:
        letters = [
            character
            for character in word.translate(FULL_WIDTH)
            if character.isalpha()
        ]
        if letters and all(character.isascii() for character in letters):
            latin += 1
    return "英" if latin * 2 > len(headwords) else guess


class BookSpec(NamedTuple):
    """開く辞書 1 冊と、それを読むのに使う appendix。"""

    path: str
    appendix: str | None = None


def nearby_appendix(book_path: str) -> str | None:
    """コレクションがディスクの隣に置いている appendix ディレクトリ。なければ None。

    Motoyuki Kasahara 氏の appendix は辞書 1 冊につき 1 ディレクトリの形で
    配布されていて、それを持っているコレクションはディスクの隣の
    ``appendix`` ディレクトリにまとめている。そこで見つけられれば、``show eb/plus`` と ``tui eb`` が
    どちらにも何も教えずに同じテキストを出せる。
    """
    candidate = os.path.join(os.path.dirname(os.path.abspath(book_path)), "appendix")
    return candidate if os.path.isdir(candidate) else None


def expand(specs: list[BookSpec], appendix: str | None = None) -> list[BookSpec]:
    """コレクションのディレクトリを、その中の本たちで置き換える。

    各本には、与えられた appendix か、コマンドラインで指定されたものか、
    隣にあるものが割り当てられる——だから辞書 1 冊を名指ししても、それが
    属するコレクションを名指ししても、同じように読める。
    """
    expanded: list[BookSpec] = []
    for spec in specs:
        found = find_books(spec.path)
        if not found:
            raise SystemExit(f"{spec.path}: no dictionary here")
        expanded.extend(
            BookSpec(path, spec.appendix or appendix or nearby_appendix(path))
            for path in found
        )
    return expanded


#: ディスクの音声や図版への位置指定。一部の EPWING ディスクを作った変換が、
#: これを普通の文字として本文に書き込んでいる——ランダムハウス は見出し語の
#: あとに必ず音声の印を、図版のある場所に画像の印を持つ。ここには音を鳴らす
#: 手段も絵を描く手段もないので、どちらも雑音でしかない。画像の印は 1 行を
#: 占めているので、その行ごと落とす。
MEDIA_TAG = r"<(?:sound|image)=[0-9A-Fa-f]+:[0-9A-Fa-f]+>"
MEDIA_MARKER = re.compile(
    rf"^[ 　]*{MEDIA_TAG}[ 　]*\n?|[ 　]*{MEDIA_TAG}", re.MULTILINE
)


#: 全角 ASCII を ASCII に、全角空白を空白に戻す。意図して NFKC より狭くして
#: ある。NFKC は、辞書が項目の構造を示すのに使っている囲み文字まで分解して
#: しまい、広辞苑 の語義番号 ①㋐ を裸の 1 と ア にして、周りの本文との区別を
#: 失わせる。
FULL_WIDTH = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
FULL_WIDTH[0x3000] = 0x20


#: 研究社新和英大辞典 はローマ字表記のアクセントを罫線素片 2 つで書く。
#: 上がるところが ┏、下がるところが ┓。紙の上では語の高い部分の上に線が
#: 引かれていて、この 2 つの角はその線の残りである——雨 は ``a┓me``（高低）、
#: 飴 は ``a┏me``（低高）、日本 は ``Ni┏ho┓n``。端末では表の角が迷い込んだ
#: ようにしか見えないので、結合上線として線を戻してやる。
#:
#: 一括で書き換えても安全である。手元の 22 サブブック全部から 8,000 項目を
#: 見たかぎり、┏ と ┓ はこの辞書にしか現れず、他の罫線素片はまったく現れない。
#: これで表を描いているものは 1 つもない。
PITCH_RISE = "┏"
PITCH_FALL = "┓"
OVERLINE = "̅"


def _in_word(character: str) -> bool:
    return character.isalnum() or character in "'’-−ー"


def _pitch_accent(text: str) -> str:
    """語の高い部分の上に、上線を引き直す。"""
    if PITCH_RISE not in text and PITCH_FALL not in text:
        return text

    out: list[str] = []
    high = False
    for character in text:
        if character == PITCH_RISE:
            high = True
        elif character == PITCH_FALL:
            if high:
                high = False
            else:
                # 上がりのない下がり。その語は高く始まっているので、線は
                # すでに出力した部分の上にかかる。
                for index in range(len(out) - 1, -1, -1):
                    if not _in_word(out[index]):
                        break
                    out[index] += OVERLINE
        else:
            if not _in_word(character):
                high = False
            out.append(character + OVERLINE if high else character)
    return "".join(out)


def _readable(text: str) -> str:
    """項目本文を、端末で読みやすい形にする。

    本文はすべて JIS X 0208 で入っているので、英単語まで ｆｕｌｌ－ｗｉｄｔｈ
    と出てくる。それを畳み戻し、音声と画像の印を落とし、研究社 のアクセントの
    角を上線に戻す。``--raw`` を付ければ全部そのまま残る。
    """
    return _pitch_accent(MEDIA_MARKER.sub("", text.translate(FULL_WIDTH)))


def _open_subbook(args) -> tuple[Book, object]:
    path = args.book
    if not is_book(path):
        # これらのコマンドは辞書 1 冊を指す。複数を取るのは tui だけ。
        inside = find_books(path)
        if inside:
            listed = "\n  ".join(inside[:8])
            more = f"\n  … and {len(inside) - 8} more" if len(inside) > 8 else ""
            raise SystemExit(
                f"{args.book} holds {len(inside)} dictionaries; name one, "
                f"or use `bkbook tui {args.book}`:\n  {listed}{more}"
            )
    book = Book(path)
    if args.subbook is None:
        subbook = book.subbooks[0]
    else:
        try:
            subbook = book.subbook(
                int(args.subbook) if args.subbook.isdigit() else args.subbook
            )
        except (KeyError, IndexError):
            raise SystemExit(
                f"no such subbook: {args.subbook!r} "
                f"(have {[s.directory_name for s in book.subbooks]})"
            )
    # どのコマンドも索引表に何かを尋ねる。2 度読み込んでも損はない——
    # 2 回目の呼び出しはすぐ返る。
    subbook.load()
    return book, subbook


def _chosen(args) -> list[BookSpec]:
    """複数を取るコマンドが指された辞書。"""
    return expand([BookSpec(path) for path in args.books], args.appendix)


def category_order(category: str) -> int:
    """TUI の並べ方に合わせて種類をまとめる整列キー。"""
    return (
        CATEGORY_ORDER.index(category)
        if category in CATEGORY_ORDER
        else len(CATEGORY_ORDER)
    )


def headword_size(subbook) -> int:
    """その本の見出し語索引の大きさ。ページ数で測る。

    見出し語の件数ではない——索引は B 木で、葉は詰まっていない——が、本どうしを
    並べる指標としては忠実で、しかも 50 万項目を歩き回るのではなく、すでに
    読んである目録から得られる。

    TUI が欲しいのはまさにこれである。TUI はキーを打つたびに開いて
    いる辞書すべてを検索し、結果を交互に並べる。だから開く順が一覧の先頭に
    何が来るかを決めるし、打った語が載っている可能性がいちばん高いのは、
    いちばん語数の多い本である。endword 索引は合計から外してある。同じ
    見出し語を逆順に持っているだけなので、二重に数えれば、それを持っている
    本を不当に有利にするだけになる。
    """
    return sum(
        search.page_count
        for name, search in subbook.searches.items()
        if name.startswith("word_")
    )


def preferred_order(path: str | None = None) -> list[str]:
    """順序ファイルに並んだ辞書名。先に読みたいものから。

    1 行に 1 つ——辞書が入っているディレクトリの名前で、コマンドラインでの
    呼び名でもある。空行と ``#`` のコメントは無視する。入っていない名前は
    文句を言わずに飛ばすので、コレクションの一部ずつを持つ複数の機械のあいだで
    同じファイルを持ち回れる。
    """
    try:
        with open(path or order_file(), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    names = []
    for line in lines:
        name = line.split("#", 1)[0].strip().rstrip("/")
        if name:
            names.append(os.path.basename(name))
    return names


def reading_order(category: str, subbook, path: str = "", order=()) -> tuple:
    """一覧の中でその辞書が来る位置。先に読みたいものから。

    まず順序ファイル——そこに名前のある辞書について。次に 英、国、百 の順、
    種類ごとには見出し語のいちばん多いものから——教えてもらわずに本を
    測るなら、これが最善である。
    """
    name = os.path.basename(path.rstrip("/"))
    rank = order.index(name) if name in order else len(order)
    return rank, category_order(category), -headword_size(subbook)


def _theirs(args, specs: list[BookSpec]) -> bool:
    """コマンドラインが辞書を 1 冊ずつ名指ししたかどうか。

    そうであれば、その順に開く——`bkbook tui plus kojien` はその 2 冊を
    その順で、という意味であり、勝手に並べ替えるのは失礼である。引数 1 つで
    指定されたコレクションにはそれ自身の順序がないので、そちらは並べ替える。
    """
    return bool(args.books) and len(specs) == len(args.books)


def _rendering(subbook, args):
    """このサブブックを描くのに使う外字表を決める。

    appendix があれば、項目がどこで終わるかもそれで決まる。なければ、
    サブブックが最初に項目を読むときに自分で割り出す。
    """
    appendix = None
    source = getattr(args, "appendix", None) or nearby_appendix(subbook.book.path)
    if source:
        try:
            appendix = appendix_module.for_subbook(subbook, source)
        except appendix_module.AppendixNotFoundError:
            if getattr(args, "appendix", None):
                raise  # コマンドラインで名指しされたのだから、ないと言う
    if appendix is not None and appendix.stop_code is not None:
        subbook.stop_code = appendix.stop_code
    return gaiji_module.resolve(subbook, args.gaiji, appendix=appendix)


def _format(text: str, args) -> str:
    return text if args.raw else _readable(text)


# -- コマンド -------------------------------------------------------------


def command_info(args) -> int:
    book, _ = _open_subbook(args)
    print(f"path:      {book.path}")
    print(f"format:    {book.disc_code}")
    print(f"encoding:  {book.character_code_name}")
    if book.epwing_version:
        print(f"epwing:    version {book.epwing_version}")
    print(f"subbooks:  {len(book.subbooks)}")
    for subbook in book.subbooks:
        subbook.load()
        print()
        print(f"  [{subbook.code}] {subbook.title}")
        print(f"      directory: {subbook.directory_name}")
        print(f"      data file: {subbook.zio.path}")
        print(f"      searches:  {', '.join(subbook.available_searches) or 'none'}")
        for number, multi in enumerate(subbook.multis):
            print(f"      multi {number}:   {multi.label}")
        for name, which in (("narrow", subbook.narrow_fonts), ("wide", subbook.wide_fonts)):
            if which:
                sizes = ", ".join(f"{f.height}px" for f in which.values())
                print(f"      {name} font: {sizes}")
    book.close()
    return 0


def _print_hits(subbook, hits, args, full: bool = False) -> None:
    """ヒット 1 件 1 行。``full`` なら罫線の下に項目全体を出す。"""
    table = _rendering(subbook, args)
    for index, hit in enumerate(hits, 1):
        heading = read_heading(
            subbook, hit.heading, renderer=PlainTextRenderer(gaiji=table)
        )
        line = _format(heading.text, args).replace("\n", " ").strip()
        if not full:
            print(f"{index:3d}. {line}    [{hit.text}]")
            continue
        body = read_text(subbook, hit.text, renderer=PlainTextRenderer(gaiji=table))
        if index > 1:
            print()
        print(f"── {index}. {line} " + "─" * 20)
        print(_format(body.text, args))


def command_search(args) -> int:
    book, subbook = _open_subbook(args)
    hits = search_pattern(subbook, args.word, exact=args.exact, limit=args.limit)
    if not hits:
        print("no match", file=sys.stderr)
        book.close()
        return 1

    _print_hits(subbook, hits, args)
    book.close()
    return 0


def command_show(args) -> int:
    book, subbook = _open_subbook(args)
    hits = search_pattern(subbook, args.word, exact=args.exact, limit=args.limit)
    if not hits:
        print("no match", file=sys.stderr)
        book.close()
        return 1

    _print_hits(subbook, hits, args, full=True)
    book.close()
    return 0


def command_keyword(args) -> int:
    """keyword 索引を使い、指定した語をすべて含む項目を出す。"""
    book, subbook = _open_subbook(args)
    hits = search_keyword(
        subbook, args.words, index=args.index, limit=args.limit
    )
    if not hits:
        print("no match", file=sys.stderr)
        book.close()
        return 1

    _print_hits(subbook, hits, args, full=args.full)
    book.close()
    return 0


def command_multi(args) -> int:
    """ラベルのついた複数の欄をまとめて埋めて行う検索。"""
    book, subbook = _open_subbook(args)
    multis = subbook.multis
    if not multis:
        print(f"{subbook.title}: no multi search", file=sys.stderr)
        book.close()
        return 1

    if args.candidates is not None:
        try:
            entry = multis[args.multi].entries[args.candidates]
        except IndexError:
            raise SystemExit(f"no field {args.candidates} in multi {args.multi}")
        if entry.menu is None:
            print(f"{entry.label}: no menu of values", file=sys.stderr)
            book.close()
            return 1
        position = (
            _parse_position(args.at) if args.at else Position(entry.menu.start_page, 1)
        )
        table = _rendering(subbook, args)
        result = read_text(subbook, position, renderer=PlainTextRenderer(gaiji=table))
        for index, candidate in enumerate(result.candidates, 1):
            label = _format(candidate.text_of(result.text), args).strip()
            where = f"[{candidate.position}]" if candidate.position else "(a word)"
            print(f"{index:3d}. {label}    {where}")
        book.close()
        return 0

    if not args.words:
        for number, multi in enumerate(multis):
            print(f"{number}. {multi.label}")
            for field, entry in enumerate(multi.entries):
                answers = "" if entry.index else "   (no index)"
                values = "   -c to list its values" if entry.menu else ""
                print(f"     {field}. {entry.label}{answers}{values}")
        book.close()
        return 0

    words = ["" if word == "-" else word for word in args.words]
    hits = search_multi(subbook, args.multi, words, limit=args.limit)
    if not hits:
        print("no match", file=sys.stderr)
        book.close()
        return 1

    _print_hits(subbook, hits, args, full=args.full)
    book.close()
    return 0


def command_entry(args) -> int:
    """page:offset を直接指定して項目を出す。`search` が表示する形式。"""
    book, subbook = _open_subbook(args)
    position = _parse_position(args.position)

    table = _rendering(subbook, args)
    body = read_text(subbook, position, renderer=PlainTextRenderer(gaiji=table))
    print(_format(body.text, args))
    book.close()
    return 0


def _parse_position(text: str) -> Position:
    try:
        page_text, offset_text = text.split(":", 1)
        return Position(int(page_text), int(offset_text))
    except ValueError:
        raise SystemExit(f"expected page:offset, got {text!r}") from None


def command_menu(args) -> int:
    """その本自身の目次、またはその 1 ページを出す。"""
    book, subbook = _open_subbook(args)
    search = subbook.searches.get("menu")
    if search is None:
        print(f"{subbook.title}: no menu", file=sys.stderr)
        book.close()
        return 1

    position = (
        _parse_position(args.position) if args.position else Position(search.start_page, 1)
    )
    table = _rendering(subbook, args)
    result = read_text(subbook, position, renderer=PlainTextRenderer(gaiji=table))
    if result.candidates:
        for index, candidate in enumerate(result.candidates, 1):
            label = _format(candidate.text_of(result.text), args).strip()
            where = f"[{candidate.position}]" if candidate.position else ""
            print(f"{index:3d}. {label}    {where}")
    else:
        print(_format(result.text, args))
    book.close()
    return 0


def command_copyright(args) -> int:
    """出版社の権利表示を出す。どのディスクにも入っている。"""
    book, subbook = _open_subbook(args)
    search = subbook.searches.get("copyright")
    if search is None:
        print(f"{subbook.title}: no copyright page", file=sys.stderr)
        book.close()
        return 1
    table = _rendering(subbook, args)
    result = read_text(
        subbook, Position(search.start_page, 1), renderer=PlainTextRenderer(gaiji=table)
    )
    print(_format(result.text, args))
    book.close()
    return 0


def command_list(args) -> int:
    """開かれる辞書を、開かれる順に表示する。"""
    from .layout import display_width, pad

    specs = _chosen(args)
    order = preferred_order()
    rows = []
    for spec in specs:
        book = Book(spec.path)
        appendix_path = None
        for subbook in book.subbooks:
            subbook.load()
            if spec.appendix and appendix_path is None:
                found = appendix_module.candidates(spec.appendix, subbook)
                appendix_path = found[0] if len(found) == 1 else None
            category = categorise(subbook)
            rows.append(
                (
                    spec,
                    subbook.title,
                    book.disc_code,
                    appendix_path,
                    category,
                    reading_order(category, subbook, spec.path, order),
                )
            )
        book.close()
    if not _theirs(args, specs):
        rows.sort(key=lambda row: row[5])

    if args.order:
        # 順序ファイルそのもの。いま入っているものから出発して、望む順に
        # 編集できるようにするため。1 行 1 冊であって 1 サブブックではない。
        # このファイルが挙げるのはディレクトリで、heritage は 2 つ持っている。
        print("# 辞書を開く順。先に読みたいものから。")
        print(f"# 置き場所: {order_file()}")
        print("# 書かなかったものはこのあとに、種類ごとに大きい順で開く。")
        seen = set()
        for spec, _, _, _, _, _ in rows:
            name = os.path.basename(spec.path.rstrip("/"))
            if name not in seen:
                seen.add(name)
                print(name)
        return 0

    marks = [
        os.path.basename(row[3]) if row[3] else "—" for row in rows
    ]
    title_width = max((display_width(row[1]) for row in rows), default=10)
    mark_width = max((display_width(mark) for mark in marks), default=8)

    print(
        f"{'#':>3}  {pad('dictionary', title_width)}  kind  format  "
        f"{pad('appendix', mark_width)}  path"
    )
    for index, ((spec, title, disc, _, category, _), mark) in enumerate(
        zip(rows, marks), 1
    ):
        print(
            f"{index:>3}  {pad(title, title_width)}  {pad(category, 4)}  "
            f"{disc:<6}  {pad(mark, mark_width)}  {spec.path}"
        )
    return 0


def command_tui(args) -> int:
    from .tui import Source, browse

    specs = _chosen(args)
    books = []
    sources = []
    notes = []
    for spec in specs:
        book = Book(spec.path)
        books.append(book)
        # ディスクの隣で見つけた appendix のほうが、コマンドラインで
        # 指定されたものより優先される。後者がすべての辞書に同時に正しい
        # ということはありえないからだ。
        appendix_source = spec.appendix or args.appendix
        for subbook in book.subbooks:
            subbook.load()
            appendix = None
            if appendix_source:
                try:
                    appendix = appendix_module.for_subbook(subbook, appendix_source)
                except appendix_module.AppendixNotFoundError:
                    pass  # そもそも大半の辞書にはない。言うほどのことではない
                except EbError as error:
                    notes.append(str(error))
            if appendix is not None and appendix.stop_code is not None:
                subbook.stop_code = appendix.stop_code
            # 辞書はたまたま入っているディレクトリ名ではなく、それ自身の
            # タイトルで呼ぶ。dgx01 や gejcje は何も語らないが、
            # 三省堂「大辞林」 や 新グローバル英和 は語る。
            sources.append(
                Source(
                    label=subbook.title or subbook.directory_name,
                    subbook=subbook,
                    title=subbook.title or subbook.directory_name,
                    category=categorise(subbook),
                    gaiji=gaiji_module.resolve(subbook, args.gaiji, appendix=appendix),
                )
            )

    if not sources:
        raise SystemExit("no subbooks to open")
    # 開いている辞書はキーを打つたびに全部検索され、結果は交互に並べられる。
    # だからこれが、誰のヒットが先頭に来るかを決める。コマンドラインで
    # 名指しされていれば、それが無条件に優先する。
    if not _theirs(args, specs):
        order = preferred_order()
        sources.sort(
            key=lambda s: reading_order(
                s.category, s.subbook, s.subbook.book.path, order
            )
        )

    for note in notes:
        print(f"bkbook: {note}", file=sys.stderr)

    readable = (lambda text: text) if args.raw else _readable
    try:
        browse(sources, readable)
    except ImportError:
        raise SystemExit("tui needs the curses module, which this Python lacks")
    finally:
        for book in books:
            book.close()
    return 0


def command_gaiji(args) -> int:
    book, subbook = _open_subbook(args)
    table = _rendering(subbook, args)

    try:
        fonts = font_set(subbook, narrow=True), font_set(subbook, narrow=False)
    except FontError as error:
        raise SystemExit(str(error))

    wanted = set()
    for code_text in args.codes:
        if len(code_text) != 5 or code_text[0] not in "hz":
            raise SystemExit(f"expected h/z + 4 hex digits, got {code_text!r}")
        wanted.add((code_text[0] == "h", int(code_text[1:], 16)))

    for fonts_narrow in fonts:
        narrow = fonts_narrow.narrow
        for code in fonts_narrow.codes():
            key = (narrow, code)
            if wanted and key not in wanted:
                continue
            if not wanted and args.unmapped and key in table:
                continue
            label = gaiji_module.format_code(narrow, code)
            mapped = table.get(key)
            suffix = f"  →  {mapped!r}" if mapped is not None else "  (unmapped)"
            print(f"{label}{suffix}")
            print(fonts_narrow.bitmap(code).to_text())
            print()
    book.close()
    return 0


def command_stopcode(args) -> int:
    """どのエスケープが項目を終わらせているかと、その根拠の強さを表示する。"""
    book, _ = _open_subbook(args)
    for subbook in book.subbooks:
        subbook.load()
        print(f"[{subbook.code}] {subbook.title}")
        candidates = stopcode_module.report(subbook)
        if not candidates:
            print("      no candidate: this subbook has no headword index")
            continue
        chosen = stopcode_module.infer(subbook)
        for candidate in candidates:
            mark = "->" if candidate.stop_code == chosen else "  "
            print(
                f"   {mark} 0x{candidate.code:04x} 0x{candidate.argument:04x}"
                f"  opens {candidate.share:6.1%} of entries at {candidate.offset:+d}"
                f", reads {candidate.score:6.1%} of them to the next one"
            )
        source = getattr(args, "appendix", None) or nearby_appendix(book.path)
        appendix = None
        if source:
            try:
                appendix = appendix_module.for_subbook(subbook, source)
            except appendix_module.AppendixNotFoundError:
                pass
        if appendix is not None and appendix.stop_code is not None:
            published = appendix.stop_code
            agree = "agrees" if published == chosen else "DISAGREES"
            print(
                f"      appendix says 0x{published[0]:04x} 0x{published[1]:04x}"
                f" — {agree}"
            )
    book.close()
    return 0


# -- 引数の解析 -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bkbook",
        description="Read EB (電子ブック) and EPWING dictionaries.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub, *, needs_word: bool = False):
        sub.add_argument("book", help="path to the book directory")
        if needs_word:
            sub.add_argument(
                "word",
                help="word to look up; a leading * matches the end of the "
                "headword instead of the start, as in '*ization'",
            )
        sub.add_argument(
            "-s", "--subbook", help="subbook index, directory name, or title"
        )
        sub.add_argument(
            "-a",
            "--appendix",
            help="appendix (*.app) file or directory; supplies gaiji text "
            "for every character the book defines, and the entry stop code",
        )
        sub.add_argument(
            "-g", "--gaiji", help="gaiji mapping file to layer over the built-in one"
        )
        sub.add_argument(
            "--raw",
            action="store_true",
            help="keep full-width characters instead of folding them to ASCII",
        )

    tui_parser = subparsers.add_parser(
        "tui", help="interactive search across one or more dictionaries"
    )
    tui_parser.add_argument(
        "books",
        nargs="+",
        help="book or collection directories to open",
    )
    tui_parser.add_argument("-a", "--appendix")
    tui_parser.add_argument("-g", "--gaiji")
    tui_parser.add_argument("--raw", action="store_true")
    tui_parser.set_defaults(handler=command_tui)

    info = subparsers.add_parser("info", help="show what a book contains")
    add_common(info)
    info.set_defaults(handler=command_info)

    search_parser = subparsers.add_parser("search", help="list matching headwords")
    add_common(search_parser, needs_word=True)
    search_parser.add_argument("-n", "--limit", type=int, default=30)
    search_parser.add_argument(
        "-e", "--exact", action="store_true", help="whole-headword match"
    )
    search_parser.set_defaults(handler=command_search)

    show = subparsers.add_parser("show", help="print matching entries in full")
    add_common(show, needs_word=True)
    show.add_argument("-n", "--limit", type=int, default=3)
    show.add_argument("-e", "--exact", action="store_true", help="whole-headword match")
    show.set_defaults(handler=command_show)

    keyword_parser = subparsers.add_parser(
        "keyword",
        help="find entries that contain given words, not entries named by them",
    )
    add_common(keyword_parser)
    keyword_parser.add_argument(
        "words",
        nargs="+",
        help="words that must all appear in the entry",
    )
    keyword_parser.add_argument("-n", "--limit", type=int, default=30)
    keyword_parser.add_argument(
        "-f", "--full", action="store_true", help="print the entries, not just a list"
    )
    keyword_parser.add_argument(
        "-i",
        "--index",
        default="keyword",
        choices=("keyword", "cross"),
        help="which of the two occurrence indexes to search (default: keyword)",
    )
    keyword_parser.set_defaults(handler=command_keyword)

    multi_parser = subparsers.add_parser(
        "multi",
        help="a search with several labelled fields; with no words, list them",
    )
    add_common(multi_parser)
    multi_parser.add_argument(
        "words",
        nargs="*",
        help="one word per field, in the order `multi` with no words lists "
        "them; '-' leaves a field unasked",
    )
    multi_parser.add_argument(
        "-m", "--multi", type=int, default=0, help="which multi search (default: 0)"
    )
    multi_parser.add_argument(
        "-c",
        "--candidates",
        type=int,
        metavar="FIELD",
        help="list what this field can be asked for, instead of searching",
    )
    multi_parser.add_argument(
        "--at",
        metavar="PAGE:OFFSET",
        help="with -c, walk further down the list of values",
    )
    multi_parser.add_argument("-n", "--limit", type=int, default=30)
    multi_parser.add_argument(
        "-f", "--full", action="store_true", help="print the entries, not just a list"
    )
    multi_parser.set_defaults(handler=command_multi)

    entry = subparsers.add_parser("entry", help="print the entry at a page:offset")
    add_common(entry)
    entry.add_argument("position", help="location as printed by `search`, e.g. 2386:1754")
    entry.set_defaults(handler=command_entry)

    menu_parser = subparsers.add_parser(
        "menu", help="the book's own table of contents"
    )
    add_common(menu_parser)
    menu_parser.add_argument(
        "position",
        nargs="?",
        help="a submenu, as printed beside a line of the menu above it",
    )
    menu_parser.set_defaults(handler=command_menu)

    copyright_parser = subparsers.add_parser(
        "copyright", help="the publisher's notice"
    )
    add_common(copyright_parser)
    copyright_parser.set_defaults(handler=command_copyright)

    list_parser = subparsers.add_parser(
        "list", help="show the dictionaries that would be opened, in order"
    )
    list_parser.add_argument(
        "books",
        nargs="+",
        help="book or collection directories",
    )
    list_parser.add_argument("-a", "--appendix")
    list_parser.add_argument(
        "-o",
        "--order",
        action="store_true",
        help="print as an order file, ready to reorder and save back",
    )
    list_parser.set_defaults(handler=command_list)

    gaiji_parser = subparsers.add_parser(
        "gaiji", help="show gaiji bitmaps so a mapping can be written"
    )
    add_common(gaiji_parser)
    gaiji_parser.add_argument(
        "codes", nargs="*", help="codes to show, e.g. ha14c zb121 (default: all)"
    )
    gaiji_parser.add_argument(
        "-u", "--unmapped", action="store_true", help="only those with no mapping yet"
    )
    gaiji_parser.set_defaults(handler=command_gaiji)

    stopcode_parser = subparsers.add_parser(
        "stopcode", help="show the escape that ends an entry, and how it was found"
    )
    add_common(stopcode_parser)
    stopcode_parser.set_defaults(handler=command_stopcode)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except EbError as error:
        print(f"bkbook: {error}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
