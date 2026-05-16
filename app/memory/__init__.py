"""会话记忆与状态管理。

Phase 2 当前实现：
    LangGraph 的 MemorySaver（in-memory checkpointer）提供 thread 级状态保持。
    所有 invoke 必须传 thread_id，状态可通过 graph.get_state(config) 查询。
    重启服务会丢失暂停的 HITL 状态——单机开发期可接受。

Phase 3 目标：
    1. 切换到持久化 checkpointer：
       - `langgraph.checkpoint.postgres.PostgresSaver`（推荐）
       - `langgraph.checkpoint.sqlite.SqliteSaver`（轻量备选）
    2. 跨会话长期记忆通过业务数据库存储（员工偏好、历史审批模式）
    3. 敏感数据脱敏后才写入 checkpoint（与 app/tools/masking.py 联动）

当前文件仅作为 Phase 3 接入点的占位与说明。
"""

__all__: list[str] = []
