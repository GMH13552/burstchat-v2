"""
行为控制器：调度器在调用 LLM 前做行为决策
- 口癖抑制
- 时间节奏
- 情绪上下文检测
- 硬规则注入
"""

import time
from collections import deque, Counter
from typing import Optional

from .models import BehaviorPlan, State


class BehaviorController:
    """调度器的行为决策层

    核心职责：
      1. 检测口癖过度重复 → 输出 suppressed_tics
      2. 分析用户消息类型 → 决定 burst_mode / max_fragments
      3. 根据 conversation 上下文 → 决定 fragment 延迟范围
      4. 检测用户情绪 → 调整 energy / emotional_register
    """

    # ── 口癖检测配置 ──
    # 过去 N 条已发消息中出现超过 M 次的词汇会被抑制
    TIC_WINDOW_SIZE = 10
    TIC_THRESHOLD = 3
    # 内置口癖模式（从 persona 中也会加载）
    DEFAULT_TIC_PATTERNS = [
        "草", "卧槽", "啊？？", "好家伙", "确实",
        "摸摸头", "笑死", "救命", "离谱", "绝了",
        "哈哈哈", "我也", "嗯嗯", "hhh",
    ]

    # ── 时间约束默认值 ──
    FRAGMENT_FAST = (1, 3)      # 高能量碎片间隔
    FRAGMENT_NORMAL = (2, 4)     # 正常碎片间隔
    FRAGMENT_SLOW = (4, 7)       # 低落碎片间隔
    THOUGHT_FAST = (4, 7)        # 高能量换话题
    THOUGHT_NORMAL = (6, 9)      # 正常换话题
    THOUGHT_SLOW = (8, 14)       # 低落换话题

    def __init__(self, persona):
        self.persona = persona  # LayeredPersona
        self.recent_tics: deque = deque(maxlen=self.TIC_WINDOW_SIZE)
        self.burst_history: deque = deque(maxlen=5)
        self.user_engagement: float = 0.5
        self.last_user_msg_time: float = 0
        self.conversation_turn: int = 0

    # ── 主入口 ──

    def analyze(self, context: list[dict], state: State,
                last_user_text: str = "") -> BehaviorPlan:
        """分析上下文，输出行为决策"""
        plan = BehaviorPlan()

        # 1. 用户消息类型
        plan.user_msg_type = self._classify_user_msg(last_user_text)

        # 2. 检测话题连续性
        plan.is_continuing_topic = self._detect_topic_continuity(context)

        # 3. 决定 burst 模式
        self._determine_burst_mode(plan, state)

        # 4. 口癖抑制
        plan.suppressed_tics = self._get_overused_tics()

        # 5. 情绪注册
        plan.emotional_register = self._compute_register(context, last_user_text)

        # 6. 时间约束
        self._compute_timing(plan)

        # 7. 用户参与度
        plan.user_engagement = self.user_engagement

        return plan

    def record_dispatched(self, texts: list[str]):
        """记录已发送的消息内容，用于口癖检测"""
        for t in texts:
            self.recent_tics.append(t)
        self.burst_history.append({
            "time": time.time(),
            "count": len(texts),
            "texts": texts,
        })

    def record_user_msg(self, text: str):
        """记录用户消息，用于参与度计算"""
        now = time.time()
        gap = now - self.last_user_msg_time if self.last_user_msg_time else 0
        self.last_user_msg_time = now
        self.conversation_turn += 1

        # 更新参与度（滑动平均）
        if gap > 0:
            if gap < 5:
                self.user_engagement = min(1.0, self.user_engagement + 0.1)
            elif gap > 300:
                self.user_engagement = max(0.1, self.user_engagement - 0.15)
            else:
                # 缓慢衰减
                self.user_engagement = max(0.3, self.user_engagement - 0.02)

    # ── 口癖检测 ──

    def _get_overused_tics(self) -> list[str]:
        """检查最近发出的消息中过度使用的口癖"""
        if len(self.recent_tics) < self.TIC_WINDOW_SIZE // 2:
            return []

        counts = Counter()
        for msg in self.recent_tics:
            for tic in self.DEFAULT_TIC_PATTERNS:
                if tic in msg:
                    counts[tic] += 1
            # 也检查 persona 中定义的口头禅
            catchphrases = self.persona.layer_2.get("catchphrases", [])
            for cp in catchphrases:
                if cp in msg:
                    counts[cp] += 1

        return [tic for tic, n in counts.items() if n >= self.TIC_THRESHOLD]

    # ── 用户消息分类 ──

    @staticmethod
    def _classify_user_msg(text: str) -> str:
        if not text:
            return "unknown"
        t = text.strip()
        # 情绪表达
        if any(kw in t for kw in ["哈哈", "笑死", "草", "卧槽", "啊？？", "好烦",
                                    "难过", "哭了", "呜呜", "开心", "麻了", "累了"]):
            return "emotional"
        # 提问
        if "?" in t or "？" in t or any(kw in t for kw in ["知道", "怎么", "什么", "哪", "谁"]):
            return "question"
        # 短问候
        if len(t) <= 3 and any(kw in t for kw in ["嗨", "hi", "早", "晚安", "嗯"]):
            return "greeting"
        # 笑话/梗
        if any(kw in t for kw in ["笑", "梗", "乐", "逗"]):
            return "joke"
        return "statement"

    # ── 话题连续性 ──

    @staticmethod
    def _detect_topic_continuity(context: list[dict]) -> bool:
        """简单检测是否在延续已有话题"""
        if len(context) < 2:
            return False
        # 看最近两条用户消息的时间间隔
        recent_users = [m for m in context[-6:] if m.get("role") == "user"]
        if len(recent_users) < 2:
            return False
        # 如果最近两条用户消息之间有 assistant 回复 → 在对话中
        last_is_user = context[-1].get("role") == "user"
        second_last_is_assistant = len(context) > 1 and context[-2].get("role") == "assistant"
        return last_is_user and second_last_is_assistant

    # ── Burst 模式决策 ──

    def _determine_burst_mode(self, plan: BehaviorPlan, state: State):
        msg_type = plan.user_msg_type

        if state == State.AWAITING_REPLAN:
            # 被打断重规划
            plan.burst_mode = "auto"
            plan.max_fragments = 4
            plan.energy = "neutral"
        elif msg_type == "emotional":
            plan.burst_mode = "multi"
            plan.max_fragments = 4
            plan.energy = "high"
        elif msg_type == "question":
            plan.burst_mode = "auto"
            plan.max_fragments = 3
            plan.energy = "neutral"
        elif msg_type == "greeting":
            plan.burst_mode = "auto"
            plan.max_fragments = 2
            plan.energy = "neutral"
        elif msg_type == "joke":
            plan.burst_mode = "multi"
            plan.max_fragments = 3
            plan.energy = "high"
        elif plan.is_continuing_topic and self.user_engagement > 0.6:
            plan.burst_mode = "multi"
            plan.max_fragments = 5
            plan.energy = "high"
        elif self.user_engagement < 0.3:
            plan.burst_mode = "auto"
            plan.max_fragments = 2
            plan.energy = "low"
        else:
            plan.burst_mode = "auto"
            plan.max_fragments = 4
            plan.energy = "neutral"

    # ── 情绪注册 ──

    def _compute_register(self, context: list[dict], last_user_text: str) -> str:
        """从上下文推断当前情绪基调"""
        text = last_user_text.strip() if last_user_text else ""

        # 负面情绪关键词
        negative_kw = ["烦", "难过", "哭", "累", "气", "不爽", "无语", "崩溃",
                        "想死", "麻了", "抑郁", "焦虑", "害怕", "不开心"]
        # 正面情绪关键词
        positive_kw = ["开心", "笑死", "乐", "草（正面）", "哈哈", "nice", "好耶",
                        "爽", "绝了", "牛", "厉害", "喜欢"]

        neg_count = sum(1 for kw in negative_kw if kw in text)
        pos_count = sum(1 for kw in positive_kw if kw in text)

        if neg_count > pos_count:
            return "down"
        elif pos_count > neg_count:
            return "up"
        return "neutral"

    # ── 时间约束计算 ──

    def _compute_timing(self, plan: BehaviorPlan):
        """根据 energy 和上下文计算具体的延迟参数"""
        timing = self.persona.timing

        # 第一条回复的延迟
        if plan.energy == "high":
            plan.first_reply_delay = timing.get("first_reply_gap", {}).get("min", 2)
        elif plan.energy == "low":
            plan.first_reply_delay = timing.get("first_reply_gap", {}).get("max", 8)
        else:
            plan.first_reply_delay = timing.get("first_reply_gap", {}).get("normal", 3)

        # 碎片间隔
        if plan.energy == "high":
            plan.fragment_delay_range = self.FRAGMENT_FAST
            plan.thought_delay_range = self.THOUGHT_FAST
        elif plan.energy == "low":
            plan.fragment_delay_range = self.FRAGMENT_SLOW
            plan.thought_delay_range = self.THOUGHT_SLOW
        else:
            plan.fragment_delay_range = self.FRAGMENT_NORMAL
            plan.thought_delay_range = self.THOUGHT_NORMAL

    # ── Correction 处理 ──

    def handle_correction(self, user_msg: str):
        """检测用户纠正意图并写入修正"""
        # 匹配纠正模式
        correction_patterns = [
            ("这不对", "你表现得不对"),
            ("不要这样", "行为不符合预期"),
            ("别老", "口癖过度"),
            ("别总是", "口癖过度"),
            ("加一条", "需要新增规则"),
            ("你应该", "需要调整行为"),
        ]

        for pattern, scene in correction_patterns:
            if pattern in user_msg:
                # 提取纠正内容
                correct_behavior = user_msg.split(pattern, 1)[1].strip() if pattern in user_msg else user_msg
                self.persona.add_correction(
                    scene=scene,
                    wrong=f"之前的行为触发了 {pattern}",
                    correct=correct_behavior,
                )
                return True
        return False
