"""スプレッドシートの読み取りを1回にまとめる。

同じ実行の中で同じタブを何度も読むと、Sheets APIの1分あたりの読み取り上限に
当たる。記事1本を作る間に案件DBを6回開いていた。案件DB・院タブ・型タブの値は
実行中に変わらないので、一度読んだら使い回す。

書き込んだあとは clear() を呼んで捨てる。捨てないと古い値を読む。
"""

import time

DEFAULT_TTL = 120.0

_store: dict = {}


def get(key, loader, ttl: float = DEFAULT_TTL):
    """key で覚えた値を返す。無いか古ければ loader() を呼んで覚える。"""
    now = time.monotonic()
    hit = _store.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = loader()
    _store[key] = (now, value)
    return value


def clear() -> None:
    _store.clear()


def size() -> int:
    return len(_store)
