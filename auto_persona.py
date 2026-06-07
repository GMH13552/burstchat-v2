#!/usr/bin/env python3
"""
一键生成完整 persona JSON

用法：
  python auto_persona.py -i 六月份_chat.txt -t 六月份 -o xiaoye.json

流程：
  1. timing_analyzer → 时间行为模式
  2. chat_summarizer → 统计画像
  3. persona_extractor → LLM 深度性格分析
  4. LLM 合成 → 结构化 persona JSON（可直接用于 burstchat-v2）

环境变量: DEEPSEEK_API_KEY
"""

import argparse, asyncio, json, os, re, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "tools"))
sys.path.insert(0, str(HERE))

# Fix Windows encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ─── Step 1: Timing ────────────────────────────────────────────

def run_timing(text: str, target: str) -> dict:
    from timing_analyzer import parse_messages, analyze_timing
    msgs = parse_messages(text, target)
    if len([m for m in msgs if m.is_them]) < 10:
        return {"error": "消息太少"}
    return analyze_timing(msgs, target)


# ─── Step 2: Stats ─────────────────────────────────────────────

def run_stats(text: str, target: str) -> dict:
    from chat_summarizer import parse_wechat_parser_output, compute_stats
    msgs = parse_wechat_parser_output(text, target)
    their = [m for m in msgs if m.is_them]
    my = [m for m in msgs if not m.is_them]
    if not their:
        return {"error": "无消息"}
    return compute_stats(their, my, msgs) if "compute_stats" in dir() else {
        "their_total": len(their), "my_total": len(my),
        "date_range": f"{their[0].timestamp} ~ {their[-1].timestamp}" if their else "N/A"
    }


def _compute_basic_stats(their, my, all_msgs):
    """Minimal stats if chat_summarizer not available"""
    avg_len = sum(len(m.content) for m in their) / max(1, len(their))
    return {
        "their_total": len(their),
        "my_total": len(my),
        "avg_chars_per_msg": round(avg_len, 1),
        "short_msg_ratio": f"{round(sum(1 for m in their if len(m.content) <= 5) / max(1, len(their)) * 100)}%",
        "ratio": f"TA {round(len(their)/max(1,len(all_msgs))*100)}% / 我 {round(len(my)/max(1,len(all_msgs))*100)}%",
    }


# ─── Step 3: Persona (LLM) ─────────────────────────────────────

PERSONA_EXTRACT_SYSTEM = """你是一个聊天记录分析专家。从微信聊天记录中提取一个人完整的说话画像。

输出格式：严格按以下六个维度输出分析结果，每个维度都要有具体行为描述+原文引用。

### 1. 表达指纹
口头禅与高频词（5-10个，附使用场景）；句式特征；emoji/表情使用；回复节奏

### 2. 情绪表达模式
如何表达在乎（引用1-2个典型原文）；如何表达不满；如何道歉；如何说"喜欢"

### 3. 冲突行为链
触发点；典型冲突序列；冷战模式；和解信号

### 4. 关系角色行为
TA主动的情况（频繁/偶尔/很少，主动原因）；消失的情况；TA的边界

### 5. 关系动态总结
3-5句话描述TA和你的关系模式

### 6. 一句话总结

原则：
- 每条结论必须有原文引用支撑
- 不要形容词，写具体行为
- 某个维度信息不足标注（聊天记录不足，以下为推断）
- 中文输出"""


async def run_persona_llm(text: str, target: str, api_key: str) -> str:
    """Call DeepSeek to extract personality profile"""
    from openai import AsyncOpenAI
    from persona_extractor import parse_generic, preprocess

    msgs = parse_generic(text, target)
    data = preprocess(msgs, target, max_chars=60000)
    if "error" in data:
        return f"ERROR: {data['error']}"

    prompt = f"""## 分析对象：{target}
消息数：{data['total_their_msgs']} 条（TA）+ {data['total_my_msgs']} 条（我）
时间范围：{data['date_range']}
样本充分度：{data['sufficiency']}

## 统计参考
{json.dumps(data['stats'], ensure_ascii=False, indent=2)}

## TA 的消息记录（重要性排序采样）
{chr(10).join(data['sampled_messages'])}
"""

    client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    resp = await client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": PERSONA_EXTRACT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7, max_tokens=4000,
    )
    return resp.choices[0].message.content


# ─── Step 4: Synthesize JSON ───────────────────────────────────

