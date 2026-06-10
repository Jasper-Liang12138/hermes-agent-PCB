import json
from llm.client import LLMClient


UI_EXPLAIN_SYSTEM_PROMPT = """
你是一个智能布线评估解释助手。
你的职责是基于已经给出的结构化评估结果，回答用户关于当前结果的问题。

要求：
1. 只能围绕输入中的评估结果回答，不要编造未提供的信息。
2. 不要声称你看到了原始设计图、版图截图或底层源文件。
3. 回答应面向工程解释和汇报表达，语言清晰、专业、可读。
4. 可以总结问题、解释问题影响、说明风险优先级、整理汇报口径。
5. 如果用户的问题超出当前结果范围，要明确说明“基于当前评估结果”能看到什么、不能确认什么。
""".strip()


def build_ui_explain_prompt(ui_payload: dict, user_message: str) -> str:
    payload_text = json.dumps(ui_payload, ensure_ascii=False, indent=2)

    return f"""
当前有一份结构化评估结果，内容如下：
{payload_text}

用户问题：
{user_message}

请基于这份评估结果回答用户问题。
输出要求：
- 使用中文
- 不要输出 JSON
- 回答尽量直接、清晰
- 如果适合，可分成“结论 / 原因 / 建议”三部分
""".strip()


def explain_with_ui_payload(ui_payload: dict, user_message: str, model: str = "") -> str:
    prompt = build_ui_explain_prompt(ui_payload, user_message)
    client = LLMClient(model=model or None)
    result = client.chat(
        prompt=prompt,
        system_prompt=UI_EXPLAIN_SYSTEM_PROMPT,
        temperature=0.2,
    )
    return result