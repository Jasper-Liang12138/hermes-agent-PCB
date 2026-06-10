import json
import os
from openai import OpenAI


def explain_issues(issues) -> str:
    """
    Use an OpenAI-compatible endpoint.
    For Qwen / DashScope:
      export DASHSCOPE_API_KEY=...
    """
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        return "[LLM] DASHSCOPE_API_KEY is not set."

    client = OpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    payload = [issue.to_dict() for issue in issues]

    prompt = (
        "You are a PCB DRC analysis assistant.\n"
        "Analyze the following BGA escape routing violations one by one.\n"
        "For each violation, explain:\n"
        "1. What the rule means\n"
        "2. The likely cause\n"
        "3. A practical fix suggestion\n\n"
        f"Issues:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

    try:
        completion = client.chat.completions.create(
            model="qwen-plus",
            messages=[
                {"role": "system", "content": "You are a helpful PCB assistant."},
                {"role": "user", "content": prompt},
            ],
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"[LLM] Call failed: {e}"