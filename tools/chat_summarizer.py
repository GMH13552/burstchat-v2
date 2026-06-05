#!/usr/bin/env python3
"""
通用聊天记录人物画像提取器

从聊天记录中提取一个人的说话画像（表达指纹、情绪模式、冲突链、角色行为）。
不依赖 LLM，纯本地统计分析。输出结构化 Markdown 报告。

用法：
  # 从 wechat_parser.py 的输出 (txt 格式) 分析
  python chat_summarizer.py --input messages.txt --target "目标人名" --output report.md

  # 从 WeChatMsg 导出的 CSV 分析
  python chat_summarizer.py --input chat.csv --format csv --target "目标人名" --output report.md

  # 直接打印到终端
  python chat_summarizer.py --input messages.txt --target "目标人名"

  # 输出 JSON（供其他程序消费）
  python chat_summarizer.py --input messages.txt --target "目标人名" --json --output report.json

支持的输入格式：
  - wechat_parser.py 的输出（"发送方: 内容" 格式）
  - WeChatMsg 导出 CSV
  - 通用格式（每一行一条消息，含发送方标识和时间戳）
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── 数据结构 ──────────────────────────────────────────────────────────────────

class Message:
    __slots__ = ("timestamp", "sender", "content", "is_them")
    def __init__(self, timestamp: str, sender: str, content: str, is_them: bool):
        self.timestamp = timestamp
        self.sender = sender
        self.content = content.strip()
        self.is_them = is_them


# ─── 解析器 ────────────────────────────────────────────────────────────────────

def parse_wechat_parser_output(text: str, target: str) -> list[Message]:
    """解析 wechat_parser.py 输出的 txt 格式

    格式示例：
      [2024-01-02 10:30:00] 目标名: 消息内容
      [2024-01-02 10:30:05] 我: 消息内容
    """
    messages = []
    # [时间戳] 发送方: 内容
    pattern = re.compile(
        r"^\[(?P<time>[^\]]+)\]\s*(?P<sender>[^:]+)[:：]\s*(?P<content>.+)$"
    )
    target_lower = target.lower()

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
        is_them = target_lower in sender.lower()
        messages.append(Message(ts, sender, content, is_them))

    return messages


def parse_csv(text: str, target: str) -> list[Message]:
    """解析 WeChatMsg 导出的 CSV 格式"""
    import csv
    import io

    messages = []
    target_lower = target.lower()
    reader = csv.DictReader(io.StringIO(text))

    for row in reader:
        # 常见列名：Type, SubType, IsSender, CreateTime, StrContent, StrTalker, Remark, NickName
        content = (row.get("StrContent") or row.get("Content") or row.get("content") or "").strip()
        if not content:
            continue
        if content in ("[图片]", "[语音]", "[视频]", "[文件]", "[撤回了一条消息]"):
            continue

        sender_name = (row.get("Remark") or row.get("NickName") or row.get("StrTalker")
                       or row.get("sender") or row.get("Sender") or "")
        is_sender = str(row.get("IsSender", "0")).strip()
        is_them = target_lower in sender_name.lower() if sender_name else False

        ts = row.get("CreateTime") or row.get("timestamp") or row.get("Time") or ""
        # WeChatMsg 的 CreateTime 是 Unix 时间戳
        if ts and ts.isdigit():
            try:
                ts = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        messages.append(Message(ts, sender_name, content, is_them))

    return messages


def parse_generic(text: str, target: str) -> list[Message]:
    """通用解析：尝试自动识别格式"""
    # 先尝试 wechat_parser 格式
    result = parse_wechat_parser_output(text, target)
    if result:
        return result
    # 再尝试 CSV
    result = parse_csv(text, target)
    if result:
        return result
    return []


# ─── 统计分析 ──────────────────────────────────────────────────────────────────

def analyze(messages: list[Message], target: str) -> dict:
    """核心分析函数，返回结构化的分析结果"""
    their_msgs = [m for m in messages if m.is_them]
    my_msgs = [m for m in messages if not m.is_them]
    total = len(messages)

    if not their_msgs:
        return {"error": f"未找到 {target} 的任何消息，请检查目标名称是否正确"}

    # ── 1. 表达指纹 ──
    # 词频统计（中文按字符级别和词级别）
    all_their_text = " ".join(m.content for m in their_msgs)

    # 高频词（2-6 字短语，出现 ≥3 次）
    phrase_counts = Counter()
    for msg in their_msgs:
        text = msg.content
        for window in (2, 3, 4):
            for i in range(len(text) - window + 1):
                sub = text[i:i+window]
                if not re.search(r"[\u4e00-\u9fff\w]", sub):  # 至少含一个中英文
                    continue
                phrase_counts[sub] += 1
    # 过滤：出现 ≥3 次且长度合理
    high_freq_phrases = [
        (p, n) for p, n in phrase_counts.most_common(100)
        if n >= 3 and 2 <= len(p) <= 8
    ][:20]

    # emoji/颜文字统计
    emoji_pattern = re.compile(
        r"([\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        r"\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251"
        r"\u200d\u2640-\u2642\u2600-\u2B55\u3030\u303d\U0001F900-\U0001F9FF"
        r"\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\u3297\u3299]"
        r"|qwq|QwQ|QWQ|TAT|tat|👉👈|QAQ|qaq|Orz|orz|OTL|otl"
        r"|[ｗｗ]{2,}|[wW]{2,}"
        r"|[\U0001F000-\U0001FFFF]"  # 补充范围
    )
    emoji_counter = Counter()
    for msg in their_msgs:
        found = emoji_pattern.findall(msg.content)
        for e in found:
            emoji_counter[e] += 1
    top_emojis = emoji_counter.most_common(10)

    # 消息长度分布
    lengths = [len(m.content) for m in their_msgs]
    avg_length = sum(lengths) / len(lengths)
    short_count = sum(1 for l in lengths if l <= 5)
    mid_count = sum(1 for l in lengths if 5 < l <= 20)
    long_count = sum(1 for l in lengths if l > 20)

    # 标点习惯
    period_count = sum(1 for m in their_msgs if m.content.rstrip().endswith("。"))
    exclaim_count = sum(1 for m in their_msgs if "！" in m.content or "!" in m.content)
    ellipsis_count = sum(1 for m in their_msgs if "..." in m.content or "…" in m.content)
    question_count = sum(1 for m in their_msgs if "？" in m.content or "?" in m.content)

    expression_fingerprint = {
        "高频短语": high_freq_phrases[:15],
        "招牌表情": top_emojis[:8],
        "消息长度": {
            "平均字数": round(avg_length, 1),
            "碎片型（≤5字）": f"{short_count} 条 ({round(short_count/len(their_msgs)*100)}%)",
            "中等（6-20字）": f"{mid_count} 条 ({round(mid_count/len(their_msgs)*100)}%)",
            "长消息（>20字）": f"{long_count} 条 ({round(long_count/len(their_msgs)*100)}%)",
        },
        "标点习惯": {
            "句号收尾": f"{period_count}/{len(their_msgs)} ({round(period_count/len(their_msgs)*100)}%)",
            "感叹号": f"{exclaim_count}/{len(their_msgs)} ({round(exclaim_count/len(their_msgs)*100)}%)",
            "省略号": f"{ellipsis_count}/{len(their_msgs)} ({round(ellipsis_count/len(their_msgs)*100)}%)",
            "问号": f"{question_count}/{len(their_msgs)} ({round(question_count/len(their_msgs)*100)}%)",
        },
    }

    # ── 2. 回复时间模式 ──
    reply_gaps = _compute_reply_gaps(messages, their_msgs)
    timing_pattern = {
        "平均回复间隔": f"{round(reply_gaps['avg'], 1)}秒" if reply_gaps.get("avg") else "N/A",
        "回复速度分布": {
            "秒回（<30s）": reply_gaps.get("instant", 0),
            "正常（30s-5min）": reply_gaps.get("normal", 0),
            "慢回（5-30min）": reply_gaps.get("slow", 0),
            "轮回（>30min）": reply_gaps.get("very_slow", 0),
        } if reply_gaps else {},
        "对方主动发起的对话段": reply_gaps.get("their_initiative_segments", 0),
        "我方主动发起的对话段": reply_gaps.get("my_initiative_segments", 0),
    }

    # ── 3. 聊天习惯特征 ──
    # 主动发言模式
    initiative_segments = _find_initiative_segments(messages, their_msgs)
    chat_habits = {
        "主动发起对话": {
            "次数": initiative_segments.get("their_initiatives", 0),
            "典型开场词": initiative_segments.get("their_openers", [])[:10],
            "主动比例": f"{round(initiative_segments.get('their_ratio', 0)*100)}%",
        },
        "碎片化程度": {
            "碎片流（短时间内连续多条）": initiative_segments.get("burst_segments", 0),
            "平均每条片段的消息数": round(initiative_segments.get("avg_burst_size", 1), 1),
        },
    }

    # ── 4. 情绪关键词检测 ──
    emotion_stats = _detect_emotion_keywords(their_msgs)

    # ── 5. 冲突信号检测 ──
    conflict_signals = _detect_conflict_signals(messages, their_msgs)

    # ── 6. 关系动态 ──
    relationship_dynamics = _analyze_relationship_dynamics(messages, their_msgs, my_msgs)

    # ── 7. 采样（代表性消息） ──
    samples = _sample_representative_messages(their_msgs)

    return {
        "目标": target,
        "分析时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "总消息数": total,
        "TA的消息数": len(their_msgs),
        "我的消息数": len(my_msgs),
        "样本充分度": "充足" if len(their_msgs) >= 200 else ("一般" if len(their_msgs) >= 50 else "不足"),
        "表达指纹": expression_fingerprint,
        "回复时间模式": timing_pattern,
        "聊天习惯": chat_habits,
        "情绪特征": emotion_stats,
        "冲突信号": conflict_signals,
        "关系动态": relationship_dynamics,
        "代表性消息采样": samples,
    }


# ─── 子分析函数 ────────────────────────────────────────────────────────────────

def _compute_reply_gaps(all_msgs: list[Message], their_msgs: list[Message]) -> dict:
    """计算回复时间间隔"""
    gaps = []
    their_initiatives = 0
    my_initiatives = 0

    for i in range(1, len(all_msgs)):
        prev = all_msgs[i-1]
        curr = all_msgs[i]
        if prev.sender == curr.sender:
            continue  # 同一个人连续发，不算回复间隔

        # 尝试解析时间戳
        gap = _parse_time_gap(prev.timestamp, curr.timestamp)
        if gap is not None and gap > 0:
            gaps.append(gap)

            # 判断谁主动
            if gap > 1800:  # >30分钟，算新对话段
                if curr.is_them:
                    their_initiatives += 1
                else:
                    my_initiatives += 1

    if not gaps:
        return {
            "avg": None,
            "instant": 0, "normal": 0, "slow": 0, "very_slow": 0,
            "their_initiative_segments": their_initiatives,
            "my_initiative_segments": my_initiatives,
        }

    return {
        "avg": sum(gaps) / len(gaps),
        "instant": sum(1 for g in gaps if g < 30),
        "normal": sum(1 for g in gaps if 30 <= g < 300),
        "slow": sum(1 for g in gaps if 300 <= g < 1800),
        "very_slow": sum(1 for g in gaps if g >= 1800),
        "their_initiative_segments": their_initiatives,
        "my_initiative_segments": my_initiatives,
    }


def _parse_time_gap(ts1: str, ts2: str) -> Optional[float]:
    """解析两个时间戳之间的秒数差"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            t1 = datetime.strptime(ts1[:19], fmt)
            t2 = datetime.strptime(ts2[:19], fmt)
            return abs((t2 - t1).total_seconds())
        except ValueError:
            continue
    return None


