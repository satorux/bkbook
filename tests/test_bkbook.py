# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux

"""bkbook のテスト。

``python -m unittest discover tests`` で実行する。

実物の辞書が要るテストは ``BKBOOK_TEST_BOOK``（またはリポジトリの隣の
``eb/plus`` か ``plus/``）を探し、なければスキップする。コレクション全体を
歩くテストは ``BKBOOK_TEST_COLLECTION``（または隣の ``eb/``）を見る。
純粋に論理だけのテストはどこでも走るようにしてある。
"""

from __future__ import annotations

import argparse
import os
import sys
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bkbook import Book  # noqa: E402
from bkbook import appendix, cli, gaiji, match, setword, stopcode, tui  # noqa: E402
from bkbook.jacode import decode_jisx0208, decode_jisx0208_char  # noqa: E402
from bkbook.search import (  # noqa: E402
    NoSuchSearchError,
    Position,
    parse_pattern,
    search,
)
from bkbook.text import bcd2, bcd4, read_heading, read_text  # noqa: E402
from bkbook.zio import EbError, Zio, ZioError  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_test_book() -> str:
    """結合テストの相手になる実物の辞書。あれば。"""
    named = os.environ.get("BKBOOK_TEST_BOOK")
    if named:
        return named
    for relative in ("eb/plus", "plus"):
        candidate = os.path.join(REPO, relative)
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(REPO, "plus")


BOOK_PATH = _find_test_book()
HAS_BOOK = os.path.isdir(BOOK_PATH)


def _find_test_collection() -> list["cli.BookSpec"]:
    """コレクション全体を相手にする結合テストの入力。あれば。

    どこに辞書があるかは CLI の関知するところではない（パスは必ず引数で
    渡す）ので、探すのはテストの側の仕事である。``BKBOOK_TEST_COLLECTION``、
    なければリポジトリの隣の ``eb`` を見て、なければ空。
    """
    named = os.environ.get("BKBOOK_TEST_COLLECTION") or os.path.join(REPO, "eb")
    if not os.path.isdir(named):
        return []
    try:
        return cli.expand([cli.BookSpec(named)])
    except SystemExit:  # 辞書が 1 冊も入っていない
        return []


TEST_COLLECTION = _find_test_collection()


class JacodeTest(unittest.TestCase):
    def test_decode_title_strips_padding(self):
        raw = b"\x25\x6a\x21\x3c\x25\x40\x21\x3c\x25\x3a" + b"\x00" * 4
        self.assertEqual(decode_jisx0208(raw), "リーダーズ")

    def test_decode_single_character(self):
        self.assertEqual(decode_jisx0208_char(0x256A), "リ")

    def test_trailing_spaces_are_padding(self):
        self.assertEqual(decode_jisx0208(b"\x25\x6a" + b"  "), "リ")


class SetWordTest(unittest.TestCase):
    def test_ascii_becomes_full_width_jis(self):
        raw, code = setword.convert_to_jisx0208("book")
        self.assertEqual(bytes(raw), b"\x23\x62\x23\x6f\x23\x6f\x23\x6b")
        self.assertEqual(code, setword.WORD_ALPHABET)

    def test_kana_is_classified_as_kana(self):
        _raw, code = setword.convert_to_jisx0208("ほん")
        self.assertEqual(code, setword.WORD_KANA)

    def test_mixed_script_is_other(self):
        _raw, code = setword.convert_to_jisx0208("本book")
        self.assertEqual(code, setword.WORD_OTHER)

    def test_surrounding_spaces_are_ignored(self):
        bare, _ = setword.convert_to_jisx0208("book")
        padded, _ = setword.convert_to_jisx0208("  book  ")
        self.assertEqual(bytes(bare), bytes(padded))

    def test_empty_word_is_rejected(self):
        with self.assertRaises(setword.WordError):
            setword.convert_to_jisx0208("   ")

    def test_half_width_katakana_is_widened(self):
        half, _ = setword.convert_to_jisx0208("ｱ")
        full, _ = setword.convert_to_jisx0208("ア")
        self.assertEqual(bytes(half), bytes(full))


class CanonicalizationTest(unittest.TestCase):
    """索引を引く前に libeb がかける正規化。"""

    def _fix(self, text, backward=False, **style):
        from bkbook.subbook import STYLE_ASIS, STYLE_CONVERT, STYLE_DELETE, Search

        defaults = dict(
            katakana=STYLE_ASIS, lower=STYLE_ASIS, mark=STYLE_ASIS,
            long_vowel=STYLE_ASIS, double_consonant=STYLE_ASIS,
            contracted_sound=STYLE_ASIS, small_vowel=STYLE_ASIS,
            voiced_consonant=STYLE_ASIS, p_sound=STYLE_ASIS, space=STYLE_ASIS,
        )
        defaults.update(style)
        search_index = Search(index_id=0x91, start_page=1, end_page=1, **defaults)
        raw, code = setword.convert_to_jisx0208(text)
        return setword.fix_word(
            search_index, raw, 2, code, backward=backward
        ).canonicalized

    def test_katakana_folds_to_hiragana(self):
        from bkbook.subbook import STYLE_CONVERT

        self.assertEqual(
            self._fix("カナ", katakana=STYLE_CONVERT), self._fix("かな")
        )

    def test_lower_case_folds_to_upper(self):
        from bkbook.subbook import STYLE_CONVERT

        self.assertEqual(self._fix("abc", lower=STYLE_CONVERT), self._fix("ABC"))

    def test_spaces_are_deleted(self):
        from bkbook.subbook import STYLE_DELETE

        self.assertEqual(
            self._fix("a b", space=STYLE_DELETE), self._fix("ab")
        )

    def test_long_vowel_becomes_the_preceding_vowel(self):
        from bkbook.subbook import STYLE_CONVERT

        self.assertEqual(
            self._fix("カー", long_vowel=STYLE_CONVERT), self._fix("カア")
        )

    def test_voiced_consonant_is_stripped(self):
        from bkbook.subbook import STYLE_CONVERT

        self.assertEqual(
            self._fix("ガ", voiced_consonant=STYLE_CONVERT), self._fix("カ")
        )

    def test_p_sound_becomes_h_sound(self):
        from bkbook.subbook import STYLE_CONVERT

        self.assertEqual(self._fix("パ", p_sound=STYLE_CONVERT), self._fix("ハ"))

    def test_backward_reverses_whole_characters(self):
        self.assertEqual(self._fix("かなも", backward=True), self._fix("もなか"))

    def test_backward_reverses_full_width_ascii(self):
        self.assertEqual(self._fix("abc", backward=True), self._fix("cba"))

    def test_backward_normalizes_before_reversing(self):
        """正規化の規則は左から右に読むので、逆順にするより先に適用する。

        ``カー`` が ``カア`` になるのは、ー が **直前** のかなの母音を取る
        からである。先に逆順にしてしまうと、取るべき直前の文字がなくなる。
        """
        from bkbook.subbook import STYLE_CONVERT

        self.assertEqual(
            self._fix("カー", backward=True, long_vowel=STYLE_CONVERT),
            self._fix("アカ"),
        )

    def test_backward_latin_reverses_bytes(self):
        from bkbook.subbook import STYLE_ASIS, Search

        index = Search(
            index_id=0x72, start_page=1, end_page=1, space=STYLE_ASIS,
            lower=STYLE_ASIS,
        )
        raw, code = setword.convert_to_latin("abc")
        query = setword.fix_word(index, raw, 1, code, backward=True)
        self.assertEqual(query.canonicalized, b"cba")


class PatternTest(unittest.TestCase):
    def test_a_plain_word_matches_the_start(self):
        self.assertEqual(parse_pattern("ization"), ("ization", False))

    def test_a_trailing_star_also_matches_the_start(self):
        self.assertEqual(parse_pattern("ization*"), ("ization", False))

    def test_a_leading_star_matches_the_end(self):
        self.assertEqual(parse_pattern("*ization"), ("ization", True))

    def test_surrounding_space_is_ignored(self):
        self.assertEqual(parse_pattern("  * ization "), ("ization", True))

    def test_both_ends_at_once_is_refused(self):
        with self.assertRaises(setword.WordError):
            parse_pattern("*ize*")

    def test_a_bare_star_is_not_yet_a_word(self):
        self.assertEqual(parse_pattern("*"), ("", True))
        self.assertEqual(parse_pattern("**"), ("", True))


