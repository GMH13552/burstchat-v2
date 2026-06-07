"""
核心调度器 v2：对话状态机 + 行为控制器 + burst 检测 + 插话重规划

对话状态模型（独立于 burst 检测）：
  OUTSIDE → ATTENTION_DELAY → IN_CONVERSATION → OUTSIDE

  OUTSIDE:          对方没在看聊天（忙自己的事）
  ATTENTION_DELAY:  消息到了但还没注意到（等待 attention_delay 结束）
  IN_CONVERSATION:  已进入对话，正常 persona 节奏
"""

import asyncio
import json
import random
import time
from typing import Optional

from .models import State, PendingMessage, BehaviorPlan
from .llm import LLMClient
from .behavior import BehaviorController
from .search import search_sogou
from .prompt import SEARCH_RESULT_HINT


class ConvState:
    """对话参与状态（相对于 burst 检测独立）"""
    OUTSIDE = "outside"
    ATTENTION_DELAY = "attention_delay"
    IN_CONVERSATION = "in_conversation"

    # 默认值
    DEFAULT_ATTENTION_DELAY = (1, 5)        # 象征性延迟，真正的回复间隔由 LLM t 控制
    DEFAULT_CONVERSATION_TIMEOUT = 600       # 10分钟没人说话 → 退出对话


