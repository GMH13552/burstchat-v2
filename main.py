#!/usr/bin/env python3
"""小野 — 拟人情感陪伴 AI 启动入口

用法:
    python main.py                     # 默认人设 (xiaoye)
    python main.py --persona xiaoye    # 指定人设

人设文件放在 personas/ 目录下，JSON 格式。
"""

import argparse
import os
import sys


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
    print("   export DEEPSEEK_API_KEY=***")
    print("   或创建 companion/.env:  DEEPSEEK_API_KEY=***")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="拟人情感陪伴 AI")
    parser.add_argument("--persona", default="xiaoye", help="人设文件名 (personas/ 目录下，不含 .json)")
    args = parser.parse_args()

    from burstchat import CompanionApp
    api_key = _load_api_key()
    app = CompanionApp(api_key, persona=args.persona)
    app.run()


if __name__ == "__main__":
    main()

