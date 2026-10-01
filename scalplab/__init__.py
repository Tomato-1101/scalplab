"""scalplab パッケージ。

futu-api SDK は既定で file_level=DEBUG（ft_logger.py:76）のため、相場プッシュを
1 件につき 2 行（受信時と callback 配送時）~/.com.futunn.FutuOpenD/Log/ に書き続ける。
録画中は 1 日 2,000 万行 = 3GB に達し、9 日で 25GB 溜まっていた。価格は 1 つも
含まれない（proto 番号と serial のみ）ので保持する意味がない。SDK 側も
「生产环境中，尽量关闭冗余的log」とコメントしており、切るのが想定された使い方。
"""
from futu.common.ft_logger import logger as _futu_logger

_futu_logger.debug_model = False