class Scheduler:
    def __init__(self, llm: LLMClient, app_callback, name: str = "", json_context: bool = True):
        self.llm = llm
        self.app = app_callback
        self.name = name
        self._on_dispatch: callable = None
        self._on_flush: callable = None
        self._json_context = json_context

        # 状态机
        self.state = State.IDLE
        self.context: list[dict] = []
        self.pending: list[PendingMessage] = []
        self.last_user_msg_time: float = 0

        # 行为控制器（v2 新增）
        self.behavior_ctrl = BehaviorController(self.llm.persona)

        # ── 对话参与状态机（v2.1） ──
        self.conv_state: str = ConvState.OUTSIDE
        self.last_activity_time: float = 0  # 对话中最后一次互动的时间
        self.attention_timer_task: Optional[asyncio.Task] = None
        self.exit_timer_task: Optional[asyncio.Task] = None

        # 从 persona 读取时间参数
        timing = self.llm.persona.timing
        self._attention_delay_range: tuple = self._parse_attention_delay(timing)
        self._conversation_timeout: int = timing.get(
            "conversation_timeout", ConvState.DEFAULT_CONVERSATION_TIMEOUT
        )

        # 定时器
        self.burst_timer_task: Optional[asyncio.Task] = None
        self.dispatch_task: Optional[asyncio.Task] = None

        # 状态追踪
        self._pending_texts: list[tuple] = []
        self._interject_at: Optional[float] = None
        self._deferred_user_msgs: list[dict] = []
        self._pending_search: str = ""
        self._last_user_text: str = ""
        self._current_behavior_plan: BehaviorPlan = None

    @staticmethod
    def _parse_attention_delay(timing: dict) -> tuple:
        """从 persona timing 读取 attention_delay，返回 (min_seconds, max_seconds)
        
        这模拟的是角色"看到消息前"的延迟（看手机习惯）。
        LLM 的第一个 t 只代表看到消息后的打字/思考时间，不再重复计。
        """
        ad = timing.get("attention_delay", {})
        profile = ad.get("profile", "moderate")
        profiles = ad.get("profiles", {})
        # 精确匹配 profile 名
        if profile in profiles:
            p = profiles[profile]
            return (p.get("min", 5), p.get("max", 60))
        # 没匹配到 → 用第一个可用的 profile（from_data / real 等 auto-generated key）
        if profiles:
            p = next(iter(profiles.values()))
            # 用 p50 作为 max，p50/4 作为 min（合理范围）
            p50 = p.get("p50", 30)
            return (max(1, p50 // 4), max(5, p50))
        return ConvState.DEFAULT_ATTENTION_DELAY

    # ── Message Entry ───────────────────────────────────────

    async def on_user_message(self, text: str):
        """收到消息 → 检测对话状态 → 进 context → 启动/重置 burst timer"""
        now = time.time()
        self.last_user_msg_time = now
        self._last_user_text = text
        self.last_activity_time = now

        # 通知行为控制器
        self.behavior_ctrl.record_user_msg(text)

        # 检测纠正意图
        if self.behavior_ctrl.handle_correction(text):
            self.app.on_status("📝 行为修正已记录")

        win = self._burst_window(text)
        self.context.append({"role": "user", "content": text})

        # ── 对话参与状态判断 ──
        if self.conv_state == ConvState.OUTSIDE:
            # 不在对话中 → 需要等 attention_delay 才能进入对话
            self._cancel_exit_timer()
            self.conv_state = ConvState.ATTENTION_DELAY
            delay = random.uniform(*self._attention_delay_range)
            self.app.on_status(f"📱 {self.name} 还没注意到消息... {delay:.0f}s" if self.name else f"📱 等待注意... {delay:.0f}s")
            self._start_attention_timer(now, delay)
            return  # 不继续 burst 流程，等 attention delay 结束

        elif self.conv_state == ConvState.ATTENTION_DELAY:
            # 延迟期间又来了一条 → 不打断，让它自然进入对话
            # 消息已进 context，等 attention timer 触发后自然会处理
            return

        # IN_CONVERSATION：正常流程
        self._cancel_exit_timer()  # 重置退出倒计时

        if self.state in (State.DISPATCHING, State.AWAITING_REPLAN, State.SEARCHING):
            self._deferred_user_msgs.append({"role": "user", "content": text})
            self._interject_at = now

        if self.state == State.IDLE:
            self.state = State.WAITING_BURST
            self._start_timer(now, win)
        elif self.state == State.WAITING_BURST:
            self._cancel_timer()
            self._start_timer(now, win)
        elif self.state == State.DISPATCHING:
            self.state = State.AWAITING_REPLAN
            self._cancel_timer()
            self._start_timer(now, win)
            self.app.on_status(f"🔄 插话检测，{win:.0f}s 后重规划...")
        elif self.state == State.PLANNING:
            self.state = State.WAITING_BURST
            self._cancel_timer()
            self._start_timer(now, win)
        elif self.state == State.AWAITING_REPLAN:
            self._cancel_timer()
            self._start_timer(now, win)

    # ── Burst Window ────────────────────────────────────────

    @staticmethod
    def _burst_window(text: str) -> float:
        n = len(text)
        if n < 5:
            return 4.0
        elif n < 15:
            return 3.0
        elif n < 50:
            return 2.0
        elif n < 100:
            return 1.0
        else:
            return 0.5

    # ── Conversation State Machine ──────────────────────────

    def _start_attention_timer(self, now: float, delay: float):
        """等待 attention_delay 后进入对话状态"""
        self._cancel_attention_timer()
        self.attention_timer_task = asyncio.create_task(self._attention_wait(delay))

    def _cancel_attention_timer(self):
        if self.attention_timer_task and not self.attention_timer_task.done():
            self.attention_timer_task.cancel()

    async def _attention_wait(self, delay: float):
        await asyncio.sleep(delay)
        now = time.time()
        self.conv_state = ConvState.IN_CONVERSATION
        self.last_activity_time = now
        self.app.on_status(f"👀 {self.name} 注意到了消息" if self.name else "👀 注意到了消息")

        # 进入对话后，像正常流程一样启动 burst 检测
        win = self._burst_window(self._last_user_text)
        self.state = State.WAITING_BURST
        self._start_timer(now, win)

    def _start_exit_timer(self):
        """对话沉默后启动退出倒计时"""
        self._cancel_exit_timer()
        self.exit_timer_task = asyncio.create_task(self._exit_wait())

    def _cancel_exit_timer(self):
        if self.exit_timer_task and not self.exit_timer_task.done():
            self.exit_timer_task.cancel()

    async def _exit_wait(self):
        await asyncio.sleep(self._conversation_timeout)
        # 超时前没人说话 → 退出对话状态
        if time.time() - self.last_activity_time >= self._conversation_timeout:
            self.conv_state = ConvState.OUTSIDE
            self.app.on_status(f"💤 {self.name} 忙别的去了" if self.name else "💤 退出对话")

    def _start_timer(self, now: float, duration: float):
        self.app.on_status(f"⏳ 等待 {duration:.0f}s...")
        self.burst_timer_task = asyncio.create_task(self._burst_timer(duration))

    def _cancel_timer(self):
        if self.burst_timer_task and not self.burst_timer_task.done():
            self.burst_timer_task.cancel()

    async def _burst_timer(self, duration: float):
        await asyncio.sleep(duration)
        await self._on_burst_end()

    async def _on_burst_end(self):
        now = time.time()

        if self.state == State.WAITING_BURST:
            self.state = State.PLANNING
            self.app.on_status(f"💭 {self.name} 思考中..." if self.name else "💭 正在思考...")
            await self._plan_and_dispatch(now, is_replan=False)

        elif self.state == State.AWAITING_REPLAN:
            self.state = State.PLANNING
            self.app.on_status(f"🔄 {self.name} 重新规划..." if self.name else "🔄 重新规划...")
            self._cancel_dispatch()
            self._flush_pending_texts()
            self._clear_remaining_pending()
            await self._plan_and_dispatch(now, is_replan=True)

    # ── Planning ─────────────────────────────────────────────

    async def _plan_and_dispatch(self, now: float, is_replan: bool = False):
        try:
            # ── v2: 行为分析 ──
            self._current_behavior_plan = self.behavior_ctrl.analyze(
                context=self.context,
                state=self.state,
                last_user_text=self._last_user_text,
            )
            bp = self._current_behavior_plan

            self._debug_behavior_plan(bp)

            plan = await self.llm.plan_messages(
                self.context, now,
                is_replan=is_replan,
                behavior_plan=bp,
            )
            messages = plan.messages
            self._pending_search = plan.search_query

            if self.state != State.PLANNING:
                return

            if not messages:
                self.state = State.IDLE
                self.app.on_status("👂 就绪")
                return

            self.pending = messages
            self._pending_texts = []
            self.state = State.DISPATCHING

            preview = " → ".join(
                f"[{m.send_at - now:.0f}s] {m.text[:15]}..." for m in messages
            )
            if self._pending_search:
                preview += f" | 🔍 {self._pending_search[:20]}"
            status = f"📡 {self.name} {len(messages)} 条 (energy={bp.energy})" if self.name else f"📡 {len(messages)} 条"
            self.app.on_status(status)

            self.dispatch_task = asyncio.create_task(self._dispatch_loop())

        except Exception as e:
            self.app.on_status(f"⚠️ LLM 出错: {e}")
            self.state = State.IDLE

    def _debug_behavior_plan(self, bp: BehaviorPlan):
        """调试输出行为计划"""
        parts = [
            f"mode={bp.burst_mode}",
            f"max={bp.max_fragments}",
            f"energy={bp.energy}",
            f"register={bp.emotional_register}",
            f"first_delay={bp.first_reply_delay}s",
            f"frag={bp.fragment_delay_range}",
            f"thought={bp.thought_delay_range}",
        ]
        if bp.suppressed_tics:
            parts.append(f"NO_TICS={bp.suppressed_tics}")
        print(f"[Behavior] {' | '.join(parts)}")

    # ── Dispatch ─────────────────────────────────────────────

    async def _dispatch_loop(self):
        try:
            for msg in self.pending:
                if self.state not in (State.DISPATCHING, State.AWAITING_REPLAN):
                    return

                delay = msg.send_at - time.time()
                while delay > 0:
                    if self.state not in (State.DISPATCHING, State.AWAITING_REPLAN):
                        return
                    await asyncio.sleep(min(delay, 0.2))
                    delay = msg.send_at - time.time()

                if self.state not in (State.DISPATCHING, State.AWAITING_REPLAN):
                    return

                self._dispatch_one(msg)

            if self.state == State.DISPATCHING:
                self._flush_pending_texts()

                # 记录已发送消息给行为控制器
                texts = [msg.text for msg in self.pending]
                self.behavior_ctrl.record_dispatched(texts)

                if self._on_flush:
                    self._on_flush()

                if self._pending_search:
                    await self._execute_search_and_replan()
                else:
                    self.state = State.IDLE
                    self._start_exit_timer()  # 对话暂时结束，启动退出倒计时
                    self.app.on_status("👂 就绪")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.app.on_status(f"⚠️ Dispatch 出错: {e}")

    async def _execute_search_and_replan(self):
        """执行网页搜索 → 注入结果到 context → 触发 replan"""
        query = self._pending_search
        self._pending_search = ""
        self.state = State.SEARCHING
        self.app.on_status(f"🔍 搜索中: {query[:30]}...")

        try:
            response = await search_sogou(query, max_results=5)
            self.app.on_status(f"🔍 搜索完成: {len(response.results)} 条结果")
        except Exception as e:
            self.app.on_status(f"⚠️ 搜索失败: {e}")
            self.state = State.IDLE
            return

        if response.error:
            self.app.on_status(f"⚠️ 搜索失败: {response.error}")
            self.state = State.IDLE
            return

        if not response.results:
            hint = SEARCH_RESULT_HINT.format(
                query=query,
                results=f"(没有找到与\"{query}\"相关的结果)",
            )
            self.context.append({"role": "system", "content": hint})
            self.app.on_status(f"🔍 无结果: {query[:20]}...")
        else:
            hint = SEARCH_RESULT_HINT.format(
                query=query,
                results=response.context_text,
            )
            self.context.append({"role": "system", "content": hint})
            self.app.on_status(f"🔍 找到 {len(response.results)} 条: {query[:20]}...")

        self.state = State.PLANNING
        now = time.time()
        await self._plan_and_dispatch(now, is_replan=True)

    def _dispatch_one(self, msg: PendingMessage):
        now = time.time()
        self.app.on_message(self.name or "assistant", msg.text, now)
        self._pending_texts.append((now, msg.text))
        if self._on_dispatch:
            self._on_dispatch(self.name, msg.text, now)

    # ── Context Flush ────────────────────────────────────────

    def _flush_pending_texts(self):
        if not self._pending_texts and not self._deferred_user_msgs:
            return

        pre, post = [], []
        for send_time, text in self._pending_texts:
            if self._interject_at is not None and send_time > self._interject_at:
                post.append(text)
            else:
                pre.append(text)

        def _fmt(texts, search_query=""):
            obj = {"messages": [{"t": 2 + j * 3, "text": t} for j, t in enumerate(texts)]}
            if search_query:
                obj["search"] = search_query
            return json.dumps(obj, ensure_ascii=False)

        if pre:
            sq = self._pending_search
            self.context.append({"role": "assistant", "content": _fmt(pre, search_query=sq)})
        for m in self._deferred_user_msgs:
            self.context.append(m)
        if post:
            self.context.append({"role": "assistant", "content": _fmt(post)})

        self._pending_texts.clear()
        self._deferred_user_msgs.clear()
        self._interject_at = None

    def _clear_remaining_pending(self):
        count = len(self.pending)
        if count:
            preview = ", ".join(m.text[:10] for m in self.pending)
            self.app.on_status(f"🗑️ 清除了 {count} 条: {preview}...")
        self.pending.clear()

    def _cancel_dispatch(self):
        if self.dispatch_task and not self.dispatch_task.done():
            self.dispatch_task.cancel()
            self.dispatch_task = None

    async def shutdown(self):
        self._cancel_timer()
        self._cancel_dispatch()
        self._cancel_attention_timer()
        self._cancel_exit_timer()
