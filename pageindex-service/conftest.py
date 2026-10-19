"""pytest 配置：让 `app` 包可导入（服务是独立包，与引擎的 app 无关）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
