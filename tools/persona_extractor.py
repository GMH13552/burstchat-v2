#!/usr/bin/env python3
"""
通用聊天记录人物画像提取器（LLM 版）

从聊天记录中调用 DeepSeek 进行深度定性分析，
输出类似 ex-skill 六层结构的结构化 persona 报告。

用法：
  # 基础用法
  python persona_extractor.py -i messages.txt -t "目标名" -o persona.md

  # 附带用户标签（更准）
  python persona_extractor.py -i messages.txt -t "目标名" \
      --tags "焦虑型,冷战派,嘴硬心软" \
      --impression "ta 平时话不多但很靠谱" \
      -o persona.md

  # 指定 API key（或设置环境变量 DEEPSEEK_API_KEY）
  python persona_extractor.py -i messages.txt -t "目标名" -k sk-xxx

  # 也支持直接读 chat_summarizer.py 的 JSON 输出
  python persona_extractor.py -i stats.json --format stats -t "目标名" -o persona.md

底层调用: DeepSeek Chat API (model: deepseek-chat)
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── 消息解析（复用 chat_summarizer 的逻辑） ────────────────────────────────────

class Message:
    __slots__ = ("timestamp", "sender", "content", "is_them")
    def __init__(self, timestamp: str, sender: str, content: str, is_them: bool):
        self.timestamp = timestamp
        self.sender = sender
        self.content = content.strip()
        self.is_them = is_them


def parse_wechat_parser_output(text: str, target: str) -> list[Message]:
    pattern = re.compile(
        r"^\[(?P<time>[^\]]+)\]\s*(?P<sender>[^:]+)[:：]\s*(?P<content>.+)$"
    )
    target_lower = target.lower()
    messages = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = pattern.match(line)
        if not m:
            continue
        sender = m.group("sender").strip()
        content = m.group("content").strip()
        ts = m.group("time").strip()
        messages.append(Message(ts, sender, content, target_lower in sender.lower()))
    return messages


def parse_generic(text: str, target: str) -> list[Message]:
    return parse_wechat_parser_output(text, target)


# ─── 数据预处理 ────────────────────────────────────────────────────────────────

def preprocess(messages: list[Message], target: str, max_chars: int = 24000) -> dict:
    """
    预处理聊天数据，为 LLM 分析准备。
    返回结构化的分析上下文，包括统计摘要和分类消息。
    """
    their_msgs = [m for m in messages if m.is_them]
    my_msgs = [m for m in messages if not m.is_them]

    if not their_msgs:
        return {"error": f"未找到 {target} 的消息"}

    total_chars = sum(len(m.content) for m in their_msgs)

    # ── 消息分类 ──
    conflict_kw = ["生气", "吵架", "分手", "算了", "随便", "不想说了", "别找我",
                    "不要了", "受够了", "不可能", "冷战", "无所谓", "就这样吧",
                    "对不起", "我错了", "抱歉", "你走", "烦死了", "滚"]
    sweet_kw = ["想你", "喜欢", "爱", "宝", "晚安", "早安", "吃了吗", "在干嘛",
                 "么么", "想见你", "开心", "幸福", "乖", "抱抱", "贴贴"]
    long_kw = []  # 用长度判断

    conflict_msgs = []
    sweet_msgs = []
    long_msgs = []
    daily_msgs = []

    for m in their_msgs:
        c = m.content
        if any(kw in c for kw in conflict_kw):
            conflict_msgs.append(m)
        elif any(kw in c for kw in sweet_kw):
            sweet_msgs.append(m)
        elif len(c) > 50:
            long_msgs.append(m)
        else:
            daily_msgs.append(m)

    # ── 采样策略 ──
    # 优先保留：全部冲突消息 → 全部甜蜜消息 → 全部长消息 → 日常消息采样
    sampled_their = []
    remaining = max_chars

    def add_batch(msgs, label, budget):
        added = []
        chars = 0
        for m in msgs:
            line = f"[{m.timestamp}] {target}: {m.content}"
            if chars + len(line) > budget:
                break
            added.append(line)
            chars += len(line)
        return added, budget - chars

    # 最大化利用 token 预算，按重要性分配
    conflict_budget = min(remaining // 2, 6000)
    added, remaining = add_batch(conflict_msgs, "conflict", conflict_budget)
    lines = added
    remaining += (conflict_budget - (conflict_budget - remaining))

    added, remaining = add_batch(sweet_msgs, "sweet", min(remaining, 4000))
    lines += added

    added, remaining = add_batch(long_msgs, "long", min(remaining, 4000))
    lines += added

    # 剩余预算给日常消息（均匀采样）
    if daily_msgs and remaining > 2000:
        step = max(1, len(daily_msgs) // (remaining // 200))
        sampled_daily = daily_msgs[::step]
        added, _ = add_batch(sampled_daily, "daily", remaining)
        lines += added

    # 也加入用户方消息的上下文（最近的）
    context_lines = []
    for m in my_msgs[-50:]:
        context_lines.append(f"[{m.timestamp}] 我: {m.content}")

    # ── 统计摘要 ──
    stats = compute_stats(their_msgs, my_msgs, messages)

    return {
        "target": target,
        "total_their_msgs": len(their_msgs),
        "total_my_msgs": len(my_msgs),
        "date_range": f"{their_msgs[0].timestamp} ~ {their_msgs[-1].timestamp}" if their_msgs else "N/A",
        "stats": stats,
        "sampled_messages": lines,
        "recent_context": context_lines,
        "sufficiency": "充足" if len(their_msgs) >= 200 else ("一般" if len(their_msgs) >= 50 else "不足"),
    }


def compute_stats(their_msgs: list[Message], my_msgs: list[Message],
                  all_msgs: list[Message]) -> dict:
    """基础统计（给 LLM 做参考）"""
    avg_len = sum(len(m.content) for m in their_msgs) / max(1, len(their_msgs))

    # emoji 计数
    emoji_pattern = re.compile(
        r"([\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        r"\U0001F1E0-\U0001F1FF\u2600-\u27BF\u200d\u2640-\u2642\u3030\u303d"
        r"\U0001F900-\U0001F9FF\U0001FA00-\U0001FAFF\u3297\u3299])"
    )
    from collections import Counter
    emoji_counter = Counter()
    for m in their_msgs:
        for e in emoji_pattern.findall(m.content):
            emoji_counter[e] += 1

    return {
        "avg_chars_per_msg": round(avg_len, 1),
        "short_msg_ratio": f"{round(sum(1 for m in their_msgs if len(m.content) <= 5) / max(1, len(their_msgs)) * 100)}%",
        "long_msg_ratio": f"{round(sum(1 for m in their_msgs if len(m.content) > 20) / max(1, len(their_msgs)) * 100)}%",
        "top_emojis": [f"{e}({n}次)" for e, n in emoji_counter.most_common(5)],
        "their_total": len(their_msgs),
        "my_total": len(my_msgs),
        "ratio": f"TA {round(len(their_msgs)/max(1,len(all_msgs))*100)}% / 我 {round(len(my_msgs)/max(1,len(all_msgs))*100)}%",
        "period_ending_ratio": f"{round(sum(1 for m in their_msgs if m.content.rstrip().endswith('。')) / max(1, len(their_msgs)) * 100)}%",
    }


# ─── LLM 调用 ──────────────────────────────────────────────────────────────────

ANALYSIS_SYSTEM_PROMPT = """你是一个聊天记录分析专家。你的任务是从微信聊天记录中提取一个人的完整说话画像。

