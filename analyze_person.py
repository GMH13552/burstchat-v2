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
        Path.home() / ".openclaw" / "workspace" / ".env",
        HERE / ".env",
    ]:
        if loc.exists():
            with open(loc) as f:
                for line in f:
                    if line.startswith("DEEPSEEK_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ── Step 1: 从微信提取聊天记录 ──────────────────────────────────────

def _find_wechatmsg_export(target: str) -> Path | None:
    """搜索 WeChatMsg 的导出文件"""
    candidates = [
        Path.home() / "Desktop" / f"{target}.txt",
        Path.home() / "Desktop" / f"{target}_chat.txt",
        Path.home() / "Downloads" / f"{target}.txt",
        Path.home() / "Downloads" / f"{target}_chat.txt",
        Path.home() / ".openclaw" / "workspace" / f"{target}_chat.txt",
    ]
    for p in candidates:
        if p.exists() and p.stat().st_size > 500:
            return p
    return None


def extract_chat(cfg: dict, target: str, output_path: Path) -> bool:
    """尝试从微信数据库提取聊天记录"""
    db_dir = cfg["WECHAT_DB_DIR"]
    if not db_dir:
        return False

    db_path = Path(db_dir)
    if not db_path.exists():
        return False

    # 检查数据库是否已解密
    test_db = db_path / "MSG0.db" if db_path.is_dir() else db_path
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{test_db}?mode=ro", uri=True)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        conn.close()
    except sqlite3.DatabaseError:
        print(f"  ⚠️ 微信数据库加密中，无法直接读取")
        print(f"  请用 WeChatMsg 导出后使用 --file 参数：")
        print(f"    python analyze_person.py {target} --file 导出文件.txt")
        return False

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

def run_cmd(cmd: list[str], desc: str, timeout: int = 120, extra_env: dict = None) -> bool:
    print(f"\n  {'─'*50}")
    print(f"  {desc}")
    print(f"  {'─'*50}")
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(cmd, timeout=timeout, env=env)
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
        # 搜索 WeChatMsg 导出
        wm_found = _find_wechatmsg_export(target)
        if wm_found:
            shutil.copy(wm_found, chat_file)
            print(f"  📋 从 WeChatMsg 导出复制: {chat_file.name}")
        else:
            # 尝试自动从微信导出
            print(f"  📱 尝试自动从微信导出...")
            auto_export = HERE / "auto_export.py"
            if auto_export.exists():
                result = subprocess.run(
                    [sys.executable, str(auto_export), target, "--export-only"],
                    timeout=120,
                )
                if result.returncode != 0 or not chat_file.exists():
                    print(f"\n  💡 自动导出失败。请打开 WeChatMsg → 选「{target}」→ 导出文本")
                    print(f"  然后运行: python analyze_person.py {target} --file 导出文件.txt")
                    sys.exit(1)
            else:
                print(f"\n  💡 提示：打开 WeChatMsg → 选「{target}」→ 导出文本")
                print(f"  然后运行: python analyze_person.py {target} --file 导出文件.txt")
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

    ok = run_cmd(
        [sys.executable, str(TOOLS_DIR / "persona_extractor.py"),
         "-i", str(chat_file), "-t", target, "-o", str(persona_out)],
        f"🧠 Step 3: LLM 深度画像 → {persona_out.name}",
        extra_env={"DEEPSEEK_API_KEY": api_key}
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
