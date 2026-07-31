"""cashflow 共享常量与路径解析。

各脚本平铺在 cashflow/ 目录直跑（cd cashflow && python xxx.py），
共享代码以兄弟模块形式 import（sys.path[0] = 脚本所在目录）。
"""
import os
from pathlib import Path

# 代码所在目录（column_map.example.yaml 等随代码走的文件在这里找）
CODE_DIR = Path(__file__).parent

# 分组口径：主体 + 项目 + 币种
GROUP = ["entity", "project", "currency"]

# 预算(周付款预测)只覆盖计划性付款；实际取"流程支出"且剔除内部资金移动，才是可比口径
BUDGET_EXCLUDE = ["同户名划转出款", "关联方拆借出款", "提现出款", "回充出款", "业务账户流水"]


def get_root() -> Path:
    """数据根目录：env CASHFLOW_ROOT 优先（测试用），默认与代码同目录（现状不变）。"""
    env = os.environ.get("CASHFLOW_ROOT")
    return Path(env) if env else CODE_DIR
