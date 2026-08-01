"""LLM 客户端：双通道——Anthropic 官方 SDK（Claude）/ openai SDK（DeepSeek 等兼容端）。

通道选择（显式优先，保证"无配置=跳过"的离线纪律）:
- `LLM_PROVIDER=anthropic` → anthropic 官方 SDK。凭据走 `ANTHROPIC_API_KEY`
  （或 `ant auth login` 档案），模型 `LLM_MODEL`（默认 claude-opus-5）。
- 否则有 `LLM_API_KEY` → openai SDK 直连 `LLM_BASE_URL`
  （DeepSeek: https://api.deepseek.com），模型 `LLM_MODEL`（默认 deepseek-chat）。
- 两者皆无 → None，调用方按"跳过"处理；确定性流水线零依赖本模块。
与 app/ 共用一份 .env；不引 langchain、不 import app/。
"""
import json
import os

from constants import CODE_DIR

DEFAULT_OPENAI_MODEL = "deepseek-chat"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(CODE_DIR.parent / ".env")
    except ImportError:
        pass


def get_client():
    """返回 ("anthropic", client) / ("openai", client) / None。"""
    _load_env()
    if os.environ.get("LLM_PROVIDER", "").lower() == "anthropic":
        try:
            import anthropic
        except ImportError:
            print("未安装 anthropic 包，LLM 环跳过（pip install anthropic）")
            return None
        return ("anthropic", anthropic.Anthropic())
    key = os.environ.get("LLM_API_KEY")
    if not key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        print("未安装 openai 包，LLM 环跳过（pip install 'openai>=1,<2'）")
        return None
    return ("openai", OpenAI(api_key=key, base_url=os.environ.get("LLM_BASE_URL") or None))


def chat_json(handle, system: str, user: str, schema: dict | None = None) -> dict:
    """一次对话，强制 JSON 输出；拒答/解析失败抛 ValueError。

    schema（JSON Schema）在 Anthropic 通道走 output_config 结构化输出硬约束；
    openai 兼容通道退化为 json_object 模式（代码侧三重闸兜底）。
    """
    provider, client = handle
    if provider == "anthropic":
        return _chat_json_anthropic(client, system, user, schema)
    resp = client.chat.completions.create(
        model=os.environ.get("LLM_MODEL", DEFAULT_OPENAI_MODEL),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM 返回不是合法 JSON: {content[:200]}") from e


def _chat_json_anthropic(client, system: str, user: str, schema: dict | None) -> dict:
    kwargs = {}
    if schema is not None:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    resp = client.messages.create(
        model=os.environ.get("LLM_MODEL", DEFAULT_ANTHROPIC_MODEL),
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        **kwargs,
    )
    if resp.stop_reason == "refusal":
        raise ValueError("Claude 安全分类器拒答（stop_reason=refusal）")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM 返回不是合法 JSON: {text[:200]}") from e
