# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux

"""入口。3 通りの呼ばれ方をする。

プロジェクトのディレクトリからの ``python3 -m bkbook``、どこからでも呼べる
``python3 path/to/bkbook``、そしてそのどちらかへの alias。2 つ目はこのファイルを
パッケージの外から素のスクリプトとして走らせるので、取り込みを手で用意して
やる必要がある。
"""

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from bkbook.cli import main
else:
    from .cli import main

sys.exit(main())
