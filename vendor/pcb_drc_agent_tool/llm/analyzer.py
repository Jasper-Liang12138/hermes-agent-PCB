import json
from typing import List, Tuple

from llm.client import LLMClient
from llm.prompt_builder import build_issue_analysis_prompt


DEFAULT_SYSTEM_PROMPT = """
你是一个资深PCB可制造性与BGA escape routing分析工程师。
你的任务是分析DRC工具输出的问题，并给出工程化、可执行的修复建议。
你必须严格围绕输入中的对象、网络、层和规则编号来分析，避免空泛回答。
""".strip()


def analyze_issues_with_llm(
    board,
    issues: List,
    model: str = "",
    save_txt_path: str = "",
    save_prompt_path: str = "",
) -> Tuple[str, str]:
    prompt = build_issue_analysis_prompt(board, issues)

    if save_prompt_path:
        with open(save_prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt)

    client = LLMClient(model=model or None)
    result = client.chat(
        prompt=prompt,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        temperature=0.2,
    )

    if save_txt_path:
        with open(save_txt_path, "w", encoding="utf-8") as f:
            f.write(result)

    return prompt, result