class LeafPageTest(unittest.TestCase):
    """葉ページの走査。形を正確にするため、ページは手で組み立てる。"""

    PAGE_SIZE = 2048

    def _page(self, page_id, entries):
        """グループ化された葉ページ 1 枚。項目は ``("head"|"member", key, n)``。"""
        body = bytearray()
        for kind, key, number in entries:
            if kind == "head":
                body += bytes([0x80, len(key), 0, 0]) + key
            else:
                body += bytes([0xC0, len(key)]) + key
                # 本文のページ／位置、続いて見出しのページ／位置
                body += number.to_bytes(4, "big") + b"\0\0"
                body += number.to_bytes(4, "big") + b"\0\0"
        header = bytes([page_id, 0]) + len(entries).to_bytes(2, "big")
        page = bytearray(header + bytes(body))
        page += bytes(self.PAGE_SIZE - len(page))
        return bytes(page)

    def _scan(self, pages, word):
        from bkbook.search import _iter_leaf_hits
        from bkbook.setword import Query
        from bkbook.subbook import Search

        class Zio:
            def read_page(self, page):
                return pages[page]

        class Stub:
            zio = Zio()

        query = Query(
            word=word,
            canonicalized=word,
            word_code=setword.WORD_ALPHABET,
            search=Search(index_id=0x90, start_page=1, end_page=len(pages)),
        )
        return list(
            _iter_leaf_hits(Stub(), 1, query, match.match_word, match.match_word)
        )

    def test_a_group_split_across_pages_is_still_matched(self):
        """0x80 のヘッダがページの最後になり、0xc0 の構成要素が次のページから
        始まることがある。

        構成要素は検索語と比較すべき鍵を持たない。持ち越されるのはヘッダの
        判定だけなので、それがページをまたいで生き延びなければならない。
        これを落とすと 広辞苑 の 羅生門河岸 が失われる。収録されていて
        読めるのに見つからない状態だった。グループ化された索引の約 4% が
        同じ目に遭っていた。
        """
        pages = {
            1: self._page(0x90, [("head", b"AB", 0)]),
            2: self._page(0xB0, [("member", b"AB", 7)]),
        }
        hits = self._scan(pages, b"AB")
        self.assertEqual([hit.text.page for hit in hits], [7])

    def test_members_of_a_group_that_did_not_match_stay_unmatched(self):
        # 「AA」 は 「AB」 より前に並ぶので走査は次のページへ続く——そのとき
        # 持ち越すのは鍵だけでなく、ヘッダの **不一致という判定** である。
        pages = {
            1: self._page(0x90, [("head", b"AA", 0)]),
            2: self._page(0xB0, [("member", b"AA", 7)]),
        }
        self.assertEqual(self._scan(pages, b"AB"), [])


class LeafSkipTest(unittest.TestCase):
    """検索が索引を丸ごと歩かずに済むようにする 2 つの近道。

    どちらも答えを変えない。木がいつも語を正しい葉に置いてくれるとは限らず、
    そのずれを埋める走査こそが、1,741 ページを読んで何も見つけずに終わる
    あの走査だから存在する。
    """

    def test_a_word_past_the_last_headword_is_not_looked_for(self):
        from bkbook.search import _beyond_the_index
        from bkbook.setword import Query
        from bkbook.subbook import Search

        page = bytearray(bytes([0xA0, 0]) + (1).to_bytes(2, "big"))
        page += bytes([2]) + b"AB" + bytes(12)
        pages = {9: bytes(page).ljust(2048, b"\0")}

        class Zio:
            def read_page(self, number):
                return pages[number]

        class Stub:
            zio = Zio()

        index = Search(index_id=0x91, start_page=1, end_page=9)
        after = Query(b"ZZ", b"ZZ", setword.WORD_ALPHABET, index)
        before = Query(b"AA", b"AA", setword.WORD_ALPHABET, index)
        self.assertTrue(_beyond_the_index(Stub(), after, match.match_word))
        self.assertFalse(_beyond_the_index(Stub(), before, match.match_word))

    def test_a_kana_index_is_never_skipped(self):
        """並び順と比較関数の順序が一致しない。

        かなの索引は見出し語をひらがなとカタカナで 2 回収めており、比較関数は
        か と カ を等しいと言う——だからページの先頭の鍵は、それより前の
        ページがまだ一致しうるかどうかについて何も語らない。近道は 2 つとも
        使ってはいけない。
        """
        from bkbook.search import _beyond_the_index, _skip_to_page
        from bkbook.setword import Query
        from bkbook.subbook import Search

        class Zio:
            def read_page(self, number):
                raise AssertionError("a kana index must not be peeked at")

        class Stub:
            zio = Zio()

        for index_id in (0x70, 0x90):
            index = Search(index_id=index_id, start_page=1, end_page=900)
            query = Query(b"\x24\x22", b"\x24\x22", setword.WORD_KANA, index)
            self.assertFalse(_beyond_the_index(Stub(), query, match.match_word))
            self.assertEqual(_skip_to_page(Stub(), query, match.match_word, 5), 5)


@unittest.skipUnless(HAS_BOOK, f"no dictionary at {BOOK_PATH}")
class LeafSkipAgreementTest(unittest.TestCase):
    """近道は、全走査とまったく同じ答えを返さねばならない。"""

    @classmethod
    def setUpClass(cls):
        cls.book = Book(BOOK_PATH)
        cls.subbook = cls.book.subbooks[0].load()

    @classmethod
    def tearDownClass(cls):
        cls.book.close()

    def _words(self, index, count=40):
        """実在の鍵。索引全体に散らばった葉から取る。"""
        from bkbook.search import _first_key

        words = []
        span = max(1, (index.end_page - index.start_page) // count)
        for page in range(index.start_page, index.end_page + 1, span):
            key = _first_key(self.subbook, page)
            if not key:
                continue
            try:
                word = decode_jisx0208(key.rstrip(b"\0")).strip()
            except EbError:
                continue
            if word and "\ufffd" not in word:
                words.append(word[:6])
        return words

    def test_the_same_hits_come_back_either_way(self):
        from bkbook import search as search_module

        index = self.subbook.searches["word_asis"]
        words = self._words(index)
        self.assertTrue(words)
        original = search_module._skip_to_page
        try:
            for word in words:
                fast = search_module.search(self.subbook, word, limit=20)
                search_module._skip_to_page = lambda *a: a[-1]
                slow = search_module.search(self.subbook, word, limit=20)
                search_module._skip_to_page = original
                self.assertEqual(fast, slow, word)
        finally:
            search_module._skip_to_page = original


class MatchTest(unittest.TestCase):
    def test_prefix_match_accepts_longer_entry(self):
        self.assertEqual(match.match_word(b"ab", b"abc"), 0)

    def test_prefix_match_reports_direction(self):
        self.assertLess(match.match_word(b"aa", b"ab"), 0)
        self.assertGreater(match.match_word(b"ac", b"ab"), 0)

    def test_exact_match_rejects_longer_entry(self):
        self.assertNotEqual(match.exact_match_word_jis(b"ab", b"abc"), 0)

    def test_exact_match_ignores_nul_padding(self):
        self.assertEqual(match.exact_match_word_jis(b"ab", b"ab\x00\x00"), 0)

    def test_kana_comparators_match_across_hiragana_and_katakana(self):
        # 点のバイトが同じで区が違う場合。ひらがな か とカタカナ カ。
        # どちらの比較関数も一致とみなす。違うのは順序だけ。
        self.assertEqual(match.match_word_kana_single(b"\x24\x2b", b"\x25\x2b"), 0)
        self.assertEqual(match.match_word_kana_group(b"\x24\x2b", b"\x25\x2b"), 0)

    def test_kana_orderings_differ_when_cells_differ(self):
        # か (242b) と ガ (252c) の比較。「single」 は点だけを比べるので
        # か は ガ の 1 つ前に並ぶ。「group」 は 2 バイト全体を比べるので、
        # カタカナの区の分だけ差がずっと大きくなる。
        single = match.match_word_kana_single(b"\x24\x2b", b"\x25\x2c")
        group = match.match_word_kana_group(b"\x24\x2b", b"\x25\x2c")
        self.assertEqual(single, -1)
        self.assertEqual(group, 0x242B - 0x252C)


class BcdTest(unittest.TestCase):
    def test_bcd2(self):
        self.assertEqual(bcd2(b"\x12\x68", 0), 1268)

    def test_bcd4(self):
        self.assertEqual(bcd4(b"\x00\x05\x54\x52", 0), 55452)


class GaijiTest(unittest.TestCase):
    def test_parse_reads_codes_and_replacements(self):
        table = gaiji.parse("# comment\n\nha14c\tə\nzb121  —\n")
        self.assertEqual(table[(True, 0xA14C)], "ə")
        self.assertEqual(table[(False, 0xB121)], "—")

    def test_empty_replacement_means_render_nothing(self):
        self.assertEqual(gaiji.parse("ha15c\n")[(True, 0xA15C)], "")

    def test_bad_code_is_rejected(self):
        with self.assertRaises(ValueError):
            gaiji.parse("xyz\n")

    def test_format_code_round_trips(self):
        self.assertEqual(gaiji.format_code(True, 0xA14C), "ha14c")
        self.assertIn((True, 0xA14C), gaiji.parse(gaiji.format_code(True, 0xA14C)))


class ZioPlainTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "start")
        with open(self.path, "wb") as handle:
            handle.write(bytes(range(256)) * 16)  # 4096 bytes = 2 pages

    def tearDown(self):
        import shutil

        shutil.rmtree(self.directory)

    def test_reads_a_byte_range(self):
        with Zio(self.path) as zio:
            self.assertEqual(zio.code, "plain")
            self.assertEqual(zio.read(10, 4), bytes(range(10, 14)))

    def test_pages_are_one_origin(self):
        with Zio(self.path) as zio:
            self.assertEqual(zio.read_page(1)[:4], bytes(range(4)))
            self.assertEqual(zio.read_page(2), zio.read(2048, 2048))

    def test_reading_past_the_end_is_short(self):
        with Zio(self.path) as zio:
            self.assertEqual(len(zio.read(4090, 100)), 6)

    def test_page_zero_is_rejected(self):
        with Zio(self.path) as zio:
            with self.assertRaises(ValueError):
                zio.read_page(0)


