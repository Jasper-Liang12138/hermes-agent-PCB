import os

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


class LLMClient:
    def __init__(self, api_key=None, base_url=None, model=None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        if OpenAI is None:
            raise ImportError(
                "openai package is not installed. Please run: pip install openai"
            )

        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is not set.")

        kwargs = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url

        self.client = OpenAI(**kwargs)

    def chat(self, prompt: str, system_prompt: str = "", temperature: float = 0.2) -> str:
        messages = []

        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt})

        messages.append({"role": "user", "content": prompt})

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
        )

        return resp.choices[0].message.content or ""