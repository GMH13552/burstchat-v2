#!/usr/bin/env python3
"""
一键人物画像分析

用法：
  python run_analysis.py messages.txt "目标名"

等价于依次运行：
  timing_analyzer.py → timing 报告
  persona_extractor.py → LLM 深度分析（需要 DEEPSEEK_API_KEY）

输出：
  {目标名}_timing.md    — 时间行为数据
  {目标名}_persona.md   — LLM 深度画像
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).parent / "tools"


def find_api_key() -> str:
    """从多个来源查找 API key"""
    # 环境变量
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key

    # companion/.env
    for loc in [
        Path.home() / ".openclaw" / "workspace" / "companion" / ".env",
        Path.home() / ".openclaw" / "workspace" / ".env",
    ]:
        if loc.exists():
            with open(loc) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DEEPSEEK_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")

    print("⚠️ 未找到 DEEPSEEK_API_KEY，跳过 LLM 分析")
    print("   设置方法：export DEEPSEEK_API_KEY=sk-xxx")
    return ""


def run(cmd: list[str], desc: str) -> bool:
    print(f"\n{'═'*60}")
    print(f"  {desc}")
    print(f"{'═'*60}")
    result = subprocess.run(cmd)
    return result.returncode == 0


async def main():
    if len(sys.argv) < 3:
        print("用法: python run_analysis.py <chat_file.txt> <目标名>")
        print("示例: python run_analysis.py 闲云_chat.txt 闲云")
        sys.exit(1)

    chat_file = Path(sys.argv[1])
    target = sys.argv[2]

    if not chat_file.exists():
        print(f"❌ 文件不存在: {chat_file}")
        sys.exit(1)

    # 输出文件名
    stem = chat_file.stem  # 闲云_chat → 闲云_chat
    base_name = target
    timing_out = chat_file.parent / f"{base_name}_timing.md"
    persona_out = chat_file.parent / f"{base_name}_persona.md"

    # Step 1: 时间分析
    ok = run(
        [sys.executable, str(TOOLS_DIR / "timing_analyzer.py"),
         "-i", str(chat_file),
         "-t", target,
         "-o", str(timing_out)],
        f"📊 Step 1/2: 时间行为分析 → {timing_out.name}"
    )
    if not ok:
        print("❌ 时间分析失败")
        sys.exit(1)

    # Step 2: LLM 深度分析
    api_key = find_api_key()
    if api_key:
        # 临时设环境变量给子进程
        env = os.environ.copy()
        env["DEEPSEEK_API_KEY"] = api_key
        ok = run(
            [sys.executable, str(TOOLS_DIR / "persona_extractor.py"),
             "-i", str(chat_file),
             "-t", target,
             "-o", str(persona_out)],
            f"🧠 Step 2/2: LLM 深度分析 → {persona_out.name}"
        )
        if not ok:
            print("❌ LLM 分析失败")
            sys.exit(1)
    else:
        print("\n⚠️ 跳过 LLM 分析（无 API key），仅完成时间分析")

    print(f"\n{'═'*60}")
    print(f"  ✅ 完成！")
    print(f"  时间画像: {timing_out}")
    if persona_out.exists():
        print(f"  LLM 画像: {persona_out}")
    print(f"{'═'*60}")


if __name__ == "__main__":
    asyncio.run(main())
