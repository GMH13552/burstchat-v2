#!/usr/bin/env python3
"""
一键人物画像 —— 从微信导出到深度分析

用法：
  # 自动从微信提取（需要微信运行中）
  python analyze_person.py 六月份

  # 使用已有聊天文件
  python analyze_person.py 六月份 --file chat.txt

  # 只做时间分析（离线）
  python analyze_person.py 六月份 --timing-only

输出：{workspace}/{名字}_timing.md  +  {workspace}/{名字}_persona.md
"""

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Windows 控制台编码修复
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ── 配置（从 .env 或默认值） ──────────────────────────────────────────

HERE = Path(__file__).parent.resolve()
TOOLS_DIR = HERE / "tools"
EXSKILL_DIR = Path.home() / "gmh" / "ex-skill-ref"


def load_env() -> dict:
    """加载配置，优先级：环境变量 > .env 文件 > 默认值"""
    cfg = {
        "WORKSPACE": str(Path.home() / ".openclaw" / "workspace"),
        "WECHAT_DB_DIR": "",
        "WECHAT_SELF_NAME": "我",
    }

    # 读 .env
    env_path = HERE / ".env"
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k in cfg:
                        cfg[k] = v

    # 环境变量覆盖
    for k in cfg:
        if os.environ.get(k):
            cfg[k] = os.environ[k]

    return cfg


def find_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    env_path = HERE / ".env"
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    # 兼容旧位置
    for loc in [
        Path.home() / ".openclaw" / "workspace" / "companion" / ".env",
    ]:
        if loc.exists():
            with open(loc) as f:
                for line in f:
                    if line.startswith("DEEPSEEK_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ── Step 1: 从微信提取聊天记录 ──────────────────────────────────────

def extract_chat(cfg: dict, target: str, output_path: Path) -> bool:
    """尝试从微信数据库提取聊天记录"""
    db_dir = cfg["WECHAT_DB_DIR"]
    if not db_dir:
        return False

    db_path = Path(db_dir)
    if not db_path.exists():
        print(f"  ⚠️ 微信数据库路径不存在: {db_dir}")
        return False

    # 尝试用 wechat_parser 提取
    parser = EXSKILL_DIR / "tools" / "wechat_parser.py"
    if not parser.exists():
        print("  ⚠️ 未找到 wechat_parser.py")
        return False

    print(f"  📱 从微信数据库提取 {target} 的聊天记录...")
    result = subprocess.run(
        [sys.executable, str(parser),
         "--db-dir", str(db_path),
         "--target", target,
         "--output", str(output_path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"  ❌ 提取失败: {result.stderr[:200]}")
        return False

    if not output_path.exists() or output_path.stat().st_size < 100:
        print(f"  ❌ 提取到的消息太少")
        return False

    size_kb = output_path.stat().st_size / 1024
    print(f"  ✅ 已提取 {size_kb:.0f}KB → {output_path.name}")
    return True


# ── Step 2 & 3: 分析 ────────────────────────────────────────────────

def run_cmd(cmd: list[str], desc: str, timeout: int = 120) -> bool:
    print(f"\n  {'─'*50}")
    print(f"  {desc}")
    print(f"  {'─'*50}")
    result = subprocess.run(cmd, timeout=timeout)
    return result.returncode == 0


# ── 主流程 ───────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(
        description="一键人物画像",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python analyze_person.py 闲云                  # 自动提取 + 全分析
  python analyze_person.py 六月份 --file chat.txt # 用已有文件
  python analyze_person.py 张三 --timing-only     # 只做时间分析
        """
    )
    parser.add_argument("target", help="要分析的联系人名称")
    parser.add_argument("--file", "-f", help="使用已有的聊天记录文件（跳过提取）")
    parser.add_argument("--timing-only", action="store_true", help="只做时间分析，跳过 LLM")
    parser.add_argument("--api-key", "-k", help="DeepSeek API key（不填则从 .env 读取）")

    args = parser.parse_args()
    target = args.target
    cfg = load_env()
    workspace = Path(cfg["WORKSPACE"])
    workspace.mkdir(parents=True, exist_ok=True)

    chat_file = workspace / f"{target}_chat.txt"

    print(f"\n{'='*60}")
    print(f"  🔬 分析人物：{target}")
    print(f"  📁 工作目录：{workspace}")
    print(f"  🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # Step 1: 获取聊天记录
    print(f"\n📥 Step 1: 获取聊天记录")

    if args.file:
        src = Path(args.file)
        if not src.exists():
            print(f"  ❌ 文件不存在: {args.file}")
            sys.exit(1)
        if src != chat_file:
            import shutil
            shutil.copy(src, chat_file)
            print(f"  📋 从 {src.name} 复制 → {chat_file.name}")
        else:
            print(f"  📋 使用已有文件: {chat_file.name}")
    elif chat_file.exists():
        print(f"  📋 使用已有文件: {chat_file.name} ({chat_file.stat().st_size/1024:.0f}KB)")
    else:
        ok = extract_chat(cfg, target, chat_file)
        if not ok:
            print(f"\n  ❌ 无法提取聊天记录。")
            print(f"  请手动导出后使用: python analyze_person.py {target} --file 导出文件.txt")
            sys.exit(1)

    # Step 2: 时间分析
    timing_out = workspace / f"{target}_timing.md"
    ok = run_cmd(
        [sys.executable, str(TOOLS_DIR / "timing_analyzer.py"),
         "-i", str(chat_file), "-t", target, "-o", str(timing_out)],
        f"📊 Step 2: 时间行为分析 → {timing_out.name}"
    )
    if not ok:
        sys.exit(1)

    if args.timing_only:
        print(f"\n{'='*60}")
        print(f"  ✅ 完成（仅时间分析）")
        print(f"  📄 {timing_out}")
        print(f"{'='*60}")
        return

    # Step 3: LLM 深度分析
    persona_out = workspace / f"{target}_persona.md"
    api_key = args.api_key or find_api_key()
    if not api_key:
        print("\n  ⚠️ 未找到 DEEPSEEK_API_KEY，跳过 LLM 分析")
        print(f"  在 .env 中设置: DEEPSEEK_API_KEY=sk-xxx")
        print(f"\n  ✅ 完成（仅时间分析）")
        return

    env = os.environ.copy()
    env["DEEPSEEK_API_KEY"] = api_key

    ok = run_cmd(
        [sys.executable, str(TOOLS_DIR / "persona_extractor.py"),
         "-i", str(chat_file), "-t", target, "-o", str(persona_out)],
        f"🧠 Step 3: LLM 深度画像 → {persona_out.name}"
    )
    if not ok:
        sys.exit(1)

    # 完成
    print(f"\n{'='*60}")
    print(f"  ✅ 全部分析完成！")
    print(f"  📊 时间画像: {timing_out}")
    print(f"  🧠 LLM 画像: {persona_out}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