class ZioEbzipTest(unittest.TestCase):
    """小さな ebzip ファイルを手で組み立てて、読み返す。"""

    def setUp(self):
        import tempfile

        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "start.ebz")
        self.plain = bytes(range(256)) * 20  # 5120 バイト、スライス 3 つ

        slices = [self.plain[i : i + 2048] for i in range(0, len(self.plain), 2048)]
        compressed = [zlib.compress(chunk, 9) for chunk in slices]
        # 縮まないスライスはそのまま入る。
        compressed = [
            raw if len(raw) < 2048 else chunk
            for raw, chunk in zip(compressed, slices)
        ]

        # 索引項目の幅は展開後の大きさから決まる。読み出し側と同じ導き方。
        size = len(self.plain)
        index_width = 2 if size < 1 << 16 else 3 if size < 1 << 24 else 4
        offset = 22 + (len(slices) + 1) * index_width
        index = b""
        for chunk in compressed:
            index += offset.to_bytes(index_width, "big")
            offset += len(chunk)
        index += offset.to_bytes(index_width, "big")

        header = (
            b"EBZip"
            + bytes([0x10])
            + b"\x00" * 3
            + len(self.plain).to_bytes(5, "big")
            + zlib.adler32(self.plain).to_bytes(4, "big")
            + (0).to_bytes(4, "big")
        )
        with open(self.path, "wb") as handle:
            handle.write(header + index + b"".join(compressed))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.directory)

    def test_round_trips_the_whole_file(self):
        with Zio(self.path) as zio:
            self.assertEqual(zio.code, "ebzip1")
            self.assertEqual(zio.file_size, len(self.plain))
            self.assertEqual(zio.read(0, len(self.plain)), self.plain)

    def test_reads_across_a_slice_boundary(self):
        with Zio(self.path) as zio:
            self.assertEqual(zio.read(2040, 16), self.plain[2040:2056])

    def test_rejects_a_bad_magic(self):
        broken = os.path.join(self.directory, "broken.ebz")
        with open(self.path, "rb") as source, open(broken, "wb") as target:
            target.write(b"XBZip" + source.read()[5:])
        # マジックがなければ ebzip ではなく無圧縮ファイルとして読まれる。
        with Zio(broken) as zio:
            self.assertEqual(zio.code, "plain")

    def test_rejects_an_unsupported_level(self):
        broken = os.path.join(self.directory, "level.ebz")
        with open(self.path, "rb") as source:
            data = bytearray(source.read())
        data[5] = 0x1F  # モード 1、レベル 15
        with open(broken, "wb") as handle:
            handle.write(bytes(data))
        with self.assertRaises(ZioError):
            Zio(broken)


class NearbyAppendixTest(unittest.TestCase):
    """ディスクの隣にある appendix ディレクトリは、指定しなくても使われる。"""

    def setUp(self):
        import tempfile

        self.directory = tempfile.mkdtemp()
        self.book = os.path.join(self.directory, "plus")
        os.mkdir(self.book)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.directory)

    def test_found_when_it_is_there(self):
        appendix_path = os.path.join(self.directory, "appendix")
        os.mkdir(appendix_path)
        self.assertEqual(cli.nearby_appendix(self.book), appendix_path)

    def test_none_when_it_is_not(self):
        self.assertIsNone(cli.nearby_appendix(self.book))


class PreferredOrderTest(unittest.TestCase):
    """順序ファイル。1 行に辞書名 1 つ、先に読みたいものから。"""

    def setUp(self):
        import tempfile

        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "books.config")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.directory)

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_no_file_is_no_opinion(self):
        self.assertEqual(cli.preferred_order(self.path), [])

    def test_one_name_to_a_line(self):
        self._write("plus\nkojien\n")
        self.assertEqual(cli.preferred_order(self.path), ["plus", "kojien"])

    def test_blanks_and_comments_are_ignored(self):
        self._write("# 英\n\nplus\n\n# 国\nkojien\n")
        self.assertEqual(cli.preferred_order(self.path), ["plus", "kojien"])

    def test_a_comment_may_follow_the_name(self):
        self._write("plus     # リーダーズ＋プラス英和辞典\n")
        self.assertEqual(cli.preferred_order(self.path), ["plus"])

    def test_a_path_is_read_as_the_name_at_the_end_of_it(self):
        self._write("eb/plus\n~/dict/kojien/\n")
        self.assertEqual(cli.preferred_order(self.path), ["plus", "kojien"])


class OrderFileLocationTest(unittest.TestCase):
    """順序ファイルの置き場所。

    利用者ごとの設定なのでリポジトリには追跡させない。``~/.config/bkbook/``
    に置く。
    """

    def setUp(self):
        import tempfile

        self.directory = tempfile.mkdtemp()
        self._saved = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.directory

    def tearDown(self):
        import shutil

        os.environ.pop("XDG_CONFIG_HOME", None)
        if self._saved is not None:
            os.environ["XDG_CONFIG_HOME"] = self._saved
        shutil.rmtree(self.directory)

    def _write_preferred(self, text):
        path = os.path.join(self.directory, "bkbook", "books.config")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_the_config_directory_is_where_it_lives(self):
        self.assertEqual(
            cli.order_file(),
            os.path.join(self.directory, "bkbook", "books.config"),
        )

    def test_xdg_config_home_is_honoured(self):
        self.assertEqual(cli.config_home(), self.directory)

    def test_the_preferred_one_is_read(self):
        self._write_preferred("plus\nkojien\n")
        self.assertEqual(cli.preferred_order(), ["plus", "kojien"])

    def test_no_file_is_no_opinion(self):
        self.assertEqual(cli.preferred_order(), [])


class CategoryGuessTest(unittest.TestCase):
    def test_english_dictionaries(self):
        for title in ("リーダーズ＋プラス英和辞典", "研究社新和英大辞典",
                      "ロングマン現代英英辞典", "American Heritage Dictionary"):
            self.assertEqual(cli.guess_category(title), "英", title)

    def test_encyclopedias(self):
        for title in ("マイペディア", "電子ブック版『日本大百科全書』",
                      "ブリタニカ小項目事典"):
            self.assertEqual(cli.guess_category(title), "百", title)

    def test_anything_else_falls_back_to_japanese(self):
        for title in ("広辞苑第五版", "三省堂「大辞林」", "角川類語新辞典"):
            self.assertEqual(cli.guess_category(title), "国", title)

    def test_a_title_written_full_width_says_the_same_thing(self):
        self.assertEqual(cli.guess_category("Ｒｏｇｅｔ’ｓ"), "英")
        self.assertEqual(cli.guess_category("Roget's"), "英")

    def test_the_categories_are_ordered_english_japanese_encyclopedia(self):
        self.assertEqual(
            sorted(["百", "国", "英"], key=cli.category_order), ["英", "国", "百"]
        )


class ReadingOrderTest(unittest.TestCase):
    """辞書を開く順。誰のヒットが先に来るかを決めるもの。"""

    class _Subbook:
        def __init__(self, **searches):
            from bkbook.subbook import Search

            self.searches = {
                name: Search(index_id=0, start_page=1, end_page=pages)
                for name, pages in searches.items()
            }

    def test_headword_pages_are_counted_and_endword_pages_are_not(self):
        subbook = self._Subbook(word_asis=100, word_kana=50, endword_asis=140)
        self.assertEqual(cli.headword_size(subbook), 150)

    def test_the_largest_dictionary_of_a_kind_comes_first(self):
        small = self._Subbook(word_asis=10)
        large = self._Subbook(word_asis=1000)
        self.assertLess(
            cli.reading_order("英", large), cli.reading_order("英", small)
        )

    def test_the_kind_outranks_the_size(self):
        huge = self._Subbook(word_asis=100000)
        tiny = self._Subbook(word_asis=1)
        self.assertLess(cli.reading_order("英", tiny), cli.reading_order("国", huge))

    def test_the_order_file_outranks_the_size(self):
        tiny = self._Subbook(word_asis=1)
        huge = self._Subbook(word_asis=100000)
        order = ["kotowaza", "kojien"]
        self.assertLess(
            cli.reading_order("国", tiny, "/eb/kotowaza", order),
            cli.reading_order("国", huge, "/eb/kojien", order),
        )

    def test_a_dictionary_the_file_leaves_out_follows_the_ones_it_names(self):
        subbook = self._Subbook(word_asis=100000)
        order = ["kotowaza"]
        self.assertLess(
            cli.reading_order("国", subbook, "/eb/kotowaza", order),
            cli.reading_order("国", subbook, "/eb/kojien", order),
        )

    def test_naming_the_dictionaries_one_by_one_keeps_that_order(self):
        args = argparse.Namespace(books=["plus", "kojien"])
        two = [cli.BookSpec("/a"), cli.BookSpec("/b")]
        self.assertTrue(cli._theirs(args, two))

    def test_a_collection_has_no_order_of_its_own(self):
        args = argparse.Namespace(books=["/dict"])
        many = [cli.BookSpec(f"/dict/{n}") for n in "abc"]
        self.assertFalse(cli._theirs(args, many))
        self.assertFalse(cli._theirs(argparse.Namespace(books=[]), many))


