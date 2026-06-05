"""
Prompt 构建器（v2：分层 persona + 调度器行为约束）
"""

import os
from datetime import datetime

from .persona import load_persona, LayeredPersona


def _here() -> str:
    return os.path.dirname(os.path.abspath(__file__))


# ── Footer（每次 LLM 调用时追加） ──

def build_footer(now: float) -> str:
    return (
        f"【格式铁律 — 你必须严格遵循】\n"
        f'输出格式: {{"search": "", "messages":[{{"t":秒数,"text":"内容"}},...]}}\n'
        f"t=与上条消息的间隔秒数。同思绪3-4s，换话题6-9s。20字+至少10s。\n"
        f"{datetime.fromtimestamp(now).strftime('%H:%M:%S')} 现在开始，你的回复只能是一个JSON对象。"
    )


# ── Search Result Hint ──

SEARCH_RESULT_HINT = (
    "\n🔍 你刚才搜了\"{query}\"：\n\n"
    "{results}"
)


# ── Public API ──

def load_persona(name: str = "xiaoye") -> LayeredPersona:
    """从 personas/ 目录加载分层 persona"""
    from .persona import load_persona as _load
    return _load(name)
