"""工具脚本的导入引导（bootstrap）。

问题：工具脚本在 工具/ 下，业务代码在 练习/ 下，两者是兄弟目录。
     Python 默认只把「脚本自己所在目录」加进 sys.path，所以 from Day8_n3_structured import ...
     在命令行运行时会 ModuleNotFoundError。

解决：运行前把 练习/ 加进 sys.path（运行时生效）。
     编辑器里请额外把 练习 目录 Mark Directory as → Sources Root，
     这样 PyCharm 的静态检查也能看到这些模块（否则会标红，但不影响运行）。

用法（工具目录下每个脚本开头）：
    import _bootstrap  # noqa: F401  —— 必须在使用业务模块之前 import
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRACTICE_DIR = PROJECT_ROOT / "app"

if str(PRACTICE_DIR) not in sys.path:
    sys.path.insert(0, str(PRACTICE_DIR))