class CategoriseTest(unittest.TestCase):
    """タイトルが何の本か語らないとき、中身を読んで種類を決める。"""

    class _Unreadable:
        """見出し語索引がなく、読んで判断する手も使えないサブブック。"""

        def __init__(self, title):
            self.title = title
            self.searches = {}

        def load(self):
            pass

    def test_a_speaking_title_is_taken_at_its_word(self):
        self.assertEqual(cli.categorise(self._Unreadable("リーダーズ英和")), "英")

    def test_a_mute_title_with_nothing_to_read_stays_japanese(self):
        self.assertEqual(cli.categorise(self._Unreadable("惡魔の辭典")), "国")

    @unittest.skipUnless(HAS_BOOK, f"no dictionary at {BOOK_PATH}")
    def test_a_mute_title_is_settled_by_the_headwords(self):
        # リーダーズ＋プラス はタイトルだけで決まってしまうので隠す。ここで
        # 試したいのは、欧文の見出し語だけで判断が付くかどうかである。
        book = Book(BOOK_PATH)
        try:
            subbook = book.subbooks[0]
            subbook.load()
            subbook.title = "惡魔の辭典"
            self.assertEqual(cli.categorise(subbook), "英")
        finally:
            book.close()


class LayoutTest(unittest.TestCase):
    """和欧混在テキストの、端末上の配置計算。"""

    def test_wide_characters_take_two_columns(self):
        self.assertEqual(tui.display_width("book"), 4)
        self.assertEqual(tui.display_width("辞書"), 4)

    def test_combining_marks_take_none(self):
        self.assertEqual(tui.display_width("ə́"), 1)
        self.assertEqual(len("ə́"), 2)

    def test_truncate_does_not_split_a_wide_character(self):
        self.assertEqual(tui.truncate("辞書ツール", 5), "辞書")
        self.assertEqual(tui.truncate("book", 2), "bo")

    def test_truncate_handles_degenerate_widths(self):
        self.assertEqual(tui.truncate("book", 0), "")
        self.assertEqual(tui.truncate("book", -1), "")

    def test_wrap_never_exceeds_the_width(self):
        text = "1a 本, 書物, 書籍; 《口》 雑誌; 著述, 著作. これは長い日本語の行です。"
        for line in tui.wrap(text, 24):
            self.assertLessEqual(tui.display_width(line), 24)

    def test_wrap_keeps_latin_words_whole(self):
        lines = tui.wrap("read write a book of the hour", 12)
        for line in lines:
            self.assertLessEqual(tui.display_width(line), 12)
        self.assertEqual("".join(lines).replace(" ", ""), "readwriteabookofthehour")

    def test_wrap_preserves_blank_lines(self):
        self.assertEqual(tui.wrap("a\n\nb", 10), ["a", "", "b"])

    def test_wrap_breaks_a_word_longer_than_the_line(self):
        lines = tui.wrap("supercalifragilistic", 8)
        for line in lines:
            self.assertLessEqual(tui.display_width(line), 8)
        self.assertEqual("".join(lines), "supercalifragilistic")


SAMPLE_APP = """\
# コメント
character-code\tjisx0208
stop-code\t0x1f09 0x0001

begin narrow
\trange-start\t0xa121
\trange-end\t0xa123

\t0xa121\t/
\t0xa122\t~
\t0xa123
end

begin wide
\trange-start\t0xb121
\trange-end\t0xb122

\t0xb121\t[P]
\t0xb122\tR,
end
"""


class AppendixTest(unittest.TestCase):
    def test_parses_directives(self):
        parsed = appendix.parse(SAMPLE_APP)
        self.assertEqual(parsed.character_code, "jisx0208")
        self.assertEqual(parsed.stop_code, (0x1F09, 0x0001))

    def test_parses_both_blocks(self):
        parsed = appendix.parse(SAMPLE_APP)
        self.assertEqual(parsed.narrow[0xA121], "/")
        self.assertEqual(parsed.wide[0xB121], "[P]")

    def test_missing_replacement_means_render_nothing(self):
        self.assertEqual(appendix.parse(SAMPLE_APP).narrow[0xA123], "")

    def test_replacement_may_contain_punctuation(self):
        self.assertEqual(appendix.parse(SAMPLE_APP).wide[0xB122], "R,")

    def test_as_gaiji_table_keys_on_width(self):
        table = appendix.parse(SAMPLE_APP).as_gaiji_table()
        self.assertEqual(table[(True, 0xA121)], "/")
        self.assertEqual(table[(False, 0xB121)], "[P]")

    def test_unknown_directive_is_rejected(self):
        with self.assertRaises(appendix.AppendixError):
            appendix.parse("nonsense\tvalue\n")

    def test_character_outside_a_block_is_rejected(self):
        with self.assertRaises(appendix.AppendixError):
            appendix.parse("0xa121\t/\n")

    def test_bad_stop_code_is_rejected(self):
        with self.assertRaises(appendix.AppendixError):
            appendix.parse("stop-code\t0x1f09\n")


@unittest.skipUnless(HAS_BOOK, f"no dictionary at {BOOK_PATH}")
class BookIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = Book(BOOK_PATH)
        cls.subbook = cls.book.subbooks[0]
        cls.subbook.load()

    @classmethod
    def tearDownClass(cls):
        cls.book.close()

    def test_catalog_is_parsed(self):
        self.assertEqual(self.book.disc_code, "eb")
        self.assertTrue(self.subbook.title)
        self.assertTrue(self.subbook.directory_name)

    def test_a_word_index_exists(self):
        self.assertIn("word_asis", self.subbook.searches)
        self.assertIn("text", self.subbook.searches)

    def test_search_finds_hits(self):
        hits = search(self.subbook, "book", limit=5)
        self.assertTrue(hits)
        for hit in hits:
            self.assertIsInstance(hit.text, Position)
            self.assertGreater(hit.text.page, 0)

    def test_headings_start_with_the_search_word(self):
        import unicodedata

        hits = search(self.subbook, "book", limit=5)
        for hit in hits:
            heading = unicodedata.normalize(
                "NFKC", read_heading(self.subbook, hit.heading).text
            )
            self.assertIn("book", heading.lower())

    def test_exact_search_is_narrower_than_prefix_search(self):
        prefix = search(self.subbook, "book", exact=False, limit=100)
        exact = search(self.subbook, "book", exact=True, limit=100)
        self.assertLessEqual(len(exact), len(prefix))

    def test_entry_text_is_read(self):
        hit = search(self.subbook, "book", limit=1)[0]
        result = read_text(self.subbook, hit.text)
        self.assertGreater(len(result.text), 100)
        self.assertIn(result.stop, ("soft", "hard"))

    def test_a_missing_word_yields_no_hits(self):
        self.assertEqual(search(self.subbook, "qqqzzzxxxvvv", limit=5), [])

    def test_a_book_without_an_endword_index_says_so(self):
        if "endword_asis" in self.subbook.searches:
            self.skipTest("this book does have an endword index")
        with self.assertRaises(NoSuchSearchError):
            search(self.subbook, "book", limit=5, backward=True)


def _find_book_with(index_name: str) -> str | None:
    """指定の索引を持つ辞書。設定されているコレクションの中にあれば。

    どのディスクもすべての索引を持っているわけではない——他の結合テストが
    相手にしている リーダーズ＋プラス には endword 索引もかな索引もない
    ——ので、これらのテストは設定されたコレクションの中から自分で相手を探し、
    適当なものが入っていなければスキップする。
    """
    for spec in TEST_COLLECTION:
        try:
            book = Book(spec.path)
        except EbError:
            continue
        try:
            for subbook in book.subbooks:
                subbook.load()
                if index_name in subbook.searches:
                    return spec.path
        except EbError:
            continue
        finally:
            book.close()
    return None


ENDWORD_BOOK = _find_book_with("endword_asis")
KANA_BOOK = _find_book_with("word_kana")


def _published_stop_codes() -> list[tuple[str, int, tuple[int, int]]]:
    """appendix が項目の終わりを宣言しているサブブックをすべて集める。"""
    found = []
    for spec in TEST_COLLECTION:
        if not spec.appendix:
            continue
        try:
            book = Book(spec.path)
        except EbError:
            continue
        try:
            for index, subbook in enumerate(book.subbooks):
                subbook.load()
                published = appendix.for_subbook(subbook, spec.appendix)
                if published.stop_code is not None:
                    found.append((spec.path, index, published.stop_code))
        except EbError:
            continue
        finally:
            book.close()
    return found


APPENDIX_STOP_CODES = _published_stop_codes()


