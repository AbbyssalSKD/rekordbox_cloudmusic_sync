"""rb_sync：Rekordbox 离线曲库同步编排器。

定位：编排层，不是下载器、不是解密器。
当前实现「多平台歌单读取」这一环（MVP 第 1 步）：
网易云 / QQ音乐 / 酷狗 / 本地 CSV-JSON。
"""

__version__ = "0.3.0"

__all__ = ["__version__"]