def _find_initiative_segments(all_msgs: list[Message], their_msgs: list[Message]) -> dict:
    """找主动发起对话的段落和碎片化程度"""
    their_initiatives = 0
    their_openers = []
    burst_segments = 0
    burst_sizes = []

    prev_time = None

    for i, msg in enumerate(all_msgs):
        curr_time = _parse_timestamp(msg.timestamp)
        if curr_time is None:
            continue

        gap = None
        if prev_time is not None:
            gap = (curr_time - prev_time).total_seconds()

        # 间隔 >30min → 新对话段
        if gap is not None and gap > 1800:
            if msg.is_them:
                their_initiatives += 1
                their_openers.append(msg.content[:30])
            # 统计上一段的消息数
            if burst_sizes:
                pass  # 在段结束时统计

        prev_time = curr_time

    # 碎片化检测：同一个人 30s 内连发 ≥2 条
    i = 0
    while i < len(their_msgs):
        j = i + 1
        while j < len(their_msgs):
            gap = _parse_time_gap(their_msgs[j-1].timestamp, their_msgs[j].timestamp)
            if gap is not None and gap < 30:
                j += 1
            else:
                break
        burst_size = j - i
        if burst_size >= 2:
            burst_segments += 1
            burst_sizes.append(burst_size)
        i = j

    return {
        "their_initiatives": their_initiatives,
        "their_openers": their_openers,
        "their_ratio": their_initiatives / max(1, their_initiatives + 1),  # approximate
        "burst_segments": burst_segments,
        "avg_burst_size": round(sum(burst_sizes) / len(burst_sizes), 1) if burst_sizes else 1,
    }


