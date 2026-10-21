#!/usr/bin/env python3
"""
matplotlib 用の日本語フォント設定（スキル共通）

    from scripts.jp_font import setup_japanese_font   # または各スキルのメインモジュールから
    font = setup_japanese_font()
    if font is None:
        ...  # 日本語フォントが無い → タイトル・軸ラベルは英語にする

方針（実際に起きたトラブルから）:
- japanize-matplotlib は使わない。環境によって wheel ビルドに失敗し、同じ pip コマンドで
  入れた他パッケージまで巻き添えで入らなくなる。また文字化けする環境もあった。
- まず環境に入っている日本語フォント（Noto Sans CJK JP など）を探して使う。
- 見つからなければ matplotlib-fontja（pure Python、フォント同梱）を試す（install=True のとき）。
- それも駄目なら None を返す（呼び出し側で英語ラベルに切り替える）。
"""

import subprocess
import sys

CANDIDATES = [
    'Noto Sans CJK JP', 'Noto Sans JP', 'IPAexGothic', 'IPAGothic', 'TakaoGothic',
    'Hiragino Sans', 'Hiragino Kaku Gothic ProN', 'Yu Gothic', 'Meiryo', 'MS Gothic',
]


def _fc_list_ja():
    try:
        return subprocess.run(['fc-list', ':lang=ja', 'family'],
                              capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ''


def setup_japanese_font(install=True):
    """日本語フォントを matplotlib に設定し、使ったフォント名を返す。無理なら None。"""
    import matplotlib
    import matplotlib.font_manager as fm

    available = {f.name for f in fm.fontManager.ttflist}
    fc = _fc_list_ja()
    for font in CANDIDATES:
        if font in available or font in fc:
            matplotlib.rcParams['font.family'] = font
            matplotlib.rcParams['axes.unicode_minus'] = False
            return font

    try:
        import matplotlib_fontja  # noqa: F401
        matplotlib.rcParams['axes.unicode_minus'] = False
        return 'matplotlib-fontja'
    except ImportError:
        pass
    if install:
        try:
            subprocess.run([sys.executable, '-m', 'pip', 'install', '-q',
                            '--break-system-packages', 'matplotlib-fontja'],
                           check=True, capture_output=True, timeout=180)
            import matplotlib_fontja  # noqa: F401
            matplotlib.rcParams['axes.unicode_minus'] = False
            return 'matplotlib-fontja'
        except Exception:
            pass
    return None


if __name__ == '__main__':
    print(setup_japanese_font())
