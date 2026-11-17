"""测试公共配置。

将插件根目录加入 sys.path，使测试可以通过 memory.xxx 形式导入各引擎模块。
"""

import sys
import os

# 将插件根目录加入 sys.path，使测试可以 import memory.xxx
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
