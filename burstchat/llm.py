"""
DeepSeek API 客户端（v2：分层 persona + 行为计划）
"""

import json
import os
import re
from datetime import datetime

from openai import AsyncOpenAI

from .models import PendingMessage, PlanResult, BehaviorPlan
from .persona import load_persona
from .prompt import build_footer


class LLMClient:
    def __init__(self, api_key: str, persona: str = "xiaoye", model: str = "deepseek-chat"):
        self.client = AsyncOpenAI(
            api_key=***,
            base_url="https://api.deepseek.com",
        )
        self.model = model
        self.persona = load_persona(persona)
        self._log_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug.log")

    def _debug_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")

    async def plan_messages(
        self,
        context: list[dict],
        now: float,
        is_replan: bool = False,
        behavior_plan: BehaviorPlan = None,
        search_context: str = "",
    ) -> PlanResult:
        """生成消息序列（v2：支持 behavior_plan 和 search_context）"""
        # 用分层 persona 构建系统 prompt
        system_text = self.persona.build_system_prompt(
            behavior_plan=behavior_plan,
            is_replan=is_replan,
            search_context=search_context,
        )
        footer = build_footer(now)

        messages = [
            {"role": "system", "content": system_text},
            *context,
            {"role": "system", "content": footer},
        ]

        self._debug_log(
            f"PROMPT ({len(messages)} msgs, is_replan={is_replan}): "
            f"ctx_turns={len(context)}, "
            f"energy={behavior_plan.energy if behavior_plan else 'N/A'}, "
            f"suppressed_tics={behavior_plan.suppressed_tics if behavior_plan else []}"
        )

        # 根据 behavior 调整 temperature
        temperature = 0.9
        if behavior_plan:
            if behavior_plan.energy == "high":
                temperature = 0.95
            elif behavior_plan.energy == "low":
                temperature = 0.7

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=800,
        )

        content = response.choices[0].message.content.strip()
        self._debug_log(f"RAW ({len(content)} chars): {content}")

        return self._parse_response(content, now)

    def _parse_response(self, content: str, now: float) -> PlanResult:
        # ── Clean content ──
        if content.startswith("```"):
            lines = content.split("\n")
            inner = "\n".join(lines[1:])
            if inner.rstrip().endswith("```"):
                inner = inner.rstrip()[:-3]
            content = inner.strip()

        data = None

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            pass

        if data is None:
            m = re.search(r"\[.*\]", content, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group())
                except json.JSONDecodeError:
                    pass

        if data is None and content:
            self._debug_log(f"PLAIN TEXT: {content[:200]}")
            blocks = [b.strip() for b in content.split("\n\n") if b.strip()]
            if len(blocks) <= 1:
                blocks = [b.strip() for b in content.split("\n") if b.strip()]
            all_items = []
            for block in blocks:
                try:
                    parsed = json.loads(block)
                    all_items.extend(self._extract_items(parsed))
                except json.JSONDecodeError:
                    all_items.append({"t": 3, "text": block[:120]})
            if all_items:
                return PlanResult(messages=self._build_messages(all_items, now))
            return PlanResult(messages=[PendingMessage(now + 2, "嗯嗯")])

        if data is None:
            self._debug_log(f"EMPTY: {repr(content[:200])}")
            return PlanResult(messages=[PendingMessage(now + 2, "嗯嗯")])

        # Extract optional search query
        search_query = ""
        if isinstance(data, dict):
            search_query = str(data.pop("search", "")).strip()

        items = self._extract_items(data)
        return PlanResult(
            messages=self._build_messages(items, now),
            search_query=search_query,
        )

    @staticmethod
    def _extract_items(data) -> list[dict]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = (
                data.get("msgs")
                or data.get("messages")
                or data.get("replies")
                or next((v for v in data.values() if isinstance(v, list)), None)
            )
            if items:
                return items
            for key in ("text", "reply", "content"):
                if key in data:
                    return [{"t": 2, "text": str(data[key])}]
            first_str = next((v for v in data.values() if isinstance(v, str)), None)
            if first_str:
                return [{"t": 2, "text": first_str}]
        return [{"t": 2, "text": "嗯嗯"}]

    @staticmethod
    def _build_messages(items: list[dict], now: float) -> list[PendingMessage]:
        result = []
        elapsed = 0.0
        for item in items:
            t = float(item.get("t", 2))
            text = str(item.get("text", ""))
            if text.strip():
                elapsed += t
                result.append(PendingMessage(now + elapsed, text))
        if not result:
            result.append(PendingMessage(now + 2, "嗯嗯"))
        return result