@unittest.skipUnless(ENDWORD_BOOK, "no dictionary here carries an endword index")
class EndwordSearchTest(unittest.TestCase):
    """実物のディスクに対する後方一致検索。

    主張できることは見かけより狭い。endword 索引は見出し語一覧に対する機械的な
    後方一致ではない。どの項目を何の下に収めるかは出版社が決めるので、
    ランダムハウス は ``*ization`` に ``Ju・da・ize`` で答えるし、``-ion`` の
    項目を ``tion`` の下に収める。すべての見出しが検索した文字で終わることを
    確かめるのは、このコードではなく編集方針を試すことになる。このコードが
    責任を負うのは、問い合わせを逆順にして endword 索引にたどり着くところ
    までである。
    """

    @classmethod
    def setUpClass(cls):
        cls.book = Book(ENDWORD_BOOK)
        cls.subbook = next(
            subbook
            for subbook in cls.book.subbooks
            if "endword_asis" in subbook.load().searches
        )

    @classmethod
    def tearDownClass(cls):
        cls.book.close()

    def test_the_endword_index_is_the_one_searched(self):
        from bkbook.search import prepare

        _query, index_name, _comparators = prepare(
            self.subbook, "tion", backward=True
        )
        self.assertTrue(index_name.startswith("endword_"), index_name)

    def test_the_query_reaches_the_index_reversed(self):
        from bkbook.search import prepare

        forward, _name, _comparators = prepare(self.subbook, "tion")
        backward, _name, _comparators = prepare(self.subbook, "tion", backward=True)
        self.assertEqual(
            backward.canonicalized,
            setword._reverse_jisx0208(forward.canonicalized),
        )

    def test_backward_search_finds_entries_forward_search_does_not(self):
        word, is_backward = parse_pattern("*tion")
        self.assertTrue(is_backward)
        hits = search(self.subbook, word, limit=10, backward=True)
        self.assertTrue(hits)
        forward = search(self.subbook, word, limit=10)
        self.assertNotEqual([hit.text for hit in hits], [h.text for h in forward])

    def test_an_ending_nothing_is_filed_under_yields_no_hits(self):
        self.assertEqual(search(self.subbook, "qqzzxxvv", backward=True), [])


@unittest.skipUnless(KANA_BOOK, "no dictionary here carries a kana index")
class KanaIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = Book(KANA_BOOK)
        cls.subbook = next(
            subbook
            for subbook in cls.book.subbooks
            if "word_kana" in subbook.load().searches
        )

    @classmethod
    def tearDownClass(cls):
        cls.book.close()

    def test_the_hiragana_and_katakana_copies_collapse(self):
        """かなの索引は見出し語を 2 回収める。一覧に出すのは 1 行でよい。

        これがないと 広辞苑 と 大辞林 はかなの問い合わせすべてに、同じ項目を
        2 行ずつ並べて答える。TUI では一覧の半分がそれで潰れる。
        """
        hits = search(self.subbook, "こころ", limit=20)
        self.assertTrue(hits)
        positions = [hit.text for hit in hits]
        self.assertEqual(len(positions), len(set(positions)))


class KeywordLeafTest(unittest.TestCase):
    """keyword 索引の葉の形。ページは手で組み立てる。

    語索引と違い、ここの項目はどれも自分の大きさを宣言しない。構成要素は
    つねに 7 バイト、グループヘッダはつねに 12 バイト＋鍵である。
    """

    PAGE_SIZE = 2048

    @staticmethod
    def _key(word):
        """``word`` で引いたとき、比較の相手になるバイト列。"""
        from bkbook.jacode import CHARCODE_JISX0208
        from bkbook.setword import convert_to_jisx0208, fix_word
        from bkbook.subbook import Search

        raw, word_code = convert_to_jisx0208(word)
        query = fix_word(
            Search(index_id=0x80, start_page=1, end_page=9),
            raw,
            CHARCODE_JISX0208,
            word_code,
        )
        return bytes(query.word)

    def _page(self, page_id, entries):
        body = bytearray()
        for entry in entries:
            kind = entry[0]
            if kind == "single":
                _, key, text, heading = entry
                body += bytes([0x00, len(key)]) + key
                body += text.to_bytes(4, "big") + b"\0\0"
                body += heading.to_bytes(4, "big") + b"\0\0"
            elif kind == "head":
                _, key, count, heading = entry
                body += bytes([0x80, len(key)]) + count.to_bytes(4, "big") + key
                body += heading.to_bytes(4, "big") + b"\0\0"
            else:
                _, text = entry
                body += bytes([0xC0]) + text.to_bytes(4, "big") + b"\0\0"
        page = bytearray(bytes([page_id, 0]) + len(entries).to_bytes(2, "big") + body)
        page += bytes(self.PAGE_SIZE - len(page))
        return bytes(page)

    def _hits(self, pages, word):
        from bkbook import search as search_module
        from bkbook import text as text_module
        from bkbook.subbook import Search

        class Zio:
            def read_page(self, page):
                return pages[page]

        from bkbook.jacode import CHARCODE_JISX0208

        class Book:
            character_code = CHARCODE_JISX0208
            disc_code = "epwing"

        class Stub:
            zio = Zio()
            book = Book()
            title = "stub"
            searches = {"keyword": Search(index_id=0x80, start_page=1, end_page=9)}

            def load(self):
                return self

        # 各構成要素の見出しは、1 つ前の見出しを読み終えたところにある。
        # だから走査は進みながら見出しを読む。スタブは位置を進めるだけ。
        original = text_module.read_heading
        text_module.read_heading = lambda subbook, position, **kw: _Heading(
            Position(position.page, position.offset + 10)
        )
        try:
            return list(search_module.iter_keyword_hits(Stub(), word))
        finally:
            text_module.read_heading = original

    def test_a_single_carries_both_its_positions(self):
        pages = {1: self._page(0xB2, [("single", self._key("AB"), 5, 6)])}
        (hit,) = self._hits(pages, "AB")
        self.assertEqual((hit.text.page, hit.heading.page), (5, 6))

    def test_members_take_their_headings_in_turn(self):
        """グループが持つ見出し位置は 1 つで、構成要素ごとではない。

        本は構成要素の見出しをそこから端から端まで並べているので、2 番目の
        構成要素の見出しは、1 番目を読み終えたところにある。
        """
        pages = {
            1: self._page(
                0xB2,
                [("head", self._key("AB"), 2, 40), ("member", 7), ("member", 8)],
            )
        }
        hits = self._hits(pages, "AB")
        self.assertEqual([hit.text.page for hit in hits], [7, 8])
        self.assertEqual([hit.heading.offset for hit in hits], [0, 10])

    def test_a_group_that_did_not_match_yields_nothing(self):
        pages = {
            1: self._page(0xB2, [("head", self._key("AA"), 1, 40), ("member", 7)]),
        }
        self.assertEqual(self._hits(pages, "AB"), [])

    def test_a_group_split_across_pages_keeps_its_heading_stream(self):
        pages = {
            1: self._page(0x92, [("head", self._key("AB"), 2, 40), ("member", 7)]),
            2: self._page(0xB2, [("member", 8)]),
        }
        hits = self._hits(pages, "AB")
        self.assertEqual([hit.text.page for hit in hits], [7, 8])
        self.assertEqual([hit.heading.offset for hit in hits], [0, 10])


class _Heading:
    def __init__(self, next_position):
        self.next_position = next_position
        self.text = ""


KEYWORD_BOOK = _find_book_with("keyword")


