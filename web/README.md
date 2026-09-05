# bkbook の JavaScript 版

`bkbook/` の Python を JavaScript に移植したもの。辞書データはサーバーに
そのまま置き、ブラウザが HTTP の Range リクエストで必要なページだけを読む。
静的ファイルだけで動き、CGI もビルドも要らない。

`index.html` `app.js` `style.css` が UI、`bkbook/` がライブラリ。

## 構成

| ファイル | Python 版 | 中身 |
|---|---|---|
| `bkbook/fs.js` | os / open | ファイルの在り処の抽象化。Node のローカルファイルと、HTTP Range＋ブロックキャッシュ |
| `bkbook/zio.js` | zio.py | ebzip の展開。`DecompressionStream("deflate")` を使う |
| `bkbook/jacode.js` | jacode.py | `TextDecoder("euc-jp")` による復号と、その逆引き表による符号化 |
| `bkbook/book.js` `subbook.js` | book.py subbook.py | catalog と索引表 |
| `bkbook/tables.js` `setword.js` `match.js` `search.js` | 同名 | 検索語の正規化、比較、B 木の走査 |
| `bkbook/text.js` | text.py | 本文の解釈とレンダラ |
| `bkbook/stopcode.js` | stopcode.py | 項目の終わりの推定 |
| `bkbook/font.js` | font.py | 外字のビットマップ。SVG にも描ける |
| `bkbook/appendix.js` | appendix.py | `*.app` の読み込み |
| `bkbook/gaiji.js` `gaiji-table.js` | gaiji.py | 外字の対応表。表は `tools/export_gaiji.py` で Python から生成する |
| `bkbook/collection.js` | cli.py の一部 | 何冊もまとめて開く。種類の判定、並び順、読みやすい整形 |
| `bkbook/html.js` | — | 本文を HTML にするレンダラ。UI 専用 |

読み出しがすべて非同期になる以外は、Python 版と同じ構造にしてある。
直すときは両方を直す。外字の対応表だけは正本が Python 側で、
`python3 web/tools/export_gaiji.py > web/bkbook/gaiji-table.js` で写す。

ブラウザの `TextDecoder("euc-jp")` は WHATWG の表を使っていて、Python の
`euc_jp` と 6 文字で食い違う（0x2141 が 〜 ではなく ～ になる、など）。
`jacode.js` はそれを Python 側に揃えている。

HTTP にはディレクトリ一覧がないので、ブラウザ版はファイルの一覧
（`manifest.txt`）を受け取ってそれを `listdir` の答えにする。
辞書のトップで `find . -type f | sort > manifest.txt` と打てば作れる。

## 検証

Python 版を正解器にする。Python で検索結果と索引の全件列挙のハッシュを
書き出し、Node で同じことをして突き合わせる。

```console
$ python3 web/test/golden.py ~/eb > golden.json
$ node web/test/compare.mjs golden.json ~/eb
5610 checks, 0 failures, 9.8s
```

目録、索引表、データファイルのページ、検索（前方・完全・後方・keyword・multi）、
索引の全件列挙、appendix と外字表、種類の判定、stop code の推定、見出しと本文、
メニュー、フォントのビットマップまでを比べる。`--quick` を付けると全件列挙を
省いて 40 秒ほどで終わる。

HTTP 経路も同じ正解で確かめられる。Python の `http.server` は Range を
解さないので、付属のサーバーを使う。

```console
$ node web/test/serve.mjs ~/eb 8000 &
$ node web/test/compare.mjs golden.json http://localhost:8000/
```

## 置き方

`web/` の中身を置き、その下に辞書のディレクトリ `eb/`（`manifest.txt` 入り）を置く。

```
bkbook/
  index.html  app.js  style.css  bkbook/
  eb/         辞書と manifest.txt
```

`index.html` の `data-eb="eb/"` が辞書の場所。別の場所なら属性を直すか、
`?eb=https://example.net/eb/` で指す。サーバーは静的ファイルを Range
リクエスト付きで返せればよく、CGI は要らない。

手元で試すときは、`web/eb` を辞書のディレクトリへのシンボリックリンクにして
（リポジトリ直下の `eb` があれば `ln -s ../eb web/eb`）、開発用サーバーを
`web/` で起こす。`manifest.txt` はサーバーがその場で作る。

```console
$ node web/test/serve.mjs web 8000
```

## 実機で測る

```console
$ node web/test/bench.mjs https://example.net/eb/ [--auth user:pass]
```

TUI と同じ動作（開いている辞書すべてに 40 件ずつ検索し、見出し 30 件と
本文 1 件を読む）をキー入力ごとに模擬して、リクエスト数と所要時間を出す。