## 输出格式

请严格按照以下六个维度输出分析结果。每个维度都要给出**具体行为描述 + 原文引用**。

---

### 1. 表达指纹

**口头禅与高频词**（列出 5-10 个，每个附在什么场景用）
**句式特征**（短句流/小作文/混合、是否用句号、感叹号/省略号习惯）
**emoji/表情使用**（最常用的是什么、分别什么场景用）
**回复节奏**（秒回还是轮回、什么情况下变慢）

### 2. 情绪表达模式

**TA 如何表达在乎**（说出来/行动暗示；引用 1-2 个典型原文）
**TA 如何表达不满**（直接说/冷战/反话/消失；引用原文）
**TA 如何道歉**（会直接说"对不起"还是绕弯子）
**TA 如何说"喜欢"**（什么情境下说、主动还是被逼出来的）

### 3. 冲突行为链

**触发点**（TA 容易被什么激怒）
**典型冲突序列**：被触发 → 第一反应 → 怎么升级 → 怎么收场
**冷战模式**（会冷战吗、冷战时怎么处理消息、通常谁先开口）
**和解信号**（TA 发出"没事了"的方式，不一定是道歉）

### 4. 关系角色行为

**TA 主动的情况**（频繁/偶尔/很少；主动时通常因为什么）
**TA 消失的情况**（有没有预兆、重新出现时怎么开口）
**TA 的边界**（哪些话题不接、怎么拒绝）

