"""
从 wechat-decrypt 已解密 WCDB 数据库导出聊天记录
消息内容格式: wxid:\n消息内容
"""
import os, sys, sqlite3, zlib
from datetime import datetime
from pathlib import Path

DECRYPT_DIR = Path(r"C:\Users\GMH13\.openclaw\workspace\tools\wechat-decrypt\decrypted")
OUTPUT_DIR = Path(r"C:\Users\GMH13\.openclaw\workspace")

def find_contact(target: str) -> tuple[str, str] | None:
    """返回 (username, display_name)"""
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

def extract_content(raw: bytes | str | None) -> str | None:
    """从 message_content 或 compress_content 提取文本"""
    if raw is None:
        return None
    
    # Try bytes first
    if isinstance(raw, bytes):
        if len(raw) < 2:
            return None
        # Try zlib decompress
        if len(raw) > 10:
            try:
                decomp = zlib.decompress(raw)
                return decomp.decode("utf-8", errors="replace").strip()
            except:
                pass
        # Try raw decode
        try:
            return raw.decode("utf-8", errors="replace").strip()
        except:
            return None
    
    s = str(raw).strip()
    if not s:
        return None
    return s

def read_messages(wxid: str) -> list[dict]:
    """从所有 message_*.db 中搜索包含目标 wxid 的消息"""
    msg_dir = DECRYPT_DIR / "message"
    all_msgs = []
    
    for dbf in sorted(msg_dir.glob("message_*.db")):
        conn = sqlite3.connect(str(dbf))
        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
        ).fetchall()]
        
        for t in tables:
            try:
                rows = conn.execute(f"""
                    SELECT create_time, message_content, compress_content
                    FROM [{t}]
                    WHERE message_content LIKE ? OR compress_content LIKE ?
                    ORDER BY create_time ASC
                """, (f"%{wxid}%", f"%{wxid}%")).fetchall()
                
                for row in rows:
                    content = extract_content(row["message_content"]) or extract_content(row["compress_content"])
                    if not content:
                        continue
                    if content.startswith("<sysmsg") or content.startswith("<?xml"):
                        continue
                    
                    # 解析 wxid:\n消息内容 格式
                    # 或 "昵称:\n消息内容" 格式
                    prefix = f"{wxid}:\n"
                    if content.startswith(prefix):
                        text = content[len(prefix):]
                        is_them = True
                    elif content.startswith(wxid + ":"):
                        text = content[len(wxid)+1:].lstrip("\n")
                        is_them = True
                    elif "\n" in content.split(":")[0] if ":" in content else False:
                        # 其他格式
                        continue
                    else:
                        # 可能是二进制或系统消息
                        continue
                    
                    if not text.strip():
                        continue
                    
                    ts = row["create_time"] or 0
                    if ts > 1e12:
                        ts = ts / 1000
                    try:
                        timestamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                    except:
                        timestamp = str(ts)
                    
                    all_msgs.append({
                        "timestamp": timestamp,
                        "content": text,
                        "is_them": is_them,
                    })
            except Exception:
                continue
        conn.close()
    
    all_msgs.sort(key=lambda x: x["timestamp"])
    return all_msgs

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "六月份"
    
    result = find_contact(target)
    if not result:
        print(f"❌ 未找到: {target}")
        sys.exit(1)
    
    wxid, display = result
    print(f"✅ 找到: {display} ({wxid})")
    
    msgs = read_messages(wxid)
    print(f"📱 共 {len(msgs)} 条消息")
    
    if not msgs:
        print("❌ 没有消息")
        sys.exit(1)
    
    their = sum(1 for m in msgs if m["is_them"])
    out = OUTPUT_DIR / f"{target}_chat.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"=== 与 {display} 的聊天记录 ===\n")
        f.write(f"共 {len(msgs)} 条消息\n")
        f.write(f"{'='*60}\n\n")
        for m in msgs:
            sender = display if m["is_them"] else "我"
            f.write(f"[{m['timestamp']}] {sender}: {m['content']}\n")
    
    print(f"✅ 导出: {out.name} ({out.stat().st_size/1024:.0f}KB, TA={their}/{len(msgs)})")
