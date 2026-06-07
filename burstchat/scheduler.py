"""
核心调度器 v2：对话状态机 + 行为控制器 + burst 检测 + 插话重规划

对话状态模型（v2.2：LLM 感知延迟，不再代码硬模拟）
  OUTSIDE → IN_CONVERSATION (首条消息直接进，sys 提示告知 LLM)
  IN_CONVERSATION → OUTSIDE (超时退出)
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
    """对话参与状态（v2.2：去掉 ATTENTION_DELAY，改为 LLM 感知节奏）
    OUTSIDE → IN_CONVERSATION (首条消息直接进，sys 提示告知 LLM)
    IN_CONVERSATION → OUTSIDE (超时退出)
    """
    OUTSIDE = "outside"
    IN_CONVERSATION = "in_conversation"
    DEFAULT_CONVERSATION_TIMEOUT = 600  # 10分钟


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

        # ── 对话参与状态机（v2.2） ──
        self.conv_state: str = ConvState.OUTSIDE
        self.last_activity_time: float = 0
        self.exit_timer_task: Optional[asyncio.Task] = None

        # 从 persona 读取时间参数
        timing = self.llm.persona.timing
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
        was_outside = (self.conv_state == ConvState.OUTSIDE)
        
        if was_outside:
            # 新对话/回归：进入对话状态，注入提示让 LLM 感知节奏
            self._cancel_exit_timer()
            gap_minutes = int((now - self.last_activity_time) / 60) if self.last_activity_time else 999
            self.conv_state = ConvState.IN_CONVERSATION
            self.last_activity_time = now
            
            # 注入 sys 提示：告诉 LLM 这是新对话首条消息，带上真实延迟数据
            timing = self.llm.persona.timing
            delay_info = self._build_delay_hint(timing)
            context_hint = (
                f"[系统提示] 这是新对话的第一条消息"
                f"（距上次对话{'已过' + str(gap_minutes) + '分钟' if gap_minutes < 999 else '很久'}）。\n"
                f"{delay_info}\n"
                f"请在第一条消息的 `t` 值中体现自然的回复延迟。"
                f"每次都要随机选择不同的值，不要每次都一样！"
            )
            self.context.append({"role": "system", "content": context_hint})
            self.app.on_status(f"💬 新对话 (gap={gap_minutes}min)" if gap_minutes < 999 else "💬 新对话")

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
    def _build_delay_hint(timing: dict) -> str:
        """从 persona timing 提取范围，构建带上限的延迟提示"""
        parts = ["你的回复延迟（真人参考 + 模拟上限）："]
        
        # 真实数据作为参考
        ad = timing.get("attention_delay", {})
        profiles = ad.get("profiles", {})
        if profiles:
            p = next(iter(profiles.values()))
            mn, mx, p50 = p.get("min", 0), p.get("max", 0), p.get("p50", 0)
            if p50:
                parts.append(f"- 真人数据：中位 {int(p50)}s，范围 {int(mn)}s-{int(mx)}s （仅供参考，不需要等这么久）")
        
        # 模拟用的实际范围（从 first_reply_gap 取，没有则默认）
        fr = timing.get("first_reply_gap", {})
        sim_min = fr.get("min", 2)
        sim_max = min(fr.get("max", 8), 120)
        parts.append(f"- 请在 {sim_min}s-{sim_max}s 之间随机选第一条 t，每次不同")
        
        # First reply gap (对话中第一条回复)
        fr = timing.get("first_reply_gap", {})
        if fr.get("normal"):
            parts.append(f"- 看到消息后思考 {fr.get('min', 1)}-{fr.get('max', 5)}s 再打字（目标约 {fr.get('normal', 3)}s）")
        
        # Fragment delay (碎片间隔)
        frag = timing.get("fragmentation", {})
        fd = frag.get("fragment_delay", {})
        if fd.get("same_thought"):
            st = fd["same_thought"]
            parts.append(f"- 同话题碎片间隔 {st[0]}-{st[1]}s")
        if fd.get("new_thought"):
            nt = fd["new_thought"]
            parts.append(f"- 换话题间隔 {nt[0]}-{nt[1]}s")
        
        return "\n".join(parts)

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
        self._cancel_exit_timer()
