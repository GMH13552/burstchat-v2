"""
Textual TUI 前端
"""

import time
from datetime import datetime

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Header, Footer, Input, Static, RichLog
from textual.binding import Binding

from .llm import LLMClient
from .scheduler import Scheduler


class CompanionApp(App):
    """拟人情感陪伴 AI — TUI"""

    CSS = """
    Screen {
        layout: grid;
        grid-rows: auto 1fr auto auto;
    }
    #status-bar {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
        text-style: italic;
    }
    #chat-log {
        height: 100%;
        border: solid $primary;
        padding: 0 1;
    }
    #input-area {
        height: auto;
        min-height: 3;
        border: solid $primary;
        padding: 0 1;
    }
    #input {
        width: 100%;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "退出", show=True),
        Binding("escape", "focus_input", "输入", show=True),
    ]

    def __init__(self, api_key: str, persona: str = "xiaoye", model: str = "deepseek-chat"):
        super().__init__()
        self.llm = LLMClient(api_key, persona=persona, model=model)
        self.scheduler = Scheduler(self.llm, self)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("  🚀 启动中...", id="status-bar")
        yield RichLog(id="chat-log", highlight=True, markup=True)
        yield Container(
            Input(placeholder="打字聊天... (Enter 发送, Ctrl+C 退出)", id="input"),
            id="input-area",
        )
        yield Footer()

    def on_mount(self):
        name = self.llm.persona.name
        self._log(f"[dim]{name}上线了 👋 随便聊点什么吧[/dim]")
        self.on_status("✅ 就绪")

    # ── Callbacks from Scheduler ──────────────────────────

    def on_message(self, role: str, text: str, ts: float):
        t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        if role == "user":
            self._log(f"[dim]{t}[/dim] [bold green]你:[/] {text}")
        else:
            name = self.llm.persona.name
            self._log(f"[dim]{t}[/dim] [bold]{name}:[/] {text}")

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

    # ── Input ──────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        event.input.value = ""
        if text:
            self.on_message("user", text, time.time())
            await self.scheduler.on_user_message(text)

    def action_focus_input(self):
        self.query_one("#input", Input).focus()

    # ── Shutdown ───────────────────────────────────────────

    def on_unmount(self):
        # Schedule async shutdown; Textual's worker handles the event loop
        pass  # handled by scheduler's task cancellation via signal

    @work(exclusive=True)
    async def _do_shutdown(self):
        await self.scheduler.shutdown()

