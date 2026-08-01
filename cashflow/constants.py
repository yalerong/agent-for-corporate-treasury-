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

# 规律核验与自动降级（validate.py）
VALIDATE_WINDOW_DAYS = 70   # recurring 检查窗口(天)，覆盖约 2 个预期日
VALIDATE_DAY_TOL = 3        # 预期日 ± 天数窗口找付款
VALIDATE_AMT_TOL = 0.4      # 金额与均额偏差 ±40% 内算 hit
WEEKLY_BAND_CVS = 2.0       # weekly 周额带宽 = base ± N·cv·base
DEMOTE_HIT_RATE = 0.5       # hit_rate 低于此为一次失败
DEMOTE_STREAK = 2           # 连续 N 次失败的 approved 自动降回 candidate（永不自动 refuted）

# 异动归因（attribution.py）
ALIGN_SHARE = 0.6           # 贡献金额落在日历预期日的占比 ≥ 此值 → 打"预期内"标签
MONTH_END_DAYS = 3          # 月末最后 N 天视为日历预期日


def get_root() -> Path:
    """数据根目录：env CASHFLOW_ROOT 优先（测试用），默认与代码同目录（现状不变）。"""
    env = os.environ.get("CASHFLOW_ROOT")
    return Path(env) if env else CODE_DIR