@unittest.skipUnless(KEYWORD_BOOK, "no dictionary here carries a keyword index")
class KeywordSearchTest(unittest.TestCase):
    """実物のディスクに対する keyword 検索。

    keyword 索引は、見出し語索引には答えられない問いに答える。ある語を
    **使っている** 項目はどれか、という問いである。研究社新和英大辞典 では
    それは訳文中の英単語のことなので、「black sheep」 から 恥晒し に届く。
    この項目はどちらの語の下にも収められていない。
    """

    @classmethod
    def setUpClass(cls):
        from bkbook.search import search_keyword

        cls.search_keyword = staticmethod(search_keyword)
        cls.book = Book(KEYWORD_BOOK)
        cls.subbook = next(
            subbook
            for subbook in cls.book.subbooks
            if "keyword" in subbook.load().searches
        )
        cls.words = cls._sample_words(cls.subbook)

    @classmethod
    def tearDownClass(cls):
        cls.book.close()

    @staticmethod
    def _sample_words(subbook):
        """そのディスクが実際に収めている keyword を 2 つ、索引から読み出す。

        keyword 索引がどんな語を持つかは出版社の裁量である——英語の訳語を
        索引する本もあれば、ことわざのかなを索引する本もある——ので、
        このテストは決め打ちせず、索引から問い合わせを取ってくる。
        """
        from bkbook.jacode import decode_jisx0208
        from bkbook.search import _first_leaf

        page = _first_leaf(subbook, subbook.searches["keyword"].start_page)
        found = []
        while len(found) < 8:
            data = subbook.zio.read_page(page)
            count = int.from_bytes(data[2:4], "big")
            offset = 4
            for _entry in range(count):
                group_id = data[offset]
                if group_id == 0xC0:
                    offset += 7
                    continue
                length = data[offset + 1]
                start = offset + (6 if group_id == 0x80 else 2)
                key = data[start : start + length]
                if len(key) >= 4:
                    found.append(decode_jisx0208(key))
                offset = start + length + (6 if group_id == 0x80 else 12)
            if data[0] & 0x20:
                break
            page += 1
        return found

    def test_a_word_out_of_the_index_finds_its_entries(self):
        self.assertTrue(self.words, "the index has no keys to test with")
        hits = self.search_keyword(self.subbook, [self.words[0]], limit=5)
        self.assertTrue(hits)
        for hit in hits:
            self.assertGreater(read_text(self.subbook, hit.text).text.strip(), "")

    def test_two_words_narrow_the_first_word_s_list(self):
        first = self.search_keyword(self.subbook, [self.words[0]])
        both = self.search_keyword(self.subbook, self.words[:2])
        self.assertLessEqual(len(both), len(first))
        positions = {hit.text for hit in first}
        for hit in both:
            self.assertIn(hit.text, positions)

    def test_a_limit_stops_the_walk_rather_than_trimming_it(self):
        """見出しは走査しながら構成要素ごとに 1 つずつ読まれる。

        「A」 のようにありふれた keyword には 4 万件あり、5 件を見せるために
        全部読んでいたときは 10 秒かかっていた。
        """
        from bkbook import text as text_module

        counted = {"n": 0}
        original = text_module.read_heading

        def counting(*args, **kwargs):
            counted["n"] += 1
            return original(*args, **kwargs)

        text_module.read_heading = counting
        try:
            hits = self.search_keyword(self.subbook, [self.words[0]], limit=2)
        finally:
            text_module.read_heading = original
        self.assertLessEqual(len(hits), 2)
        self.assertLessEqual(counted["n"], 4)

    def test_the_order_does_not_depend_on_which_word_is_typed_first(self):
        forwards = self.search_keyword(self.subbook, self.words[:2], limit=10)
        backwards = self.search_keyword(
            self.subbook, list(reversed(self.words[:2])), limit=10
        )
        self.assertEqual(
            [hit.text for hit in forwards], [hit.text for hit in backwards]
        )

    def test_a_word_the_index_does_not_carry_yields_nothing(self):
        self.assertEqual(
            self.search_keyword(self.subbook, ["qqzzxxvvqqzz"]), []
        )

    def test_an_empty_query_is_not_a_search(self):
        self.assertEqual(self.search_keyword(self.subbook, []), [])
        self.assertEqual(self.search_keyword(self.subbook, ["  "]), [])

    def test_a_book_without_the_index_says_so(self):
        other = next(
            (
                subbook
                for subbook in Book(BOOK_PATH).subbooks
                if "keyword" not in subbook.load().searches
            ),
            None,
        )
        if other is None:
            self.skipTest("every subbook here has a keyword index")
        with self.assertRaises(NoSuchSearchError):
            self.search_keyword(other, ["word"])


class MultiTableTest(unittest.TestCase):
    """multi 検索が自前で持っている、欄の小さな表を読む。"""

    def _subbook(self, page_bytes):
        from bkbook.subbook import Search, SubBook

        class Zio:
            def read_page(self, page):
                return page_bytes

        class Book:
            character_code = 2
            disc_code = "epwing"

        subbook = SubBook(Book(), 0, "stub", "stub", 1)
        subbook._zio = Zio()
        subbook._loaded = True
        subbook.multi_searches = [Search(index_id=0xFF, start_page=1, end_page=1)]
        return subbook

    def _table(self, fields):
        """``fields`` は ``(ラベル, [(index_id, 開始, ブロック数), …])``。"""
        page = bytearray(len(fields).to_bytes(2, "big") + bytes(14))
        for label, indexes in fields:
            encoded = label.encode("euc_jp")
            encoded = bytes(byte & 0x7F for byte in encoded)
            page += bytes([len(indexes), 0]) + encoded.ljust(30, b"\0")
            for index_id, start, blocks in indexes:
                page += bytes([index_id, 0])
                page += start.to_bytes(4, "big") + blocks.to_bytes(4, "big")
                page += bytes([1, 0, 0, 0, 0, 0])
        return bytes(page).ljust(2048, b"\0")

    def test_fields_are_read_with_their_labels(self):
        subbook = self._subbook(
            self._table(
                [("事項", [(0xA1, 100, 5), (0x0D, 200, 5)]), ("作者", [(0xA1, 300, 5)])]
            )
        )
        (multi,) = subbook.multis
        self.assertEqual([entry.label for entry in multi.entries], ["事項", "作者"])
        self.assertEqual(multi.label, "事項 / 作者")

    def test_the_word_index_is_told_from_the_candidate_pages(self):
        subbook = self._subbook(
            self._table([("事項", [(0xA1, 100, 5), (0x0D, 200, 5), (0x01, 300, 5)])])
        )
        (entry,) = subbook.multis[0].entries
        self.assertEqual(entry.index.start_page, 100)
        self.assertEqual([c.start_page for c in entry.candidates], [200, 300])

    def test_a_kana_field_folds_katakana_like_a_kana_index(self):
        from bkbook.subbook import STYLE_CONVERT

        subbook = self._subbook(self._table([("事項", [(0xA1, 100, 5)])]))
        (entry,) = subbook.multis[0].entries
        self.assertEqual(entry.index.katakana, STYLE_CONVERT)


class MultiLeafTest(unittest.TestCase):
    """multi 検索の索引は、グループの書き方が違う。

    構成要素の数が 2 バイトではなく 4 バイトで、構成要素は鍵を繰り返さない
    ——どれも同じ語なので、繰り返すものがない。語索引の規則で読むと、項目の
    外へまっすぐ出ていってしまう。
    """

    def _page(self, key, members):
        body = bytearray([0x80, len(key)]) + len(members).to_bytes(4, "big") + key
        for text in members:
            body += bytes([0xC0])
            body += text.to_bytes(4, "big") + b"\0\0"
            body += (text + 1000).to_bytes(4, "big") + b"\0\0"
        page = bytearray(bytes([0xB0, 0]) + (len(members) + 1).to_bytes(2, "big"))
        page += body
        return bytes(page).ljust(2048, b"\0")

    def test_members_without_keys_are_all_hits_of_the_group(self):
        from bkbook.search import MULTI_LAYOUT, _iter_leaf_hits
        from bkbook.setword import Query
        from bkbook.subbook import Search

        key = KeywordLeafTest._key("AB")
        pages = {1: self._page(key, [7, 8, 9])}

        class Zio:
            def read_page(self, page):
                return pages[page]

        class Stub:
            zio = Zio()

        query = Query(
            word=key,
            canonicalized=key,
            word_code=setword.WORD_ALPHABET,
            search=Search(index_id=0xA1, start_page=1, end_page=1),
        )
        hits = list(
            _iter_leaf_hits(
                Stub(), 1, query, match.match_word, match.match_word, MULTI_LAYOUT
            )
        )
        self.assertEqual([hit.text.page for hit in hits], [7, 8, 9])
        self.assertEqual([hit.heading.page for hit in hits], [1007, 1008, 1009])


def _find_multi_book():
    """multi 検索を持つ辞書。設定されているコレクションの中にあれば。

    欄が値のメニューを持つものを優先する。持っているのは一部だけで、しかも
    メニューはこの機能の半分を占めるうえ、現物がなければ試しようがない。
    """
    fallback = None
    for spec in TEST_COLLECTION:
        try:
            book = Book(spec.path)
        except EbError:
            continue
        try:
            for index, subbook in enumerate(book.subbooks):
                if not subbook.load().multi_searches:
                    continue
                found = (spec.path, index)
                if any(entry.menu for entry in subbook.multis[0].entries):
                    return found
                fallback = fallback or found
        except EbError:
            continue
        finally:
            book.close()
    return fallback


MULTI_BOOK = _find_multi_book()


