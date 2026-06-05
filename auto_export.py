#!/usr/bin/env python3
"""
微信聊天自动导出 —— 参考 WeChatMsg，一键从运行中的 PC 微信提取聊天记录

原理：
  1. 从微信进程内存提取 SQLCipher 密钥
  2. 解密 MSG*.db → 读取指定联系人的消息
  3. 输出为 analyze_person.py 可用的 txt 格式

用法：
  # 导出指定联系人的聊天，自动跑完全流程
  python auto_export.py 六月份

  # 只导出，不分析
  python auto_export.py 六月份 --export-only

  # 列出所有联系人
  python auto_export.py --list

  # 导出后自动分析
  python auto_export.py 六月份 --analyze

输出：{WORKSPACE}/{名字}_chat.txt

前置条件：
  - 微信 PC 端已登录
  - pip install psutil pymem pycryptodome
"""

import argparse
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ── 配置 ────────────────────────────────────────────────────────────

HERE = Path(__file__).parent.resolve()
TOOLS_DIR = HERE / "tools"
EXSKILL_DIR = Path.home() / "gmh" / "ex-skill-ref"


def load_env() -> dict:
    cfg = {
        "WORKSPACE": str(Path.home() / ".openclaw" / "workspace"),
        "WECHAT_DATA_DIR": str(Path.home() / "Documents" / "WeChat Files"),
    }
    env_path = HERE / ".env"
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k in cfg:
                        cfg[k] = v
    for k in cfg:
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


# ── Step 1: 密钥提取 ────────────────────────────────────────────────

def find_wechat_pid() -> int | None:
    import psutil
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info["name"] or "").lower()
        if name in ("wechat.exe", "wechatapp.exe"):
            return proc.info["pid"]
    return None


def extract_key(pid: int) -> str | None:
    """从微信进程内存提取 SQLCipher 密钥"""
    import pymem
    import pymem.process

    pm = pymem.Pymem(pid)
    try:
        module = pymem.process.module_from_name(pm.process_handle, "WeChatWin.dll")
        if not module:
            print("  ❌ 未找到 WeChatWin.dll，请确认微信已登录", file=sys.stderr)
            return None

        base, size = module.lpBaseOfDll, module.SizeOfImage
        phone_pattern = b"iphone\x00"
        offset = 0

        while offset < size:
            try:
                chunk = pm.read_bytes(base + offset, min(0x100000, size - offset))
            except Exception:
                offset += 0x100000
                continue

            pos = 0
            while True:
                idx = chunk.find(phone_pattern, pos)
                if idx == -1:
                    break
                key_offset = idx - 0x70
                if key_offset >= 0:
                    candidate = chunk[key_offset:key_offset + 32]
                    if len(candidate) == 32 and candidate != b"\x00" * 32:
                        return candidate.hex()
                pos = idx + 1
            offset += 0x100000
    except Exception as e:
        print(f"  ❌ 内存扫描失败: {e}", file=sys.stderr)
    return None


# ── Step 2: 解密数据库 ──────────────────────────────────────────────

def decrypt_db(db_path: str, key_hex: str, output_path: str) -> bool:
    from Crypto.Hash import HMAC, SHA1
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Cipher import AES

    PAGE_SIZE = 4096
    SQLITE_HEADER = b"SQLite format 3\x00"
    key_bytes = bytes.fromhex(key_hex)

    with open(db_path, "rb") as f:
        raw = f.read()

    if len(raw) < PAGE_SIZE:
        return False

    salt = raw[:16]
    key = PBKDF2(key_bytes, salt, dkLen=32, count=4000,
                 prf=lambda p, s: HMAC.new(p, s, SHA1).digest())

    output = bytearray()
    pages = len(raw) // PAGE_SIZE

    for page_num in range(pages):
        page = raw[page_num * PAGE_SIZE:(page_num + 1) * PAGE_SIZE]
        if page_num == 0:
            iv = page[16:32]
            cipher = AES.new(key, AES.MODE_CBC, iv)
            decrypted = cipher.decrypt(page[32:PAGE_SIZE - 32])
            output.extend(SQLITE_HEADER + decrypted[len(SQLITE_HEADER):])
            output.extend(b"\x00" * 32)
        else:
            iv = page[-48:-32]
            cipher = AES.new(key, AES.MODE_CBC, iv)
            decrypted = cipher.decrypt(page[:PAGE_SIZE - 48])
            output.extend(decrypted)
            output.extend(b"\x00" * 48)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(output)

    try:
        conn = sqlite3.connect(output_path)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        conn.close()
        return True
    except sqlite3.DatabaseError:
        Path(output_path).unlink(missing_ok=True)
        return False


