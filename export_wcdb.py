"""
从 wechat-decrypt 已解密 WCDB 数据库导出聊天记录
支持群聊：解析 wxid:\\ncontent 格式，按发送方分类
"""
import os, sys, sqlite3, zlib
from datetime import datetime
from pathlib import Path

DECRYPT_DIR = Path(r"C:\Users\GMH13\.openclaw\workspace\tools\wechat-decrypt\decrypted")
OUTPUT_DIR = Path(r"C:\Users\GMH13\.openclaw\workspace")

# 我的 wxid（从目录名推断）
MY_WXID = "wxid_v7wi4ion6qnk22"

def find_contact(target: str) -> tuple[str, str] | None:
    db = DECRYPT_DIR / "contact" / "contact.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    t = target.lower()
    for row in conn.execute("SELECT username, remark, nick_name FROM contact WHERE nick_name != ''"):
        for f in [row["remark"], row["nick_name"]]:
            if f and t in f.lower():
                conn.close()
                return (row["username"], f)
    conn.close()
    return None

def extract_content(raw) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        if len(raw) < 2:
            return None
        if len(raw) > 10:
            try:
                decomp = zlib.decompress(raw)
                return decomp.decode("utf-8", errors="replace").strip()
            except:
                pass
        try:
            return raw.decode("utf-8", errors="replace").strip()
        except:
            return None
    s = str(raw).strip()
    return s if s else None

def parse_msg(content: str, target_wxid: str) -> dict | None:
    """
    解析消息内容，返回 {wxid, text, is_them, is_me, is_other}
    格式: wxid_xxx:\n消息内容（群聊）或 消息内容（私聊/系统）
    """
    # 尝试匹配 wxid:\n 或 wxid: 格式
    import re
    m = re.match(r'(wxid_[a-zA-Z0-9]+):\n?(.*)', content, re.DOTALL)
    if m:
        sender = m.group(1)
        text = m.group(2).strip()
        return {
            "wxid": sender,
            "text": text,
            "is_them": sender == target_wxid,
            "is_me": sender == MY_WXID,
            "is_other": sender != target_wxid and sender != MY_WXID,
        }
    # 也匹配昵称:格式（群聊中可能用昵称而非wxid）
    m = re.match(r'([^:\n]{1,30}):\n(.+)', content, re.DOTALL)
    if m:
        return {
            "wxid": "",
            "text": m.group(2).strip(),
            "is_them": False, "is_me": False, "is_other": True,
        }
    return None

def read_messages(wxid: str) -> list[dict]:
    msg_dir = DECRYPT_DIR / "message"
    all_msgs = []
    
    for dbf in sorted(msg_dir.glob("message_*.db")):
        conn = sqlite3.connect(str(dbf))
        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
        ).fetchall()]
        
        for t in tables:
            like_c = conn.execute(
                f"SELECT COUNT(*) FROM [{t}] WHERE message_content LIKE ? OR compress_content LIKE ?",
                (f"%{wxid}%", f"%{wxid}%")
            ).fetchone()[0]
            if like_c == 0:
                continue
            
            rows = conn.execute(f"""
                SELECT create_time, message_content, compress_content
                FROM [{t}]
                WHERE (message_content IS NOT NULL AND message_content != '')
                   OR (compress_content IS NOT NULL AND compress_content != '')
                ORDER BY create_time ASC
            """).fetchall()
            
            for row in rows:
                content = extract_content(row["message_content"]) or extract_content(row["compress_content"])
                if not content:
                    continue
                if content.startswith("<sysmsg") or content.startswith("<?xml"):
                    continue
                if content.startswith('"') and ('邀请"' in content or '请注意' in content):
                    continue
                
                parsed = parse_msg(content, wxid)
                if not parsed:
                    # 没有 wxid 前缀 → 可能是压缩数据或系统消息
                    if len(content) > 5 and not any(c in content[:5] for c in '<{["'):
                        # 可能是"我"发送的私聊消息（无wxid前缀）
                        parsed = {"wxid": "", "text": content, "is_them": False, "is_me": True, "is_other": False}
                    else:
                        continue
                
                if not parsed["text"]:
                    continue
                if parsed["is_other"]:
                    continue  # 跳过第三方消息
                
                ts = row["create_time"] or 0
                if ts > 1e12:
                    ts = ts / 1000
                try:
                    timestamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                except:
                    timestamp = str(ts)
                
                all_msgs.append({
                    "timestamp": timestamp,
                    "content": parsed["text"],
                    "is_them": parsed["is_them"],
                })
        
        conn.close()
    
    # 去重排序
    seen = set()
    deduped = []
    for m in sorted(all_msgs, key=lambda x: x["timestamp"]):
        key = (m["timestamp"], m["content"][:80])
        if key not in seen:
            seen.add(key)
            deduped.append(m)
    
    return deduped

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "六月份"
    
    result = find_contact(target)
    if not result:
        print(f"no: {target}")
        sys.exit(1)
    
    wxid, display = result
    print(f"found: {display}")
    
    msgs = read_messages(wxid)
    
    their = sum(1 for m in msgs if m["is_them"])
    my = sum(1 for m in msgs if not m["is_them"])
    print(f"msgs: {len(msgs)} (TA={their}, ME={my})")
    
    if not msgs:
        print("no msgs")
        sys.exit(1)
    
    out = OUTPUT_DIR / f"{target}_chat.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"=== 与 {display} 的聊天记录 ===\n")
        f.write(f"共 {len(msgs)} 条消息\n")
        f.write(f"{'='*60}\n\n")
        for m in msgs:
            sender = display if m["is_them"] else "我"
            f.write(f"[{m['timestamp']}] {sender}: {m['content']}\n")
    
    print(f"ok: {out.name} ({out.stat().st_size/1024:.0f}KB)")