@unittest.skipUnless(MULTI_BOOK, "no dictionary here carries a multi search")
class MultiSearchTest(unittest.TestCase):
    """実物のディスクに対する multi 検索。

    欄が **何であるか** は出版社の裁量である——ことわざ は 事項 と 作者 を、
    日本大百科 は 索引語 を尋ねる——ので、このテストは本自身の索引から語を
    読み出して、それで問い合わせる。
    """

    @classmethod
    def setUpClass(cls):
        from bkbook.search import search_multi

        cls.search_multi = staticmethod(search_multi)
        path, index = MULTI_BOOK
        cls.book = Book(path)
        cls.subbook = cls.book.subbooks[index].load()
        cls.multi = cls.subbook.multis[0]

    @classmethod
    def tearDownClass(cls):
        cls.book.close()

    def _first_key(self, entry):
        """その欄の索引が実際に収めている語。先頭の葉から取る。"""
        from bkbook.jacode import decode_jisx0208
        from bkbook.search import _first_leaf

        data = self.subbook.zio.read_page(
            _first_leaf(self.subbook, entry.index.start_page)
        )
        offset = 4
        if not data[0] & 0x10:  # グループなし。長さのバイト、続いて鍵
            return decode_jisx0208(data[offset + 1 : offset + 1 + data[offset]])
        group_id = data[offset]
        length = data[offset + 1]
        start = offset + (6 if group_id == 0x80 else 2)
        return decode_jisx0208(data[start : start + length])

    def test_the_table_names_its_fields(self):
        self.assertTrue(self.multi.entries)
        self.assertTrue(any(entry.label for entry in self.multi.entries))
        self.assertTrue(any(entry.index for entry in self.multi.entries))

    def test_a_word_from_the_field_s_own_index_finds_entries(self):
        entry = next(e for e in self.multi.entries if e.index)
        field = self.multi.entries.index(entry)
        words = [""] * len(self.multi.entries)
        words[field] = self._first_key(entry)
        hits = self.search_multi(self.subbook, 0, words, limit=5)
        self.assertTrue(hits, words)
        for hit in hits:
            self.assertGreater(len(read_text(self.subbook, hit.text).text), 0)

    def test_leaving_every_field_empty_is_not_a_search(self):
        blank = [""] * len(self.multi.entries)
        self.assertEqual(self.search_multi(self.subbook, 0, blank), [])

    def test_a_field_with_a_menu_offers_words_at_the_bottom_of_it(self):
        """メニューは木であり、その葉がその欄に指定できる語である。

        ことわざ の 事項 はまず 16 の見出しに分かれ、その各々がさらに 10 ほど
        に分かれ、そこでようやく 人生 や 一生 に至る。この欄が実際に答える語
        である。行き先のない行がそれにあたる。
        """
        entry = next((e for e in self.multi.entries if e.menu), None)
        if entry is None:
            self.skipTest("this book's fields offer no menu")
        seen = [Position(entry.menu.start_page, 1)]
        for _depth in range(4):
            result = read_text(self.subbook, seen[-1])
            self.assertTrue(result.candidates)
            deeper = [c for c in result.candidates if c.position]
            if not deeper:
                break
            seen.append(deeper[0].position)
        else:
            self.fail("the menu never reached a word")
        words = [c.text_of(result.text) for c in result.candidates]
        self.assertTrue(all(word.strip() for word in words))
        field = self.multi.entries.index(entry)
        query = [""] * len(self.multi.entries)
        query[field] = words[0]
        self.assertTrue(self.search_multi(self.subbook, 0, query, limit=1), query)

    def test_asking_for_a_multi_search_the_book_lacks_says_so(self):
        with self.assertRaises(NoSuchSearchError):
            self.search_multi(self.subbook, 99, ["word"])


class BrowserKeywordModeTest(unittest.TestCase):
    """^K は同じ問い合わせを、見出し語ではなく keyword 索引に投げる。"""

    def _browser(self, keyword_hits, word_hits):
        from bkbook.search import Hit, Position

        source = tui.Source(label="book", subbook="book", title="book")
        browser = tui.Browser(None, [source], lambda text: text)

        def fake(kind, hits):
            def call(subbook, *args, **kwargs):
                if hits is None:
                    raise NoSuchSearchError(f"no {kind} index")
                return [Hit(Position(1, n), Position(1, n)) for n in range(hits)]

            return call

        browser._restore = (tui.search, tui.search_keyword)
        tui.search = fake("word", word_hits)
        tui.search_keyword = fake("keyword", keyword_hits)
        return browser

    def _finish(self, browser):
        tui.search, tui.search_keyword = browser._restore

    def test_keyword_mode_uses_the_keyword_index(self):
        browser = self._browser(keyword_hits=3, word_hits=9)
        try:
            browser.keyword = True
            browser.query = "black sheep"
            browser.refresh_results()
            self.assertEqual(len(browser.entries), 3)
        finally:
            self._finish(browser)

    def test_a_book_without_a_keyword_index_just_adds_nothing(self):
        browser = self._browser(keyword_hits=None, word_hits=9)
        try:
            browser.keyword = True
            browser.query = "black"
            browser.refresh_results()
            self.assertEqual(browser.entries, [])
            self.assertEqual(browser.status, "")
        finally:
            self._finish(browser)

    def test_leaving_keyword_mode_searches_headwords_again(self):
        browser = self._browser(keyword_hits=3, word_hits=9)
        try:
            browser.query = "black"
            browser.refresh_results()
            self.assertEqual(len(browser.entries), 9)
        finally:
            self._finish(browser)


class InterleaveTest(unittest.TestCase):
    """複数の辞書のヒットを 1 つの一覧に織り込む方法。"""

    def _order(self, counts, titles=None):
        from bkbook.search import Hit, Position

        titles = titles or [f"book{i}" for i in range(len(counts))]
        hits = {
            title: [
                Hit(Position(index, rank), Position(index, rank))
                for rank in range(count)
            ]
            for index, (title, count) in enumerate(zip(titles, counts))
        }
        sources = [
            tui.Source(label=title, subbook=title, title=title) for title in titles
        ]

        original = tui.search
        tui.search = lambda subbook, *a, **k: hits[subbook]
        try:
            browser = tui.Browser(None, sources, lambda text: text)
            browser.query = "word"
            browser.refresh_results()
        finally:
            tui.search = original
        return [
            (entry.source.title, entry.hit.text.offset) for entry in browser.entries
        ]

    def test_one_hit_from_each_dictionary_in_turn(self):
        self.assertEqual(
            self._order([2, 2]),
            [("book0", 0), ("book1", 0), ("book0", 1), ("book1", 1)],
        )

    def test_a_dictionary_that_runs_out_is_skipped(self):
        self.assertEqual(
            self._order([1, 3]),
            [("book0", 0), ("book1", 0), ("book1", 1), ("book1", 2)],
        )

    def test_a_paired_dictionary_takes_two_at_a_time(self):
        paired = sorted(tui.PAIRED_TITLES)[0]
        self.assertEqual(
            self._order([3, 3], titles=[paired, "other"]),
            [
                (paired, 0), (paired, 1), ("other", 0),
                (paired, 2), ("other", 1),
                ("other", 2),
            ],
        )


