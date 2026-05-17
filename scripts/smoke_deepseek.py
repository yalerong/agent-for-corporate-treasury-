"""DeepSeek 连通性最小测试。

仅验证：
1. .env 加载正确
2. LLM_MODEL 在 OpenAI 兼容端点能识别
3. 一次完整的 invoke 能拿到 content
"""
from __future__ import annotations

import sys
import traceback

from app.config import get_settings
from app.llm import get_chat_model, reset_chat_model_cache


def main() -> int:
    s = get_settings()
    print(f"[config] provider={s.llm_provider} model={s.llm_model} base_url={s.llm_base_url}")
    print(f"[config] api_key={'<set>' if s.llm_api_key else '<EMPTY>'}")

    reset_chat_model_cache()
    chat = get_chat_model()
    print(f"[chat] class={chat.__class__.__name__}")

    try:
        resp = chat.invoke("用一句中文回答：你好吗？")
    except Exception as e:
        print(f"[FAIL] invoke raised: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1

    print(f"[ok] content={resp.content!r}")
    print(f"[ok] usage={getattr(resp, 'usage_metadata', None)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