# ── Step 3: 读取消息 ────────────────────────────────────────────────

def read_messages(decrypted_db: str, target_wxid: str | None = None) -> list[dict]:
    """从解密后的 MSG.db 读取文本消息"""
    conn = sqlite3.connect(decrypted_db)
    conn.row_factory = sqlite3.Row

    messages = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

    if "MSG" not in tables:
        conn.close()
        return messages

    try:
        if "Name2ID" in tables:
            rows = conn.execute("""
                SELECT m.Type, m.IsSender, m.CreateTime, m.StrContent,
                       COALESCE(n.UsrName, '') AS talker
                FROM MSG m
                LEFT JOIN Name2ID n ON n._id = m.TalkerId
                WHERE m.Type = 1 AND m.StrContent != ''
                ORDER BY m.CreateTime ASC
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT Type, IsSender, CreateTime, StrContent, '' AS talker
                FROM MSG WHERE Type = 1 AND StrContent != ''
                ORDER BY CreateTime ASC
            """).fetchall()
    except Exception:
        conn.close()
        return messages

    for row in rows:
        content = (row["StrContent"] or "").strip()
        if not content or content in ("[图片]", "[语音]", "[文件]", "[视频]", "[撤回了一条消息]"):
            continue

        # XML 消息提取文本
        if content.startswith("<"):
            m = re.search(r"<title[^>]*>([^<]+)</title>", content)
            content = f"[分享] {m.group(1).strip()}" if m else ""
            if not content:
                continue

        talker = row["talker"] or ""
        if target_wxid and talker and talker != target_wxid:
            continue
        if target_wxid and not talker:
            continue  # 无法确定联系人，跳过

        ts = row["CreateTime"] or 0
        try:
            timestamp = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            timestamp = str(ts)

        messages.append({
            "sender": "me" if row["IsSender"] == 1 else "them",
            "content": content,
            "timestamp": timestamp,
        })

    conn.close()
    return messages


# ── Step 4: 找联系人 ────────────────────────────────────────────────

def find_contacts(decrypted_db: str) -> list[dict]:
    """从 MicroMsg.db 列出联系人"""
    conn = sqlite3.connect(decrypted_db)
    conn.row_factory = sqlite3.Row
    contacts = []
    try:
        for row in conn.execute("""
            SELECT UserName, Alias, Remark, NickName
            FROM Contact WHERE Type != 4 AND NickName != ''
            ORDER BY NickName
        """):
            contacts.append({
                "wxid": row["UserName"] or "",
                "alias": row["Alias"] or "",
                "remark": row["Remark"] or "",
                "nickname": row["NickName"] or "",
            })
    except Exception:
        pass
    conn.close()
    return contacts


def resolve_target(db_dir: str, target: str) -> str | None:
    """根据昵称/备注找到 wxid"""
    micro_msg = Path(db_dir) / "MicroMsg.db"
    if not micro_msg.exists():
        return None
    contacts = find_contacts(str(micro_msg))
    target_lower = target.lower()

    for c in contacts:
        for field in [c["remark"], c["nickname"], c["alias"], c["wxid"]]:
            if field.lower() == target_lower:
                return c["wxid"]

    for c in contacts:
        for field in [c["remark"], c["nickname"]]:
            if target_lower in field.lower():
                print(f"  模糊匹配: {field}")
                return c["wxid"]

    # 列出所有联系人供选择
    print(f"  未找到精确匹配 '{target}'，可用联系人：")
    for c in contacts[:30]:
        display = c["remark"] or c["nickname"]
        print(f"    {display}")
    return None