def _parse_timestamp(ts: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(ts[:19], fmt)
        except ValueError:
            continue
    return None


def _detect_emotion_keywords(their_msgs: list[Message]) -> dict:
    """检测情绪关键词"""
    positive_kw = [
        "哈哈", "笑死", "开心", "快乐", "nice", "好耶", "爽", "牛", "绝了",
        "喜欢", "爱", "幸福", "太好了", "恭喜", "厉害", "棒", "完美",
    ]
    negative_kw = [
        "烦", "难过", "哭", "累", "气", "不爽", "无语", "崩溃",
        "想死", "麻了", "抑郁", "焦虑", "害怕", "不开心", "难受",
        "痛苦", "失望", "讨厌", "恶心",
    ]

    pos_count = sum(1 for m in their_msgs if any(kw in m.content for kw in positive_kw))
    neg_count = sum(1 for m in their_msgs if any(kw in m.content for kw in negative_kw))
    total = len(their_msgs)

    # 找具体的情绪用语
    emotion_words = Counter()
    for m in their_msgs:
        for kw in positive_kw + negative_kw:
            if kw in m.content:
                emotion_words[kw] += 1

    return {
        "正面情绪消息": f"{pos_count}/{total} ({round(pos_count/total*100)}%)",
        "负面情绪消息": f"{neg_count}/{total} ({round(neg_count/total*100)}%)",
        "最常见情绪词": emotion_words.most_common(10),
        "情绪基调": "偏积极" if pos_count > neg_count * 1.5 else (
            "偏消极" if neg_count > pos_count * 1.5 else "混合"
        ),
    }


def _detect_conflict_signals(all_msgs: list[Message], their_msgs: list[Message]) -> dict:
    """检测冲突信号"""
    conflict_keywords = [
        "生气", "吵架", "分手", "算了", "随便", "不想说了", "别找我",
        "不要了", "受够了", "不可能", "冷战", "随便你", "你走",
        "无所谓", "爱咋咋", "就这样吧",
    ]
    apology_keywords = [
        "对不起", "我错了", "抱歉", "原谅", "不是故意的",
    ]
    silence_keywords = [
        "哦", "嗯", "行吧", "好吧", "随便",
    ]

    conflict_msgs = []
    apology_msgs = []
    for m in their_msgs:
        if any(kw in m.content for kw in conflict_keywords):
            conflict_msgs.append(m.content)
        if any(kw in m.content for kw in apology_keywords):
            apology_msgs.append(m.content)

    # 检测冷战模式：看是否有连续的单字回复
    silent_replies = sum(1 for m in their_msgs if len(m.content.strip()) <= 2
                         and any(kw in m.content for kw in silence_keywords))

    return {
        "争吵相关消息": len(conflict_msgs),
        "典型冲突用语": conflict_msgs[:5] if conflict_msgs else [],
        "道歉消息": len(apology_msgs),
        "道歉风格": f"{len(apology_msgs)} 条道歉" if apology_msgs else "未检测到直接道歉",
        "简短敷衍（≤2字+特定词）": f"{silent_replies} 条",
        "冲突倾向": "较高" if len(conflict_msgs) > len(their_msgs) * 0.05 else "正常",
    }


def _analyze_relationship_dynamics(all_msgs: list[Message], their_msgs: list[Message],
                                    my_msgs: list[Message]) -> dict:
    """分析关系动态"""
    # 谁说话更多
    their_ratio = len(their_msgs) / max(1, len(all_msgs))
    my_ratio = len(my_msgs) / max(1, len(all_msgs))

    # 主动发起比例（间隔 >30min 后第一条消息的发出者）
    their_init = 0
    my_init = 0
    prev_time = None
    for msg in all_msgs:
        curr_time = _parse_timestamp(msg.timestamp)
        if curr_time is None:
            continue
        gap = None
        if prev_time is not None:
            gap = (curr_time - prev_time).total_seconds()
        if gap is not None and gap > 1800:
            if msg.is_them:
                their_init += 1
            else:
                my_init += 1
        prev_time = curr_time

    total_init = their_init + my_init

    return {
        "消息量比例": f"TA:{round(their_ratio*100)}% / 我:{round(my_ratio*100)}%",
        "主动发起比例": f"TA:{their_init} / 我:{my_init}" if total_init > 0 else "无法判断",
        "关系模式": "TA更主动" if their_init > my_init * 1.5 else (
            "我更主动" if my_init > their_init * 1.5 else "双向"
        ) if total_init > 0 else "数据不足",
        "平均每天消息数": f"{round(len(their_msgs) / max(1, _estimate_days_span(all_msgs)), 1)} 条（TA）",
    }


def _estimate_days_span(all_msgs: list[Message]) -> int:
    """估算对话跨越的天数"""
    if len(all_msgs) < 2:
        return 1
    first = _parse_timestamp(all_msgs[0].timestamp)
    last = _parse_timestamp(all_msgs[-1].timestamp)
    if first and last:
        return max(1, (last - first).days)
    return 1


def _sample_representative_messages(their_msgs: list[Message]) -> dict:
    """提取代表性消息样本"""
    # 取长消息
    long = sorted(their_msgs, key=lambda m: len(m.content), reverse=True)[:5]
    # 随机取短消息
    short = [m for m in their_msgs if len(m.content) <= 10][:5]

    return {
        "长消息": [m.content for m in long],
        "短消息": [m.content for m in short],
    }


# ─── 输出格式化 ────────────────────────────────────────────────────────────────

def format_report(data: dict, target: str) -> str:
    """生成 Markdown 报告"""
    if "error" in data:
        return f"# 错误\n\n{data['error']}"

    lines = [
        f"# {target} 的聊天画像",
        f"",
        f"> 分析时间：{data['分析时间']}  "
        f"> 总消息：{data['总消息数']}  |  TA：{data['TA的消息数']}  |  我：{data['我的消息数']}  "
        f"> 样本充分度：**{data['样本充分度']}**",
        "",
        "---",
        "",
        "## 🗣️ 表达指纹",
        "",
    ]

    ef = data["表达指纹"]

    # 消息长度分布
    lines.append("### 消息长度分布")
    ml = ef["消息长度"]
    lines.append(f"| 类型 | 比例 |")
    lines.append(f"|------|------|")
    lines.append(f"| 碎片型（≤5字） | {ml['碎片型（≤5字）']} |")
    lines.append(f"| 中等（6-20字） | {ml['中等（6-20字）']} |")
    lines.append(f"| 长消息（>20字） | {ml['长消息（>20字）']} |")
    lines.append(f"| 平均 | {ml['平均字数']} 字 |")
    lines.append("")

    # 高频短语
    lines.append("### 高频短语")
    if ef["高频短语"]:
        for phrase, count in ef["高频短语"]:
            lines.append(f"- **「{phrase}」** — {count} 次")
    else:
        lines.append("- 未检测到明显高频短语")
    lines.append("")

    # 招牌表情
    lines.append("### 招牌表情/颜文字")
    if ef["招牌表情"]:
        for emoji, count in ef["招牌表情"]:
            lines.append(f"- {emoji} — {count} 次")
    else:
        lines.append("- 未检测到表情/颜文字")
    lines.append("")

    # 标点习惯
    lines.append("### 标点习惯")
    punct = ef["标点习惯"]
    lines.append(f"- 句号收尾：{punct['句号收尾']}")
    lines.append(f"- 感叹号使用：{punct['感叹号']}")
    lines.append(f"- 省略号使用：{punct['省略号']}")
    lines.append(f"- 问号使用：{punct['问号']}")
    lines.append("")

    # 回复时间模式
    lines.append("---")
    lines.append("")
    lines.append("## ⏱️ 回复时间模式")
    lines.append("")
    rp = data["回复时间模式"]
    if rp.get("平均回复间隔") and rp["平均回复间隔"] != "N/A":
        speed = rp["回复速度分布"]
        lines.append(f"- 平均回复间隔：**{rp['平均回复间隔']}**")
        lines.append(f"- 秒回（<30s）：{speed.get('秒回（<30s）', 0)} 次")
        lines.append(f"- 正常（30s-5min）：{speed.get('正常（30s-5min）', 0)} 次")
        lines.append(f"- 慢回（5-30min）：{speed.get('慢回（5-30min）', 0)} 次")
        lines.append(f"- 轮回（>30min）：{speed.get('轮回（>30min）', 0)} 次")
    else:
        lines.append("- 时间戳数据不足，无法分析")
    lines.append(f"- 对方发起的新对话段：{rp.get('对方主动发起的对话段', 0)}")
    lines.append(f"- 我方发起的新对话段：{rp.get('我方主动发起的对话段', 0)}")
    lines.append("")

    # 聊天习惯
    lines.append("---")
    lines.append("")
    lines.append("## 💬 聊天习惯")
    lines.append("")
    ch = data["聊天习惯"]
    initiative = ch["主动发起对话"]
    lines.append(f"- 主动发起对话：{initiative['次数']} 次")
    lines.append(f"- 主动比例：{initiative['主动比例']}")
    if initiative.get("典型开场词"):
        lines.append(f"- 典型开场词：{', '.join(initiative['典型开场词'])}")
    frag = ch["碎片化程度"]
    lines.append(f"- 碎片流（连续多条）：{frag['碎片流（短时间内连续多条）']} 段")
    lines.append(f"- 平均每段碎片数：{frag['平均每条片段的消息数']}")
    lines.append("")

    # 情绪特征
    lines.append("---")
    lines.append("")
    lines.append("## 😊 情绪特征")
    lines.append("")
    es = data["情绪特征"]
    lines.append(f"- 正面情绪消息：{es['正面情绪消息']}")
    lines.append(f"- 负面情绪消息：{es['负面情绪消息']}")
    lines.append(f"- 情绪基调：**{es['情绪基调']}**")
    if es.get("最常见情绪词"):
        lines.append(f"- 最常见情绪词：{', '.join(f'{w}({n}次)' for w, n in es['最常见情绪词'])}")
    lines.append("")

    # 冲突信号
    lines.append("---")
    lines.append("")
    lines.append("## ⚡ 冲突信号")
    lines.append("")
    cs = data["冲突信号"]
    lines.append(f"- 争吵相关消息：{cs['争吵相关消息']} 条")
    lines.append(f"- 道歉风格：{cs['道歉风格']}")
    lines.append(f"- 简短敷衍消息：{cs['简短敷衍（≤2字+特定词）']}")
    lines.append(f"- 冲突倾向：**{cs['冲突倾向']}**")
    if cs.get("典型冲突用语"):
        lines.append(f"- 典型冲突用语：{', '.join(repr(c) for c in cs['典型冲突用语'])}")
    lines.append("")

    # 关系动态
    lines.append("---")
    lines.append("")
    lines.append("## 🔗 关系动态")
    lines.append("")
    rd = data["关系动态"]
    for key, val in rd.items():
        lines.append(f"- {key}：{val}")
    lines.append("")

    # 代表性消息
    lines.append("---")
    lines.append("")
    lines.append("## 📋 代表性消息采样")
    lines.append("")
    sm = data["代表性消息采样"]
    lines.append("### TA 最长的几条消息")
    for msg in sm["长消息"]:
        lines.append(f"- \"{msg}\"")
    lines.append("")
    lines.append("### TA 日常短消息")
    for msg in sm["短消息"]:
        lines.append(f"- \"{msg}\"")
    lines.append("")

    # 底部提示
    lines.append("---")
    lines.append("")
    lines.append(f"*本报告由 chat_summarizer 自动生成。分析基于 {data['TA的消息数']} 条消息。*")
    if data['样本充分度'] != '充足':
        lines.append(f"*⚠️ 样本偏少（{data['TA的消息数']} 条），画像可信度较低。建议追加更多消息记录。*")

    return "\n".join(lines)


# ─── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="通用聊天记录人物画像提取器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python chat_summarizer.py --input messages.txt --target "目标名" --output report.md
  python chat_summarizer.py --input chat.csv --format csv --target "目标名"
  python chat_summarizer.py --input messages.txt --target "目标名" --json --output report.json
        """
    )
    parser.add_argument("--input", "-i", required=True, help="聊天记录文件路径")
    parser.add_argument("--target", "-t", required=True, help="要分析的人名（聊天记录中显示的名字）")
    parser.add_argument("--output", "-o", help="输出文件路径（默认输出到终端）")
    parser.add_argument("--format", "-f", choices=["auto", "csv", "wechat_parser"],
                        default="auto", help="输入格式（默认自动检测）")
    parser.add_argument("--json", "-j", action="store_true", help="输出 JSON 格式而非 Markdown")

    args = parser.parse_args()

    # 读取输入
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误：文件不存在 — {args.input}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    # 解析
    if args.format == "csv":
        messages = parse_csv(text, args.target)
    elif args.format == "wechat_parser":
        messages = parse_wechat_parser_output(text, args.target)
    else:
        messages = parse_generic(text, args.target)

    if not messages:
        print(f"错误：未能从文件中解析出消息。请检查格式和目标名称。", file=sys.stderr)
        print(f"支持格式：wechat_parser.py 输出、WeChatMsg CSV", file=sys.stderr)
        sys.exit(1)

    print(f"解析完成：共 {len(messages)} 条消息，其中 TA 发出 {sum(1 for m in messages if m.is_them)} 条")

    # 分析
    data = analyze(messages, args.target)

    # 输出
    if args.json:
        output = json.dumps(data, ensure_ascii=False, indent=2)
    else:
        output = format_report(data, args.target)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"报告已输出到：{args.output}")
    else:
        print()
        print(output)


if __name__ == "__main__":
    main()