SYNTHESIS_SYSTEM = """你是一个 persona 编译器。你的任务是把人物分析报告 + 时间数据 + 统计数据，编译成一个结构化的 JSON persona 配置。

输出纯 JSON，包含以下字段：

{
  "name": "名字",
  "description": "一句话性格概述",
  "layer_0_core_rules": {
    "rules": ["核心行为规则1", "规则2", ...]
  },
  "layer_1_identity": {
    "age": "年龄段(如 20-25)",
    "job": "推断的职业/身份",
    "traits": ["性格特质1", "特质2", ...],
    "pet": "宠物(如有)"
  },
  "layer_2_expression": {
    "max_chars_per_msg": 12,
    "no_period": true,
    "casual_typos": true,
    "catchphrases": ["口头禅1", "口头禅2", ...],
    "emoji": ["常用emoji1", ...],
    "style_notes": ["风格备注"]
  },
  "layer_3_emotional": {
    "express_care": "如何表达在乎(具体行为)",
    "express_upset": "如何表达不满(具体行为)",
    "apology_style": "道歉方式",
    "affection_style": "说\"喜欢\"的方式"
  },
  "layer_4_conflict": {
    "conflict_chain": "冲突模式描述",
    "reconciliation_signal": "和解信号描述",
    "trigger_topics": ["容易被激怒的话题"]
  },
  "layer_5_triggers": {},
  "timing": {
    "attention_delay": {"profile": "moderate", "profiles": {"from_data": {"min": 5, "max": 60}}},
    "conversation_timeout": 600,
    "first_reply_gap": {"normal": 3, "min": 1, "max": 8},
    "fragmentation": {"mode": "medium", "avg_per_burst": 2, "fragment_delay": {"same_thought": [2,4], "new_thought": [6,9]}}
  },
  "example_bursts": [],
  "rules": ["角色规则"]
}

规则：
1. 所有字段必须从分析报告中推断，不要编造
2. 口头禅/emoji必须来自报告中的原文引用
3. timing 直接使用提供的时间数据
4. 情感行为必须对应报告中的具体行为描述
5. 如果某维度信息不足，用合理的默认值填充
6. 输出纯JSON，不要markdown包裹，不要注释"""


async def synthesize_persona(persona_report: str, timing_data: dict,
                              stats: dict, target: str, api_key: str) -> dict:
    """Final LLM call: compile everything into structured persona JSON"""
    from openai import AsyncOpenAI

    timing_str = json.dumps(timing_data, ensure_ascii=False, indent=2)
    stats_str = json.dumps(stats, ensure_ascii=False, indent=2)

    prompt = f"""## 人物：{target}

## 性格分析报告
{persona_report}

## 时间行为数据
{timing_str}

## 统计数据
{stats_str}

请编译为完整的 persona JSON。"""

    client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    resp = await client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": SYNTHESIS_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3, max_tokens=4000,
    )
    raw = resp.choices[0].message.content

    # Extract JSON from response (may have ```json wrappers)
    json_match = re.search(r'\{[\s\S]*\}', raw)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Failed to parse LLM JSON output: {raw[:200]}")


# ─── Main ──────────────────────────────────────────────────────

async def main_async(args):
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: file not found: {args.input}"); sys.exit(1)

    with open(input_path, encoding="utf-8", errors="replace") as f:
        text = f.read()

    target = args.target
    api_key = args.api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("ERROR: need DEEPSEEK_API_KEY"); sys.exit(1)

    # Time filtering
    if args.since:
        filtered = []
        for line in text.split('\n'):
            if line.startswith('['):
                ts = line[1:11]  # YYYY-MM-DD
                if ts >= args.since:
                    filtered.append(line)
        old_count = len([l for l in text.split('\n') if l.startswith('[')])
        new_count = len(filtered)
        print(f"Time filter: {args.since} -> {new_count}/{old_count} messages")
        text = '\n'.join(filtered)
        if not filtered:
            print(f"ERROR: no messages since {args.since}"); sys.exit(1)

    # Step 1: Timing
    print("[1/3] Analyzing timing...")
    timing = run_timing(text, target)
    print(f"  samples: {timing.get('sample_sizes', {}).get('total_their_messages', '?')} msgs")
    if "error" in timing:
        print(f"  WARNING: {timing['error']}")

    # Step 2: Stats
    print("[2/3] Computing stats...")
    from persona_extractor import parse_generic, preprocess
    msgs = parse_generic(text, target)
    data = preprocess(msgs, target, max_chars=args.max_chars)
    if "error" in data:
        print(f"  ERROR: {data['error']}"); sys.exit(1)
    stats = data["stats"]
    print(f"  TA: {stats['their_total']} msgs, ME: {stats['my_total']} msgs")

    # Step 3: Persona LLM + Synthesize
    print("[3/3] Generating persona via LLM...")
    
    # First LLM: extract personality profile
    print("  -> extracting personality profile...")
    persona_report = await run_persona_llm(text, target, api_key)
    
    # Second LLM: compile into JSON
    print("  -> compiling persona JSON...")
    persona_json = await synthesize_persona(
        persona_report, timing, stats, target, api_key
    )
    
    # Inject timing from analyzer directly (more accurate than LLM inference)
    if "error" not in timing:
        from timing_analyzer import generate_timing_config
        tc = json.loads(generate_timing_config(timing))
        persona_json["timing"] = tc
    
    # Add metadata
    persona_json["_generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    persona_json["_based_on"] = f"{stats['their_total']} messages"
    persona_json["_source"] = str(input_path)

    # Output
    output_path = Path(args.output or f"{target}_persona.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(persona_json, f, ensure_ascii=False, indent=2)
    
    print(f"\n[OK] Persona saved: {output_path}")
    print(f"  layers: {[k for k in persona_json if k.startswith('layer_')]}")
    print(f"  timing: {list(persona_json.get('timing', {}).keys())[:5]}...")


def main():
    parser = argparse.ArgumentParser(description="一键生成完整 persona JSON")
    parser.add_argument("--input", "-i", required=True)
    parser.add_argument("--target", "-t", required=True)
    parser.add_argument("--output", "-o")
    parser.add_argument("--api-key", "-k")
    parser.add_argument("--since", "-s", help="只分析此日期之后的消息，如 2026-03-01")
    parser.add_argument("--max-chars", type=int, default=60000, help="发送给LLM的最大字符数")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