class StopCodeTest(unittest.TestCase):
    """形の分かっている本を組み立てて、項目の終わりを割り出す。

    合成したディスクは固定長の索引項目からなる葉ページ 1 枚と、実物と同じ
    配置の本文ファイルを持つ。どの項目も字下げと keyword で始まるので、
    どちらのエスケープでも本文は区切れてしまい、読んでみるまで区別が付かない。
    """

    PAGE_SIZE = 2048
    INDENT = b"\x1f\x09\x00\x01"
    KEYWORD = b"\x1f\x41\x01\x00"
    HEADWORD = b"\x24\x22" * 3 + b"\x1f\x61"
    BODY = b"\x24\x24" * 24

    def _disc(self, entries, interruption=b""):
        """``entries`` 件分の 1 ページ索引と、それが指す本文。

        ``interruption`` は 4 項目に 1 つの割合で本文の途中に挟み込まれる。
        字下げを渡せば、日本大百科全書 が図版のキャプションでそうしているように、
        開始用のエスケープを記事の途中でも使う辞書を作れる。
        """
        text = bytearray()
        starts = []
        for number in range(entries):
            starts.append(len(text))
            text += self.INDENT + self.KEYWORD + self.HEADWORD
            text += self.BODY
            if interruption and number % 4 == 0:
                text += interruption + b"\x24\x26" * 8
            text += self.BODY

        page = bytearray([0xA0, 4]) + entries.to_bytes(2, "big")
        for number, start in enumerate(starts):
            location = start + self.PAGE_SIZE  # 本文は 2 ページ目から
            page += number.to_bytes(4, "big")  # 見出し語。4 バイト分
            page += (location // self.PAGE_SIZE + 1).to_bytes(4, "big")
            page += (location % self.PAGE_SIZE).to_bytes(2, "big")
            page += (1).to_bytes(4, "big") + (0).to_bytes(2, "big")
        page += bytes(self.PAGE_SIZE - len(page))

        return bytes(page) + bytes(text)

    def _subbook(self, data):
        from bkbook.subbook import Search

        page_size = self.PAGE_SIZE

        class Zio:
            path = "<memory>"

            def read(self, location, length):
                return data[location : location + length]

            def read_page(self, page, count=1):
                start = (page - 1) * page_size
                return data[start : start + count * page_size]

        class StubBook:
            disc_code = "eb"
            character_code = 2
            path = "<memory>"

        class Stub:
            book = StubBook()
            title = "test"
            zio = Zio()
            searches = {"word_asis": Search(index_id=0x91, start_page=1, end_page=1)}

            def load(self):
                return self

        return Stub()

    def test_every_entry_is_enumerated(self):
        from bkbook.search import iter_index

        subbook = self._subbook(self._disc(60))
        self.assertEqual(len(list(iter_index(subbook, "word_asis"))), 60)

    def test_the_escape_nearest_the_boundary_wins_a_tie(self):
        # 字下げも keyword もすべての項目を開いており、どちらも他の場所には
        # 現れないので、どちらでも本は正しく読める。appendix が挙げているのは
        # 前のほうであり、ここでもそう振る舞う。
        subbook = self._subbook(self._disc(60))
        self.assertEqual(stopcode.infer(subbook, trials=20), (0x1F09, 0x0001))

    def test_an_escape_used_inside_an_article_loses_to_one_that_is_not(self):
        subbook = self._subbook(self._disc(60, interruption=self.INDENT))
        self.assertEqual(stopcode.infer(subbook, trials=20), (0x1F41, 0x0100))

    def test_the_candidates_are_the_escapes_that_open_every_entry(self):
        subbook = self._subbook(self._disc(60))
        found = {
            (candidate.code, candidate.argument, candidate.offset)
            for candidate in stopcode.report(subbook, trials=20)
        }
        self.assertEqual(found, {(0x1F09, 0x0001, 0), (0x1F41, 0x0100, 4)})

    def test_a_book_with_no_headword_index_yields_nothing(self):
        subbook = self._subbook(self._disc(60))
        subbook.searches = {}
        self.assertIsNone(stopcode.infer(subbook))


@unittest.skipUnless(APPENDIX_STOP_CODES, "no appendix here declares a stop code")
class StopCodeAgreementTest(unittest.TestCase):
    """推定した stop code は、照合できる公開値すべてと一致しなければならない。

    appendix ファイルは、それが覆うディスクについての答え合わせである。だから
    食い違えば推定のほうが誤っている——これがあるから、appendix を読むのを
    やめても安全だと言える。
    """

    def test_inference_agrees_with_every_published_stop_code(self):
        disagreements = []
        for path, index, published in APPENDIX_STOP_CODES:
            book = Book(path)
            try:
                subbook = book.subbooks[index]
                subbook.load()
                inferred = stopcode.infer(subbook)
                if inferred != published:
                    disagreements.append(
                        f"{subbook.title}: inferred {inferred}, appendix says {published}"
                    )
            finally:
                book.close()
        self.assertEqual(disagreements, [])


class AmbiguousWidthTest(unittest.TestCase):
    """外字の置き換えに、端末上の幅が曖昧な記号を使ってはならない。

    東アジア曖昧幅の文字——① Ⓒ ▶ → ↔——は端末によって 1 桁にも 2 桁にも
    なる。与えられた 1 桁に全角で描くフォントは、隣の文字の上に重ねて
    書いてしまう。だから対応表は代わりに (1) や (C) と書く。

    文字（letter）は例外である。取り替えるものがないからだ。ə も ð も á も
    発音そのものであり、それを #e や dh と綴るのは、この対応表がまさに
    改善するために存在する ASCII の代用表記である。
    """

    def test_no_built_in_replacement_is_an_ambiguous_symbol(self):
        import unicodedata

        offenders = []
        for title, table in gaiji.BUILTIN.items():
            for value in table.values():
                for character in value or "":
                    if unicodedata.combining(character):
                        continue
                    if unicodedata.east_asian_width(character) != "A":
                        continue
                    if unicodedata.category(character).startswith("L"):
                        continue
                    offenders.append(f"{title}: {character!r} in {value!r}")
        self.assertEqual(offenders, [])


class StrayByteTest(unittest.TestCase):
    """文字の先頭になりえないバイトは、読み出しの位相をずらす。

    ロングマン は banger、beam、get の途中に 0x00 を紛れ込ませている。これに
    2 バイト使うと以降の 2 バイト対が 1 つずつずれ、項目は自分の終わりを
    読み抜けて次の項目まで文字化けしたまま進み、0x1f がたまたま偶数バイト目に
    来るまで止まらない。
    """

    #: 項目を開くエスケープ。これはその項目の stop code でもある。
    START = b"\x1f\x09\x00\x01\x1f\x41\x01\x60"

    def _read(self, text):
        class Zio:
            path = "<memory>"

            def read(self, location, length):
                return text[location : location + length]

        class StubBook:
            disc_code = "eb"
            character_code = 2
            path = "<memory>"

        class Stub:
            book = StubBook()
            title = "test"
            zio = Zio()
            searches = {}

            def load(self):
                return self

        return read_text(Stub(), Position(1, 0), stop_code=(0x1F09, 0x0001))

    def test_an_entry_without_one_reads_to_its_end(self):
        result = self._read(self.START + b"\x23\x61\x23\x62" + self.START)
        self.assertEqual(result.text, "ａｂ")
        self.assertEqual(result.stop, "soft")

    def test_a_stray_byte_does_not_carry_the_next_one_off_with_it(self):
        result = self._read(
            self.START + b"\x23\x61\x23\x62" + b"\x00" + b"\x23\x63\x23\x64"
            + self.START
        )
        self.assertEqual(result.text, "ａｂｃｄ")
        self.assertEqual(result.stop, "soft")


class CandidateTest(unittest.TestCase):
    """メニューの行。参照ではなく候補である。

    エスケープの対は 0x1f43 … 0x1f63 で、閉じるほうに続く位置は 2 進数では
    なく BCD である。位置が 0:0 の行は、後続の行にかかる見出しである——
    開く先を持たない 「共通事項分類」 など。
    """

    START = b"\x1f\x02"

    def _read(self, text):
        class Zio:
            path = "<memory>"

            def read(self, location, length):
                return text[location : location + length]

        class StubBook:
            disc_code = "epwing"
            character_code = 2
            path = "<memory>"

        class Stub:
            book = StubBook()
            title = "test"
            zio = Zio()
            searches = {}

            def load(self):
                return self

        return read_text(Stub(), Position(1, 0), stop_code=(0x1F09, 0x0001))

    def _candidate(self, label, page_bcd, offset_bcd):
        return (
            b"\x1f\x43"
            + label
            + b"\x1f\x63"
            + bytes.fromhex(page_bcd)
            + bytes.fromhex(offset_bcd)
            + b"\x00\x00"
        )

    def test_a_candidate_says_where_it_goes(self):
        result = self._read(
            self.START + self._candidate(b"\x23\x61", "00026475", "0000") + b"\x1f\x02"
        )
        (candidate,) = result.candidates
        self.assertEqual(candidate.text_of(result.text), "ａ")
        self.assertEqual((candidate.position.page, candidate.position.offset), (26475, 0))

    def test_a_candidate_that_goes_nowhere_is_a_label(self):
        result = self._read(
            self.START + self._candidate(b"\x23\x61", "00000000", "0000") + b"\x1f\x02"
        )
        (candidate,) = result.candidates
        self.assertIsNone(candidate.position)


@unittest.skipUnless(_find_book_with("menu"), "no dictionary here carries a menu")
class MenuTest(unittest.TestCase):
    """ディスクが持っている目次。実物から読み出す。"""

    @classmethod
    def setUpClass(cls):
        cls.book = Book(_find_book_with("menu"))
        cls.subbook = next(
            subbook
            for subbook in cls.book.subbooks
            if "menu" in subbook.load().searches
        )

    @classmethod
    def tearDownClass(cls):
        cls.book.close()

    def test_the_menu_is_a_list_of_places_to_go(self):
        start = Position(self.subbook.searches["menu"].start_page, 1)
        result = read_text(self.subbook, start)
        self.assertTrue(result.candidates)
        for candidate in result.candidates:
            self.assertTrue(candidate.text_of(result.text).strip())

    def test_a_menu_line_leads_somewhere_readable(self):
        start = Position(self.subbook.searches["menu"].start_page, 1)
        result = read_text(self.subbook, start)
        followable = [c for c in result.candidates if c.position]
        self.assertTrue(followable)
        text = read_text(self.subbook, followable[0].position).text
        self.assertTrue(text.strip())


class ReadableTextTest(unittest.TestCase):
    """一部の EPWING 変換が、本文に文字として残していく印。"""

    def test_an_inline_sound_marker_goes(self):
        self.assertEqual(
            cli._readable("ｍｕ・ｓｉｃ <sound=054d038c:06e8>"), "mu・sic"
        )

    def test_an_image_marker_takes_its_line_with_it(self):
        self.assertEqual(
            cli._readable("ハチの総称.\n<image=01B89724:0000510C>\n［1530］"),
            "ハチの総称.\n[1530]",
        )

    def test_a_pitch_rise_overlines_what_follows(self):
        # 飴 は低高。線は ┏ のところから始まる。
        self.assertEqual(cli._readable("a┏me"), "am̅e̅")

    def test_a_pitch_fall_overlines_what_precedes(self):
        # 雨 は高低。この ┓ の前には、範囲を開く ┏ がない。
        self.assertEqual(cli._readable("a┓me"), "a̅me")

    def test_a_span_between_the_two_marks(self):
        self.assertEqual(cli._readable("Ni┏ho┓n"), "Nih̅o̅n")

    def test_the_overline_does_not_cross_a_word_boundary(self):
        self.assertEqual(cli._readable("ame a┓me"), "ame a̅me")

    def test_text_without_accent_marks_is_untouched(self):
        self.assertEqual(cli._readable("ｈａｓｈｉ"), "hashi")

    def test_enclosed_numbers_survive(self):
        # NFKC はこれらを裸の 1 と ア に潰してしまう。
        self.assertEqual(cli._readable("①㋐"), "①㋐")


if __name__ == "__main__":
    unittest.main()
