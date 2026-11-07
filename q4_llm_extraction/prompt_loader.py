# -*- coding: utf-8 -*-
"""解析 prompt_template.txt（===== 分段格式）为机读字典，保证模板文件即交付物本体。"""
from pathlib import Path


def load_prompts(path: Path) -> dict:
    """按 '===== NAME =====' 分段切分；返回 {段名: 文本}。"""
    sections, current = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = __import__("re").match(r"^===== (\w+) =====\s*$", line)
        if m:
            current = m.group(1)
            sections[current] = []
        elif current:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


if __name__ == "__main__":
    import sys
    p = Path(__file__).resolve().parent / "prompt_template.txt"
    d = load_prompts(p)
    for k, v in d.items():
        print(f"{k}: {len(v)} chars")
    sys.exit(0 if set(d) >= {"SYSTEM_PROMPT", "USER_TEMPLATE", "RETRY_PROMPT"} else 1)