# ── 主流程 ──────────────────────────────────────────────────────────

def export_chat(target: str, output_path: Path, cfg: dict) -> bool:
    """一键导出：内存提密钥 → 解密 → 读消息 → 写文件"""
    temp_dir = Path(tempfile.mkdtemp(prefix="wx_decrypt_"))
    try:
        # 1. 检查微信
        pid = find_wechat_pid()
        if not pid:
            print("  ❌ 未找到微信进程。请先打开微信 PC 端并登录。")
            return False
        print(f"  ✅ 找到微信进程 (PID={pid})")

        # 2. 提取密钥
        print(f"  🔑 从内存提取密钥...")
        key = extract_key(pid)
        if not key:
            print("  ❌ 密钥提取失败。可以尝试：")
            print("    1. 以管理员身份运行本脚本")
            print("    2. 确认微信已登录（非锁屏状态）")
            return False
        print(f"  ✅ 密钥提取成功")

        # 3. 找数据库
        wx_data = Path(cfg["WECHAT_DATA_DIR"])
        wxid_dirs = [d for d in wx_data.iterdir() if d.is_dir() and d.name.startswith("wxid_")]
        if not wxid_dirs:
            print(f"  ❌ 未找到微信数据目录: {wx_data}")
            return False
        wxid_dir = wxid_dirs[0]
        if len(wxid_dirs) > 1:
            print(f"  找到多个账号目录，使用: {wxid_dir.name}")

        msg_dir = wxid_dir / "Msg" / "Multi"
        if not msg_dir.exists():
            msg_dir = wxid_dir / "Msg"

        db_files = sorted(msg_dir.glob("MSG*.db"))
        if not db_files:
            print(f"  ❌ 未找到 MSG*.db: {msg_dir}")
            return False
        print(f"  📁 {len(db_files)} 个数据库文件")

        # 4. 解密所有 MSG.db
        decrypted_files = []
        for dbf in db_files:
            out = temp_dir / dbf.name
            print(f"  🔓 解密 {dbf.name}...", end=" ", flush=True)
            if decrypt_db(str(dbf), key, str(out)):
                print("✓")
                decrypted_files.append(out)
            else:
                print("✗")

        if not decrypted_files:
            print("  ❌ 解密失败")
            return False

        # 5. 解密 MicroMsg.db 找 wxid
        micro_src = wxid_dir / "Msg" / "MicroMsg.db"
        micro_dec = temp_dir / "MicroMsg.db"
        target_wxid = None
        if micro_src.exists():
            if decrypt_db(str(micro_src), key, str(micro_dec)):
                target_wxid = resolve_target(str(temp_dir), target)
            else:
                # 尝试 Contact 目录
                contact_src = wxid_dir / "Msg" / "Multi" / "MicroMsg.db"
                if not contact_src.exists():
                    # 从 MSG 表直接搜 TalkerId
                    target_wxid = _find_talker_from_msg(decrypted_files[0], target)
        else:
            target_wxid = _find_talker_from_msg(decrypted_files[0], target)

        if not target_wxid:
            print(f"  ❌ 未找到联系人 '{target}'")
            return False
        print(f"  👤 匹配到: {target} (wxid={target_wxid[:12]}...)")

        # 6. 读消息
        all_msgs = []
        for dbf in decrypted_files:
            msgs = read_messages(str(dbf), target_wxid)
            all_msgs.extend(msgs)

        all_msgs.sort(key=lambda x: x["timestamp"])
        print(f"  💬 共 {len(all_msgs)} 条消息")

        if len(all_msgs) < 10:
            print(f"  ❌ 消息太少")
            return False

        # 7. 写入文件
        their_count = sum(1 for m in all_msgs if m["sender"] == "them")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"=== 与 {target} 的聊天记录 ===\n")
            f.write(f"共 {len(all_msgs)} 条消息\n")
            f.write(f"============================================================\n\n")
            for msg in all_msgs:
                sender = target if msg["sender"] == "them" else "我"
                f.write(f"[{msg['timestamp']}] {sender}: {msg['content']}\n")

        print(f"  ✅ 导出完成: {output_path.name} ({output_path.stat().st_size/1024:.0f}KB)")
        return True

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _find_talker_from_msg(decrypted_db: Path, target: str) -> str | None:
    """从消息表的 StrTalker 字段推断 wxid"""
    conn = sqlite3.connect(str(decrypted_db))
    conn.row_factory = sqlite3.Row
    try:
        # 尝试找有备注/昵称匹配的消息
        rows = conn.execute("""
            SELECT DISTINCT StrTalker FROM MSG
            WHERE StrTalker != '' AND Type = 1
            LIMIT 2000
        """).fetchall()
        talkers = [r["StrTalker"] for r in rows if r["StrTalker"]]
        for t in talkers:
            if target.lower() in t.lower():
                return t
        # 如果用微信备注搜索不到，返回最常见的 talker
        if talkers:
            return talkers[0]
    except Exception:
        pass
    conn.close()
    return None


