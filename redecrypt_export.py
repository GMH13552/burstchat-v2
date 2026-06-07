#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Re-decrypt WCDB + export WeChat chat records (v2)
v2: Uses Name2Id table to properly map sender IDs for private chats.
"""
import hashlib, hmac as hmac_mod, json, os, re, sqlite3, struct, sys, zlib
from datetime import datetime
from pathlib import Path

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ─── Config ──────────────────────────────────────────────────
DECRYPT_TOOLKIT = Path(r"C:\Users\GMH13\.openclaw\workspace\tools\wechat-decrypt")
KEYS_FILE = DECRYPT_TOOLKIT / "all_keys.json"
DECRYPTED_DIR = DECRYPT_TOOLKIT / "decrypted"
OUTPUT_DIR = Path(r"C:\Users\GMH13\.openclaw\workspace")
SRC_DB_DIR = Path(r"C:\Users\GMH13\xwechat_files\wxid_v7wi4ion6qnk22_2bfb\db_storage")
MY_WXID = "wxid_v7wi4ion6qnk22"

PAGE_SZ = 4096; SALT_SZ = 16; IV_SZ = 16; HMAC_SZ = 64; RESERVE_SZ = 80
SQLITE_HDR = b'SQLite format 3\x00'

# ─── Decrypt ──────────────────────────────────────────────────
def derive_mac_key(enc_key, salt):
    return hashlib.pbkdf2_hmac("sha512", enc_key, bytes(b ^ 0x3a for b in salt), 2, dklen=32)

def decrypt_page(enc_key, page_data, pgno):
    from Crypto.Cipher import AES
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        encrypted = page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]
        decrypted = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted)
        return bytes(bytearray(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ))
    encrypted = page_data[:PAGE_SZ - RESERVE_SZ]
    decrypted = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted)
    return decrypted + b'\x00' * RESERVE_SZ

def decrypt_database(db_path, out_path, enc_key):
    from Crypto.Cipher import AES
    sz = os.path.getsize(db_path)
    total = sz // PAGE_SZ + (1 if sz % PAGE_SZ else 0)
    with open(db_path, 'rb') as f:
        p1 = f.read(PAGE_SZ)
    if len(p1) < PAGE_SZ: return False
    salt = p1[:SALT_SZ]
    mac_key = derive_mac_key(enc_key, salt)
    p1_data = p1[SALT_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    hm = hmac_mod.new(mac_key, p1_data, hashlib.sha512)
    hm.update(struct.pack('<I', 1))
    if hm.digest() != p1[PAGE_SZ - HMAC_SZ: PAGE_SZ]:
        print(f"    [FAIL] HMAC mismatch"); return False
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(db_path, 'rb') as fin, open(out_path, 'wb') as fout:
        for pgno in range(1, total + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if len(page) > 0: page += b'\x00' * (PAGE_SZ - len(page))
                else: break
            fout.write(decrypt_page(enc_key, page, pgno))
    try:
        sqlite3.connect(out_path).execute("SELECT 1").fetchone()
        return True
    except:
        Path(out_path).unlink(missing_ok=True); return False

# ─── Extract content ──────────────────────────────────────────
def decompress_wcdb(raw: bytes) -> str:
    if not raw or len(raw) < 2: return ""
    for method, arg in [("raw", -15), ("full", 0), ("gzip", 31)]:
        try:
            d = zlib.decompress(raw, arg) if method != "gzip" else zlib.decompress(raw, 16 + 15)
            return d.decode('utf-8', errors='replace')
        except: pass
    # skip magic header then retry
    for skip in [4, 6, 8]:
        if len(raw) > skip:
            try:
                d = zlib.decompress(raw[skip:], -15)
                return d.decode('utf-8', errors='replace')
            except: pass
    try: return raw.decode('utf-8', errors='replace')
    except: return ""

def get_text(val):
    if val is None: return ""
    if isinstance(val, str): return val.strip()
    if isinstance(val, bytes): return decompress_wcdb(val)
    return ""

# ─── Find contact ─────────────────────────────────────────────
def find_contact(target: str):
    db = DECRYPTED_DIR / "contact" / "contact.db"
    if not db.exists(): return None
    conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
    t = target.lower()
    for fld in ["remark", "nick_name"]:
        for row in conn.execute(f"SELECT username, remark, nick_name FROM contact WHERE LOWER({fld}) = ?", (t,)):
            conn.close(); return (row["username"], row[fld] or row["nick_name"])
    for row in conn.execute("SELECT username, remark, nick_name FROM contact WHERE nick_name != ''"):
        for f in [row["remark"], row["nick_name"]]:
            if f and t in f.lower():
                conn.close(); return (row["username"], f)
    conn.close(); return None

# ─── Read messages via Name2Id ─────────────────────────────────
def read_messages(target_wxid: str) -> list[dict]:
    msg_dir = DECRYPTED_DIR / "message"
    all_msgs = []
    
    for dbf_name in ['message_0.db', 'message_1.db', 'message_2.db']:
        dbf = msg_dir / dbf_name
        if not dbf.exists(): continue
        conn = sqlite3.connect(str(dbf)); conn.row_factory = sqlite3.Row
        
        # Resolve sender IDs for this DB (Name2Id rowids differ across DBs)
        target_sid = None; my_sid = None
        has_n2i = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='Name2Id'").fetchone()[0]
        if has_n2i:
            for r in conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall():
                if r["user_name"] == target_wxid:
                    target_sid = r["rowid"]
                elif r["user_name"] == MY_WXID:
                    my_sid = r["rowid"]
        
        is_msg0 = (dbf_name == 'message_0.db')
        print(f"  [{dbf_name}] Name2Id: target={target_sid}, me={my_sid}")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
        ).fetchall()]
        
        # Check if this DB has real_sender_id
        has_sender = 'real_sender_id' in [r[1] for r in conn.execute(
            f"PRAGMA table_info([{tables[0]}])" if tables else "SELECT 1"
        ).fetchall()] if tables else False
        
        matched = 0
        for t in tables:
            # Strategy 1: real_sender_id (only reliable in message_0.db for private chats)
            if has_sender and is_msg0 and target_sid:
                cnt = conn.execute(f"SELECT COUNT(*) FROM [{t}] WHERE real_sender_id = ?", (target_sid,)).fetchone()[0]
            else:
                cnt = 0
            # Strategy 2: wxid in content (group chats)
            if cnt == 0:
                try:
                    cnt = conn.execute(
                        f"SELECT COUNT(*) FROM [{t}] WHERE message_content LIKE ? OR compress_content LIKE ?",
                        (f"%{target_wxid}%", f"%{target_wxid}%")
                    ).fetchone()[0]
                except: cnt = 0
            if cnt == 0: continue
            matched += 1
            
            rows = conn.execute(f"""
                SELECT create_time, real_sender_id, message_content, compress_content
                FROM [{t}] ORDER BY create_time ASC
            """).fetchall()
            
            for r in rows:
                content = get_text(r['message_content']) or get_text(r['compress_content'])
                if not content or content.startswith('<sysmsg') or content.startswith('<?xml'):
                    continue
                
                sid = r['real_sender_id'] if has_sender else None
                is_them = (sid == target_sid)
                is_me = (sid == my_sid)
                
                if not is_them and not is_me:
                    # Parse from content (wxid:\n format)
                    if content.startswith(target_wxid + ':'):
                        is_them = True
                        content = content.split('\n', 1)[1] if '\n' in content else content[len(target_wxid)+1:]
                    elif content.startswith(MY_WXID + ':'):
                        is_me = True
                        content = content.split('\n', 1)[1] if '\n' in content else content[len(MY_WXID)+1:]
                    elif content.startswith('wxid_'):
                        continue  # other person
                    elif content.startswith('"') or '邀请' in content or '加入了群聊' in content:
                        continue  # system
                
                if not is_them and not is_me: continue
                content = content.strip()
                if not content: continue
                
                ts = r['create_time'] or 0
                if ts > 1e12: ts /= 1000
                try: timestamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                except: timestamp = str(ts)
                
                all_msgs.append({"timestamp": timestamp, "content": content, "is_them": is_them})
        
        conn.close()
        print(f"  [{dbf_name}]: {matched} tables matched")
    
    # Dedup + sort
    seen = set(); deduped = []
    for m in sorted(all_msgs, key=lambda x: x['timestamp']):
        k = (m['timestamp'], m['content'][:100])
        if k not in seen:
            seen.add(k)
            deduped.append(m)
    return deduped

# ─── Main ──────────────────────────────────────────────────────
def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "六月份"
    skip_decrypt = "--no-decrypt" in sys.argv

    if not skip_decrypt:
        print("=" * 60)
        print("  [Step 1] Re-decrypt WCDB databases")
        print("=" * 60)
        if not KEYS_FILE.exists():
            print(f"[ERROR] keys not found: {KEYS_FILE}"); sys.exit(1)
        with open(KEYS_FILE) as f:
            keys = json.load(f)
        keys = {k: v for k, v in keys.items() if not k.startswith('_') and isinstance(v, dict)}
        
        for db_name in ["message_0.db", "message_1.db", "message_2.db"]:
            enc = SRC_DB_DIR / "message" / db_name; out = DECRYPTED_DIR / "message" / db_name
            if not enc.exists(): continue
            ki = keys.get(f"message\\{db_name}")
            if not ki: continue
            print(f"  {db_name} ({os.path.getsize(enc)/1024/1024:.1f}MB) ...")
            ok = decrypt_database(str(enc), str(out), bytes.fromhex(ki["enc_key"]))
            print(f"    {'[OK]' if ok else '[FAIL]'}")

    print(f"\n{'='*60}")
    print(f"  [Step 2] Find contact: {target}")
    print(f"{'='*60}")
    result = find_contact(target)
    if not result: print(f"[ERROR] Not found: {target}"); sys.exit(1)
    wxid, display = result
    print(f"  [OK] {display} ({wxid})")

    print(f"\n{'='*60}")
    print(f"  [Step 3] Extract messages")
    print(f"{'='*60}")
    msgs = read_messages(wxid)
    their = sum(1 for m in msgs if m["is_them"]); my = sum(1 for m in msgs if not m["is_them"])
    print(f"  [Stats] {len(msgs)} msgs (THEM={their}, ME={my})")
    if not msgs: sys.exit(1)

    out = OUTPUT_DIR / f"{target}_chat.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"=== 与 {display} 的聊天记录 ===\n共 {len(msgs)} 条消息\n{'='*60}\n\n")
        for m in msgs:
            sender = display if m["is_them"] else "我"
            f.write(f"[{m['timestamp']}] {sender}: {m['content']}\n")
    
    print(f"  [OK] {out.name} ({out.stat().st_size/1024:.0f}KB)")
    print(f"  [Range] {msgs[0]['timestamp']} ~ {msgs[-1]['timestamp']}")

if __name__ == "__main__":
    main()
