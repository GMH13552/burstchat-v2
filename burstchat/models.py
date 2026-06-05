"""
数据模型: 状态枚举、消息结构、行为计划
"""

from dataclasses import dataclass, field
from typing import Optional


class State:
    IDLE = "idle"
    WAITING_BURST = "waiting_burst"
    PLANNING = "planning"
    DISPATCHING = "dispatching"
    AWAITING_REPLAN = "awaiting_replan"
    SEARCHING = "searching"


class PendingMessage:
    __slots__ = ("send_at", "text")

    def __init__(self, send_at: float, text: str):
        self.send_at = send_at  # Unix timestamp
        self.text = text


@dataclass
class PlanResult:
    """LLM 规划结果：消息序列 + 可选的搜索查询"""
    messages: list[PendingMessage] = field(default_factory=list)
    search_query: str = ""  # 非空表示需要在发消息前后执行搜索


@dataclass
class BehaviorPlan:
    """调度器的行为决策：在调用 LLM 之前计算出来的约束"""

    # ── 回复模式 ──
    burst_mode: str = "auto"  # "single" | "multi" | "auto"
    max_fragments: int = 6
    energy: str = "neutral"   # "high" | "neutral" | "low" | "upset"

    # ── 口癖抑制 ──
    suppressed_tics: list[str] = field(default_factory=list)

    # ── 情绪注册 ──
    emotional_register: str = "neutral"

    # ── 时间约束（调度器管理的维度） ──
    fragment_delay_range: tuple = (2, 4)   # 同思绪碎片之间的秒数范围
    thought_delay_range: tuple = (6, 9)   # 转换话题/思绪之间的秒数范围
    first_reply_delay: int = 3            # 第一句回复的延迟（秒）

    # ── 状态标记 ──
    is_continuing_topic: bool = False     # 是否在延续已有话题
    user_msg_type: str = "unknown"        # "emotional" | "question" | "statement" | "greeting" | "joke"
    user_engagement: float = 0.5          # 用户参与度（0-1）