def list_contacts_full(cfg: dict):
    """列出所有微信联系人"""
    pid = find_wechat_pid()
    if not pid:
        print("❌ 微信未运行，请先登录微信")
        return

    key = extract_key(pid)
    if not key:
        print("❌ 密钥提取失败")
        return

    wx_data = Path(cfg["WECHAT_DATA_DIR"])
    wxid_dirs = [d for d in wx_data.iterdir() if d.is_dir() and d.name.startswith("wxid_")]
    if not wxid_dirs:
        print("❌ 未找到微信数据")
        return

    temp_dir = Path(tempfile.mkdtemp(prefix="wx_contacts_"))
    try:
        micro_src = wxid_dirs[0] / "Msg" / "MicroMsg.db"
        if not micro_src.exists():
            micro_src = wxid_dirs[0] / "Msg" / "Multi" / "MicroMsg.db"
        micro_dec = temp_dir / "MicroMsg.db"

        if micro_src.exists() and decrypt_db(str(micro_src), key, str(micro_dec)):
            contacts = find_contacts(str(micro_dec))
            print(f"\n{'='*60}")
            print(f"  微信联系人（{len(contacts)} 人）")
            print(f"{'='*60}")
            for c in sorted(contacts, key=lambda x: x["remark"] or x["nickname"]):
                display = c["remark"] or c["nickname"]
                print(f"  {display}")
        else:
            print("❌ 无法解密联系人数据库")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ── CLI ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="微信聊天自动导出（参考 WeChatMsg 原理）",
        epilog="""
示例：
  python auto_export.py 六月份              # 导出 + 全分析
  python auto_export.py 六月份 --export-only # 只导出
  python auto_export.py --list              # 列出所有联系人
        """
    )
    parser.add_argument("target", nargs="?", help="联系人名称")
    parser.add_argument("--export-only", action="store_true", help="只导出，不分析")
    parser.add_argument("--list", action="store_true", help="列出所有联系人")

    args = parser.parse_args()
    cfg = load_env()
    workspace = Path(cfg["WORKSPACE"])

    if args.list:
        list_contacts_full(cfg)
        return

    if not args.target:
        parser.print_help()
        return

    target = args.target
    chat_file = workspace / f"{target}_chat.txt"

    print(f"\n{'='*60}")
    print(f"  📱 自动导出: {target}")
    print(f"  📁 输出: {chat_file}")
    print(f"{'='*60}\n")

    ok = export_chat(target, chat_file, cfg)
    if not ok:
        print(f"\n❌ 导出失败。请改用 WeChatMsg 手动导出。")
        sys.exit(1)

    if args.export_only:
        print(f"\n✅ 导出完成: {chat_file}")
        return

    # 自动跑分析
    print(f"\n{'='*60}")
    print(f"  🧠 开始分析...")
    print(f"{'='*60}")

    import subprocess
    analyze_script = HERE / "analyze_person.py"
    if analyze_script.exists():
        subprocess.run([sys.executable, str(analyze_script), target])
    else:
        print(f"💡 请手动运行: python analyze_person.py {target}")


if __name__ == "__main__":
    main()
