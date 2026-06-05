#!/usr/bin/env python3
"""
聊天记录时间画像提取器

从聊天记录中提取一个人的实际时间行为模式，输出可直接填入 persona.json 的 timing 配置。

分析维度：
  1. attention_delay  — 长时间沉默后第一条回复的延迟（判断看手机习惯）
  2. in_conversation_reply  — 对话中回复消息的延迟（打字+思考速度）
  3. fragmentation  — 消息拆分习惯（连续发几条、碎片间隔）
  4. typing_speed  — 纯打字速度（从碎片间隔中分离出来）

用法：
  python timing_analyzer.py -i messages.txt -t "目标名"

输出：JSON timing 配置，可直接粘贴到 persona.json 的 timing 字段
"""

import argparse
import json
import re
import sys
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── 消息解析 ──────────────────────────────────────────────────────────────────

class Message:
    __slots__ = ("timestamp", "sender", "content", "is_them")
    def __init__(self, timestamp: str, sender: str, content: str, is_them: bool):
        self.timestamp = timestamp
        self.sender = sender
        self.content = content.strip()
        self.is_them = is_them


def parse_messages(text: str, target: str) -> list[Message]:
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


def parse_time(ts: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(ts[:19], fmt)
        except ValueError:
            continue
    return None


# ─── 时间分析 ──────────────────────────────────────────────────────────────────

def analyze_timing(messages: list[Message], target: str) -> dict:
    """核心分析：从聊天记录提取时间行为模式"""
    their_msgs = [m for m in messages if m.is_them]
    my_msgs = [m for m in messages if not m.is_them]

    if len(their_msgs) < 10:
        return {"error": f"{target} 的消息太少（{len(their_msgs)} 条），需要至少 10 条"}

    # ── 1. Attention Delay: 长时间沉默后对方第一条回复的延迟 ──
    # 定义"长时间沉默"：和上一条消息间隔 > 30min
    LONG_GAP_THRESHOLD = 1800  # 30min

    attention_delays = []  # (gap_seconds, reply_delay_seconds, their_first_msg)
    prev_time = None
    prev_sender = None

    for msg in messages:
        curr_time = parse_time(msg.timestamp)
        if curr_time is None:
            continue
        if prev_time is None:
            prev_time = curr_time
            prev_sender = msg.sender
            continue

        gap = (curr_time - prev_time).total_seconds()

        # 检测：长间隔后，对方发的第一条消息
        if gap > LONG_GAP_THRESHOLD and msg.is_them:
            attention_delays.append({
                "gap_before": gap,
                "reply_delay": gap,  # 对于"第一条消息"，延迟就是 gap 本身
                "first_msg": msg.content[:30],
                "date": msg.timestamp[:10],
            })

        prev_time = curr_time
        prev_sender = msg.sender

    # ── 2. In-Conversation Reply Speed: 对话中回复对方的延迟 ──
    # 定义"对话中"：上一条消息是自己发的，且间隔 < 5min
    IN_CONV_THRESHOLD = 300  # 5min — 超过这个就算自然停顿了

    in_conv_delays = []
    for i in range(1, len(messages)):
        curr = messages[i]
        prev = messages[i-1]
        if not curr.is_them:
            continue  # 只看对方的回复
        if prev.is_them:
            continue  # 不看对方连续自说自话

        gap = _time_gap(prev.timestamp, curr.timestamp)
        if gap is None or gap > IN_CONV_THRESHOLD:
            continue  # 不是对话中的快速回复
        if gap < 0.5:
            continue  # 过滤异常值

        in_conv_delays.append(gap)

    # ── 3. Fragmentation: 对方连续发消息的模式 ──
    # 定义"碎片"：对方在 60s 内连发 ≥2 条
    FRAGMENT_WINDOW = 60

    fragments = []
    current_batch = []

    for msg in their_msgs:
        if not current_batch:
            current_batch.append(msg)
        else:
            gap = _time_gap(current_batch[-1].timestamp, msg.timestamp)
            if gap is not None and gap < FRAGMENT_WINDOW:
                current_batch.append(msg)
            else:
                if len(current_batch) >= 2:
                    fragments.append(current_batch)
                current_batch = [msg]

    if len(current_batch) >= 2:
        fragments.append(current_batch)

    # 碎片内部延迟
    fragment_internal_delays = []
    for batch in fragments:
        delays = []
        for j in range(1, len(batch)):
            gap = _time_gap(batch[j-1].timestamp, batch[j].timestamp)
            if gap is not None and gap > 0:
                delays.append(gap)
        if delays:
            fragment_internal_delays.extend(delays)

    # 碎片大小分布
    fragment_sizes = [len(b) for b in fragments]

    # ── 4. Typing Speed: 从碎片间隔推算 ──
    # 碎片间隔 = 打字时间 + 思考时间
    # 如果碎片内容很短（1-3字），间隔主要反映打字+发送速度
    short_frag_delays = []
    for batch in fragments:
        for j in range(1, len(batch)):
            prev_len = len(batch[j-1].content)
            if prev_len <= 4:  # 短消息后的延迟更接近纯打字+发送速度
                gap = _time_gap(batch[j-1].timestamp, batch[j].timestamp)
                if gap is not None and 0.5 < gap < 30:
                    short_frag_delays.append(gap)

    # ── 5. First Reply in Session: 进入对话后第一条回复的延迟 ──
    # 区别于 attention_delay，这是对方已经注意到消息后，第一条回复本身的延迟
    first_reply_delays = []
    for msg in messages:
        if not msg.is_them:
            continue
        for j in range(messages.index(msg) - 1, -1, -1):
            if messages[j].is_them:
                break
            if not messages[j].is_them:
                # 上一条是"我"发的
                gap = _time_gap(messages[j].timestamp, msg.timestamp)
                if gap is not None and 1 < gap < 300:
                    # 检查这条"我"的消息之前是否有长间隔
                    prev_gap = None
                    if j > 0:
                        prev_gap = _time_gap(messages[j-1].timestamp, messages[j].timestamp)
                    if prev_gap is None or prev_gap > LONG_GAP_THRESHOLD:
                        first_reply_delays.append(gap)
                break

    # ── 编译结果 ──
    return {
        "target": target,
        "sample_sizes": {
            "total_their_messages": len(their_msgs),
            "total_my_messages": len(my_msgs),
            "attention_delay_samples": len(attention_delays),
            "in_conv_reply_samples": len(in_conv_delays),
            "fragment_batches": len(fragments),
            "fragment_delay_samples": len(fragment_internal_delays),
        },
        "attention_delay": _summarize_attention(attention_delays),
        "in_conversation_reply": _summarize_reply_delays(in_conv_delays),
        "fragmentation": _summarize_fragmentation(fragments, fragment_internal_delays, fragment_sizes),
        "first_reply": _summarize_reply_delays(first_reply_delays, label="第一条回复"),
        "typing_speed": _summarize_typing(short_frag_delays),
        # 原始数据用于调试
        "_raw_attention_delays": [d["reply_delay"] for d in attention_delays],
        "_raw_in_conv_delays": in_conv_delays,
        "_raw_fragment_sizes": fragment_sizes,
        "_raw_fragment_delays": fragment_internal_delays,
    }


def _time_gap(ts1: str, ts2: str) -> Optional[float]:
    t1 = parse_time(ts1)
    t2 = parse_time(ts2)
    if t1 and t2:
        return (t2 - t1).total_seconds()
    return None


def _summarize_attention(attention_delays: list[dict]) -> dict:
    """总结 attention delay 模式"""
    if not attention_delays:
        return {"profile": "unknown", "note": "数据不足"}

    delays = [d["reply_delay"] for d in attention_delays]
    delays_sorted = sorted(delays)

    p50 = statistics.median(delays_sorted)
    min_d = min(delays)
    max_d = max(delays)

    # 推断 profile
    if p50 < 10:
        profile = "instant"
    elif p50 < 60:
        profile = "fast"
    elif p50 < 300:
        profile = "moderate"
    elif p50 < 1800:
        profile = "slow"
    else:
        profile = "twilight"

    # 典型的新对话段开场消息
    openers = [d["first_msg"] for d in attention_delays[-10:]]

    return {
        "profile": profile,
        "p50_seconds": round(p50),
        "min_seconds": round(min_d),
        "max_seconds": round(max_d),
        "sample_count": len(delays),
        "typical_openers": openers[:5],
        "note": f"对方在长时间沉默后，大约 {_format_seconds(p50)} 开始回复。共 {len(delays)} 个新对话段样本。"
    }


def _summarize_reply_delays(delays: list[float], label: str = "回复") -> dict:
    """总结对话中的回复延迟"""
    if not delays:
        return {"note": f"{label}延迟数据不足"}

    delays_sorted = sorted(delays)
    p50 = statistics.median(delays_sorted)
    p90 = statistics.quantiles(delays_sorted, n=10)[8] if len(delays) >= 10 else max(delays)

    # 分布
    instant = sum(1 for d in delays if d < 5)
    quick = sum(1 for d in delays if 5 <= d < 20)
    moderate = sum(1 for d in delays if 20 <= d < 60)
    slow = sum(1 for d in delays if d >= 60)

    return {
        "p50_seconds": round(p50, 1),
        "p90_seconds": round(p90, 1),
        "min_seconds": round(min(delays), 1),
        "distribution": {
            "秒回（<5s）": f"{instant}/{len(delays)} ({round(instant/len(delays)*100)}%)",
            "快回（5-20s）": f"{quick}/{len(delays)} ({round(quick/len(delays)*100)}%)",
            "中等（20-60s）": f"{moderate}/{len(delays)} ({round(moderate/len(delays)*100)}%)",
            "慢回（>60s）": f"{slow}/{len(delays)} ({round(slow/len(delays)*100)}%)",
        },
        "sample_count": len(delays),
    }


def _summarize_fragmentation(batches: list[list], internal_delays: list[float],
                             sizes: list[int]) -> dict:
    """总结碎片化模式"""
    if not batches:
        return {"mode": "low", "note": "几乎不拆消息"}

    avg_size = sum(sizes) / len(sizes)
    median_size = statistics.median(sizes)

    # 碎片率：碎片消息占总消息的比例
    fragmented_count = sum(sizes)
    total_their = fragmented_count + (0 if not batches else 0)  # placeholder

    # 推断拆分模式
    if avg_size >= 4:
        mode = "high"
    elif avg_size >= 2.5:
        mode = "medium"
    else:
        mode = "low"

    delay_summary = _summarize_reply_delays(internal_delays, label="碎片间") if internal_delays else {"note": "无内部延迟数据"}

    return {
        "mode": mode,
        "avg_fragments_per_burst": round(avg_size, 1),
        "median_fragments_per_burst": round(median_size, 1),
        "max_fragments_in_burst": max(sizes) if sizes else 0,
        "total_bursts": len(batches),
        "sample_burst_sizes": sizes[-10:],  # 最近10次
        "internal_delays": delay_summary,
        "note": f"平均每次连发 {round(avg_size, 1)} 条。{'主谓宾分开发' if mode == 'high' else '中等拆分' if mode == 'medium' else '倾向于一句话说完'}。"
    }


def _summarize_typing(delays: list[float]) -> dict:
    """估算打字速度"""
    if not delays:
        return {"note": "打字速度数据不足"}

    delays_sorted = sorted(delays)
    p50 = statistics.median(delays_sorted)

    if p50 < 2:
        speed = "very_fast"
    elif p50 < 4:
        speed = "fast"
    elif p50 < 7:
        speed = "normal"
    else:
        speed = "slow"

    return {
        "estimated_speed": speed,
        "p50_fragment_gap_seconds": round(p50, 1),
        "min_gap": round(min(delays), 1),
        "sample_count": len(delays),
        "note": f"短消息之间的间隔约 {round(p50, 1)}s（含打字+发送）。"
    }


def _format_seconds(s: float) -> str:
    if s < 60:
        return f"{round(s)}秒"
    elif s < 3600:
        return f"{round(s/60)}分钟"
    elif s < 86400:
        return f"{round(s/3600)}小时"
    return f"{round(s/86400)}天"


# ─── 输出格式化 ────────────────────────────────────────────────────────────────

def generate_timing_config(data: dict) -> str:
    """生成可直接粘贴到 persona.json 的 timing 配置"""
    if "error" in data:
        return json.dumps(data, ensure_ascii=False, indent=2)

    ad = data["attention_delay"]
    icr = data["in_conversation_reply"]
    frag = data["fragmentation"]
    fr = data["first_reply"]
    ts = data["typing_speed"]

    # 提取碎片延迟的具体范围
    frag_delays = frag.get("internal_delays", {})
    same_thought_min = 2
    same_thought_max = 4
    new_thought_min = 6
    new_thought_max = 9

    if frag_delays.get("p50_seconds"):
        p50 = frag_delays["p50_seconds"]
        same_thought_min = max(1, round(p50 * 0.5))
        same_thought_max = max(2, round(p50 * 1.5))
        new_thought_min = max(3, round(p50 * 2))
        new_thought_max = max(4, round(p50 * 3))

    config = {
        "_generated_by": "timing_analyzer.py",
        "_based_on": f"{data['sample_sizes']['total_their_messages']} 条消息的分析",
        "_summary": f"看手机习惯: {ad.get('profile','?')} ({ad.get('p50_seconds','?')}s) | "
                    f"对话中回复: {icr.get('p50_seconds','?')}s | "
                    f"碎片化: {frag.get('mode','?')} ({frag.get('avg_fragments_per_burst','?')}条/次)",

        "attention_delay": {
            "_doc": "收到消息后注意到消息的延迟——从实际数据分析得出",
            "profile": ad.get("profile", "moderate"),
            "profiles": {
                "from_data": {
                    "min": ad.get("min_seconds", 5),
                    "max": ad.get("max_seconds", 60),
                    "p50": ad.get("p50_seconds", 30),
                    "desc": ad.get("note", "")
                }
            }
        },
        "conversation_timeout": 600,
        "first_reply_gap": {
            "_doc": "对话中第一条回复的典型延迟",
            "normal": round(fr.get("p50_seconds", 3)),
            "min": round(fr.get("min_seconds", 1)) if fr.get("min_seconds") else 1,
            "max": round(fr.get("p90_seconds", 8)) if fr.get("p90_seconds") else 8,
            "unit": "seconds"
        },
        "fragmentation": {
            "_doc": f"消息拆分习惯：{frag.get('note','')}",
            "mode": frag.get("mode", "medium"),
            "avg_per_burst": frag.get("avg_fragments_per_burst", 2),
            "max_per_burst": frag.get("max_fragments_in_burst", 4),
            "fragment_delay": {
                "same_thought": [same_thought_min, same_thought_max],
                "new_thought": [new_thought_min, new_thought_max],
                "unit": "seconds"
            }
        },
        "typing_speed": {
            "estimated": ts.get("estimated_speed", "normal"),
            "p50_gap_seconds": ts.get("p50_fragment_gap_seconds", 3),
            "_doc": "从短消息碎片间隔估算的纯打字速度"
        }
    }

    return json.dumps(config, ensure_ascii=False, indent=2)


def generate_report(data: dict, target: str) -> str:
    """生成可读的 Markdown 报告"""
    if "error" in data:
        return f"# 错误\n\n{data['error']}"

    lines = [
        f"# {target} 的时间行为画像",
        f"",
        f"> 基于 {data['sample_sizes']['total_their_messages']} 条消息的统计分析",
        "",
    ]

    # Attention Delay
    ad = data["attention_delay"]
    lines.append("## 📱 看手机习惯（Attention Delay）")
    lines.append("")
    if ad.get("profile") != "unknown":
        lines.append(f"- **类型：{ad['profile']}**")
        lines.append(f"- 中位延迟：{_format_seconds(ad['p50_seconds'])}")
        lines.append(f"- 范围：{_format_seconds(ad['min_seconds'])} ~ {_format_seconds(ad['max_seconds'])}")
        lines.append(f"- 样本：{ad['sample_count']} 个新对话段")
        lines.append(f"- 评估：{ad.get('note', '')}")
    else:
        lines.append(f"- {ad.get('note', '数据不足')}")
    lines.append("")

    # In-Conversation Reply
    icr = data["in_conversation_reply"]
    lines.append("## 💬 对话中回复速度")
    lines.append("")
    if icr.get("p50_seconds"):
        lines.append(f"- 中位延迟：**{icr['p50_seconds']}s**")
        lines.append(f"- P90 延迟：{icr['p90_seconds']}s")
        lines.append(f"- 分布：")
        for label, val in icr.get("distribution", {}).items():
            lines.append(f"  - {label}：{val}")
    else:
        lines.append(f"- {icr.get('note', '数据不足')}")
    lines.append("")

    # Fragmentation
    frag = data["fragmentation"]
    lines.append("## ✂️ 消息拆分习惯")
    lines.append("")
    lines.append(f"- 拆分模式：**{frag.get('mode', '?')}**")
    lines.append(f"- 平均每次连发：{frag.get('avg_fragments_per_burst', '?')} 条")
    lines.append(f"- 最多一次连发：{frag.get('max_fragments_in_burst', '?')} 条")
    lines.append(f"- 碎片流总数：{frag.get('total_bursts', '?')} 次")
    lines.append(f"- {frag.get('note', '')}")

    fd = frag.get("internal_delays", {})
    if fd.get("p50_seconds"):
        lines.append(f"- 碎片间中位间隔：{fd['p50_seconds']}s")
    lines.append("")

    # Typing Speed
    ts = data["typing_speed"]
    lines.append("## ⌨️ 打字速度")
    lines.append("")
    lines.append(f"- 估算：**{ts.get('estimated_speed', '?')}**")
    lines.append(f"- 短消息间中位间隔：{ts.get('p50_fragment_gap_seconds', '?')}s")
    lines.append(f"- {ts.get('note', '')}")
    lines.append("")

    # First Reply
    fr = data["first_reply"]
    lines.append("## 🏁 进入对话后第一条回复")
    lines.append("")
    if fr.get("p50_seconds"):
        lines.append(f"- 中位延迟：**{fr['p50_seconds']}s**")
        if fr.get("distribution"):
            lines.append(f"- 分布：")
            for label, val in fr.get("distribution", {}).items():
                lines.append(f"  - {label}：{val}")
    else:
        lines.append(f"- {fr.get('note', '数据不足')}")
    lines.append("")

    # JSON 配置
    lines.append("---")
    lines.append("")
    lines.append("## 📋 可直接使用的 timing 配置")
    lines.append("")
    lines.append("```json")
    lines.append(generate_timing_config(data))
    lines.append("```")

    return "\n".join(lines)


# ─── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="聊天记录时间画像提取器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python timing_analyzer.py -i messages.txt -t "张三"
  python timing_analyzer.py -i messages.txt -t "张三" --json -o timing.json
  python timing_analyzer.py -i messages.txt -t "张三" -o report.md
        """
    )
    parser.add_argument("--input", "-i", required=True, help="聊天记录文件")
    parser.add_argument("--target", "-t", required=True, help="要分析的人名")
    parser.add_argument("--output", "-o", help="输出文件路径（默认打印到终端）")
    parser.add_argument("--json", "-j", action="store_true", help="只输出 JSON timing 配置")

    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误：文件不存在 — {args.input}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    messages = parse_messages(text, args.target)
    if not messages:
        print("错误：未能解析消息", file=sys.stderr)
        sys.exit(1)

    data = analyze_timing(messages, args.target)
    if "error" in data:
        print(f"错误：{data['error']}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        output = generate_timing_config(data)
    else:
        output = generate_report(data, args.target)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"输出 → {args.output}")
    else:
        print()
        print(output)


if __name__ == "__main__":
    main()