### 5. 关系动态总结

用 3-5 句话描述 TA 和你的关系模式：
- TA 在关系中扮演什么角色
- TA 对你的态度（亲近/有距离/忽冷忽热）
- 消息量的主动/被动比例

### 6. 一句话总结

用一句话概括这个人的聊天风格和核心性格特征。

---

## 分析原则

1. **每条结论必须有原文引用支撑**。引用格式：`"原文"`。
2. 不要写形容词（如"性格温柔"），要写具体行为（如“生气时不直接说，而是回'哦'然后消失”）。
3. 如果某个维度信息不足，标注 `（聊天记录不足，以下为推断）`。
4. 标注口头禅和 emoji 时要说明使用场景：不只是统计频率，还要说明什么情况下用。
5. 注意区分 TA **对你**的说话风格 vs TA 对其他人的说话风格（如果数据中有多人对话）。
6. 使用中文输出。"""


def build_analysis_prompt(data: dict, tags: str, impression: str) -> str:
    """构建发送给 LLM 的分析 prompt"""
    parts = []

    # 基础信息
    parts.append(f"## 分析对象：{data['target']}")
    parts.append(f"消息数：{data['total_their_msgs']} 条（TA）+ {data['total_my_msgs']} 条（我）")
    parts.append(f"时间范围：{data['date_range']}")
    parts.append(f"样本充分度：{data['sufficiency']}")
    parts.append("")

    # 用户标签
    if tags or impression:
        parts.append("## 用户提供的背景")
        if tags:
            parts.append(f"- 标签：{tags}")
        if impression:
            parts.append(f"- 描述：{impression}")
        parts.append("（注意：手动标签优先于聊天记录分析结论）")
        parts.append("")

    # 统计参考
    parts.append("## 统计参考")
    for k, v in data["stats"].items():
        parts.append(f"- {k}: {v}")
    parts.append("")

    # 聊天记录
    parts.append("## TA 的消息记录（重要性排序采样）")
    parts.append("")
    for line in data["sampled_messages"]:
        parts.append(line)
    parts.append("")

    # 最近上下文
    parts.append("## 最近的对话上下文（我发的消息）")
    parts.append("")
    for line in data["recent_context"]:
        parts.append(line)

    return "\n".join(parts)


async def call_deepseek(prompt: str, api_key: str, model: str = "deepseek-chat") -> str:
    """调用 DeepSeek API"""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
        max_tokens=4000,
    )

    return response.choices[0].message.content


# ─── 输出格式化 ────────────────────────────────────────────────────────────────

def format_output(llm_response: str, data: dict, tags: str, impression: str) -> str:
    """将 LLM 输出格式化为完整报告"""
    lines = [
        f"# {data['target']} 的深度画像",
        f"",
        f"> 基于 {data['total_their_msgs']} 条消息的 LLM 定性分析",
        f"> 时间范围：{data['date_range']}",
        f"> 分析时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 样本充分度：**{data['sufficiency']}**",
    ]
    if tags:
        lines.append(f"> 用户标签：{tags}")
    if impression:
        lines.append(f"> 用户印象：{impression}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 嵌入 LLM 的分析结果
    lines.append(llm_response)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*本报告由 persona_extractor (DeepSeek) 自动生成。*")

    return "\n".join(lines)


# ─── 主程序 ────────────────────────────────────────────────────────────────────

async def main_async(args):
    # 读取输入
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误：文件不存在 — {args.input}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    # 解析消息
    if args.format == "stats":
        # 直接读 chat_summarizer.py 的 JSON 输出
        stats_data = json.loads(text)
        # TODO: stats 格式需要包含原始消息
        print("stats 格式暂不支持，请使用原始聊天记录格式", file=sys.stderr)
        sys.exit(1)
    else:
        messages = parse_generic(text, args.target)

    if not messages:
        print(f"错误：未能解析消息。请检查格式。", file=sys.stderr)
        sys.exit(1)

    print(f"解析完成：{len(messages)} 条消息，TA: {sum(1 for m in messages if m.is_them)} 条")

    # 预处理
    data = preprocess(messages, args.target, max_chars=args.max_chars)
    if "error" in data:
        print(f"错误：{data['error']}", file=sys.stderr)
        sys.exit(1)

    print(f"预处理完成：采样 {len(data['sampled_messages'])} 条消息发送给 LLM")

    # 获取 API key
    api_key = args.api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("错误：需要 DeepSeek API key。通过 -k 指定或设置 DEEPSEEK_API_KEY 环境变量", file=sys.stderr)
        sys.exit(1)

    # 构建 prompt
    prompt = build_analysis_prompt(data, args.tags or "", args.impression or "")
    print(f"Prompt 构建完成：{len(prompt)} 字符，调用 DeepSeek...")

    # 调用 LLM
    try:
        llm_response = await call_deepseek(prompt, api_key, args.model)
    except Exception as e:
        print(f"LLM 调用失败：{e}", file=sys.stderr)
        sys.exit(1)

    # 输出
    output = format_output(llm_response, data, args.tags or "", args.impression or "")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"报告已输出到：{args.output}")
    else:
        print()
        print(output)


def main():
    parser = argparse.ArgumentParser(
        description="聊天记录人物画像提取器（LLM 深度分析版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python persona_extractor.py -i messages.txt -t "张三" -o persona.md
  python persona_extractor.py -i messages.txt -t "张三" --tags "焦虑型,冷战派" -o persona.md
  python persona_extractor.py -i messages.txt -t "张三" -k sk-xxxxx -o persona.md

环境变量：
  DEEPSEEK_API_KEY  DeepSeek API 密钥（也可通过 -k 指定）
        """
    )
    parser.add_argument("--input", "-i", required=True, help="聊天记录文件（wechat_parser.py 输出格式）")
    parser.add_argument("--target", "-t", required=True, help="要分析的人名")
    parser.add_argument("--output", "-o", help="输出文件路径（默认打印到终端）")
    parser.add_argument("--tags", help="逗号分隔的标签，如 '焦虑型,冷战派,嘴硬心软'")
    parser.add_argument("--impression", help="你对 TA 的主观印象（一句话）")
    parser.add_argument("--api-key", "-k", help="DeepSeek API key")
    parser.add_argument("--model", "-m", default="deepseek-chat", help="DeepSeek 模型名")
    parser.add_argument("--format", "-f", default="auto", choices=["auto", "stats"],
                        help="输入格式（默认自动检测）")
    parser.add_argument("--max-chars", type=int, default=24000,
                        help="发送给 LLM 的最大字符数（默认 24000）")

    args = parser.parse_args()

    import asyncio
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
