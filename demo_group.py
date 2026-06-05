"""
多角色群聊 Demo — 所有消息走同一个 burst 流程
"""

import asyncio
import os
import sys
import time
from datetime import datetime

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Header, Footer, Input, Static, RichLog
from textual.binding import Binding

from burstchat.llm import LLMClient
from burstchat.scheduler import Scheduler

COLORS = ["#FF6B8A", "#4ECDC4", "#FFD93D", "#6BCB77", "#4D96FF"]


class GroupChat:
    """管理多个角色，消息路由 + 批量投递"""

    def __init__(self, api_key: str, personas: list[str], user_name: str, app):
        self.app = app
        self.user_name = user_name
        self.personas: dict[str, Scheduler] = {}
        self._colors = {}
        self._buffers: dict[str, list[str]] = {}

        for i, name in enumerate(personas):
            llm = LLMClient(api_key, persona=name)
            sched = Scheduler(llm, app, name=name)
            self.personas[name] = sched
            self._colors[name] = COLORS[i % len(COLORS)]

        # Wire callbacks
        for name, sched in self.personas.items():
            sched._on_dispatch = self._make_on_dispatch(name)
            sched._on_flush = self._make_on_flush(name)

    def _make_on_dispatch(self, name: str):
        def cb(sender, text, ts):
            self._buffers.setdefault(name, []).append(text)
        return cb

    def _make_on_flush(self, name: str):
        def cb():
            texts = self._buffers.pop(name, [])
            for text in texts:
                for other, sched in self.personas.items():
                    if other != name:
                        asyncio.create_task(sched.on_user_message(f"[{name}]: {text}"))
        return cb

    async def on_user_input(self, text: str):
        """用户发言 → 广播给所有角色"""
        self.app.on_message(self.user_name, text, time.time())
        self._buffers.clear()  # 清空旧缓冲
        for name, sched in self.personas.items():
            await sched.on_user_message(text)

    async def shutdown(self):
        for _, sched in self.personas.items():
            await sched.shutdown()


# ── TUI ─────────────────────────────────────────────────────

class GroupChatApp(App):
    CSS = """
    Screen { layout: grid; grid-rows: auto 1fr auto auto; }
    #status-bar { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #chat-log { height: 100%; border: solid $primary; padding: 0 1; }
    #input-area { height: auto; min-height: 3; border: solid $primary; padding: 0 1; }
    #input { width: 100%; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "退出", show=True),
        Binding("escape", "focus_input", "输入", show=True),
    ]

    def __init__(self, api_key: str, personas: list[str], user_name: str = "我"):
        super().__init__()
        self.group = GroupChat(api_key, personas, user_name, self)
        self._user = user_name
        self._colors = self.group._colors

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("  👥 群聊启动中...", id="status-bar")
        yield RichLog(id="chat-log", highlight=True, markup=True)
        yield Container(
            Input(placeholder="打字... (Enter 发送)", id="input"),
            id="input-area",
        )
        yield Footer()

    def on_mount(self):
        names = "  ".join(f"[{self._colors[n]}]{n}[/]" for n in self._colors)
        self._log(f"[dim]👥 [{self._user}] {names}[/dim]")
        self.on_status("✅ 就绪")

    def on_message(self, role: str, text: str, ts: float):
        t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        color = self._colors.get(role)
        if role == self._user:
            self._log(f"[dim]{t}[/dim] [bold white]{role}:[/] {text}")
        elif color:
            self._log(f"[dim]{t}[/dim] [{color}]{role}:[/] {text}")
        else:
            self._log(f"[dim]{t}[/dim] {role}: {text}")

    def on_status(self, status: str):
        try:
            self.query_one("#status-bar", Static).update(f"  {status}")
        except Exception:
            pass

    def _log(self, markup: str):
        try:
            self.query_one("#chat-log", RichLog).write(markup)
        except Exception:
            pass

    async def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        event.input.value = ""
        if text:
            await self.group.on_user_input(text)

    def action_focus_input(self):
        self.query_one("#input", Input).focus()

    @work(exclusive=True)
    async def _do_shutdown(self):
        await self.group.shutdown()


# ── Main ────────────────────────────────────────────────────

def _load_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    print("❌ 请设置 DEEPSEEK_API_KEY")
    sys.exit(1)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="多角色群聊 Demo")
    parser.add_argument("--personas", nargs="+", default=["xiaoye", "achen"])
    parser.add_argument("--user", default="我", help="你的名字")
    args = parser.parse_args()

    api_key = _load_api_key()
    app = GroupChatApp(api_key, args.personas, user_name=args.user)
    app.run()


if __name__ == "__main__":
    main()

