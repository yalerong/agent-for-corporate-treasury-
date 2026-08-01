"""LLM 客户端：openai SDK 直连（不引 langchain，不 import app/）。

环境变量: LLM_API_KEY / LLM_BASE_URL / LLM_MODEL，与 app/ 共用一份 .env。
无 key 或未安装 openai 包 → get_client() 返回 None，调用方按"跳过"处理，
确定性流水线完全不依赖本模块。
"""
import json
import os

from constants import CODE_DIR

DEFAULT_MODEL = "deepseek-chat"


def get_client():
    try:
        from dotenv import load_dotenv
        load_dotenv(CODE_DIR.parent / ".env")
    except ImportError:
        pass
    key = os.environ.get("LLM_API_KEY")
    if not key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        print("未安装 openai 包，LLM 环跳过（pip install 'openai>=1,<2'）")
        return None
    return OpenAI(api_key=key, base_url=os.environ.get("LLM_BASE_URL") or None)


def chat_json(client, system: str, user: str, model: str | None = None) -> dict:
    """一次对话，强制 JSON 输出；解析失败抛 ValueError。temperature=0 保持可复现倾向。"""
    resp = client.chat.completions.create(
        model=model or os.environ.get("LLM_MODEL", DEFAULT_MODEL),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM 返回不是合法 JSON: {content[:200]}") from e
