"""
Truecaller Userbot API — @RAJFFLIVEBOT
- HTML file download karo (text pagination nahi)
- Clean flat JSON output
- All records returned (multi-row, multi-source)
- Auto access refresh via Nick_Bypass_Bot
- Multi-account round-robin system
- API Key management with expiry
- 5x String Session support via env vars
- Response time tracking
- In-memory cache with TTL
- Admin panel with login (no key in URL)
- Bot tag: @rajfflivebot | Dev: @rajfflive
"""

from flask import Flask, request, jsonify, make_response, g
from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest
from telethon.sessions import StringSession
import asyncio, threading, re, os, time, logging, requests
import json, uuid, sqlite3, io
from datetime import datetime, timedelta
from functools import wraps
from bs4 import BeautifulSoup, NavigableString, Tag

# ==================== CREDENTIALS (from env only) ====================
API_ID    = int(os.environ["API_ID"])
API_HASH  = os.environ["API_HASH"]
API_KEY   = os.environ["API_KEY"]
ADMIN_KEY = os.environ["ADMIN_KEY"]

# ==================== CONFIG ====================
TRUECALLER_BOT = "@Truecaller_redbot"
NICK_BOT       = "@Nick_Bypass_Bot"
BOT_USERNAME   = os.environ.get("BOT_USERNAME", "@RAJFFLIVEBOT")
LEAK_BOT       = os.environ.get("LEAK_BOT", "")
DEVELOPER_TAG  = os.environ.get("DEVELOPER_TAG", "👤 @RAJFFLIVE | 📢 t.me/RAJFFLIVE")
BOT_TAG        = os.environ.get("BOT_TAG", "@RAJFFLIVEBOT")
CACHE_TTL      = int(os.environ.get("CACHE_TTL", 86400))  # seconds, default 24h

logging.basicConfig(level=logging.INFO)
logging.getLogger('telethon').setLevel(logging.WARNING)

app  = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
loop = None

pending      = {}
leak_pending = {}
stats        = {"total": 0, "success": 0, "failed": 0, "cache_hits": 0}

# ==================== IN-MEMORY CACHE ====================
_cache: dict = {}
_cache_lock  = threading.Lock()

def cache_get(key: str):
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        if time.time() - entry["ts"] > CACHE_TTL:
            del _cache[key]
            return None
        return entry["result"]

def cache_set(key: str, result: dict):
    with _cache_lock:
        _cache[key] = {"result": result, "ts": time.time()}

def cache_clear():
    with _cache_lock:
        _cache.clear()

def cache_stats():
    with _cache_lock:
        now = time.time()
        valid = sum(1 for v in _cache.values() if now - v["ts"] <= CACHE_TTL)
        return {"total": len(_cache), "valid": valid, "ttl_hours": CACHE_TTL // 3600}

# ==================== DATABASE ====================
DB_PATH = os.environ.get("DB_PATH", "rajff.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key         TEXT UNIQUE NOT NULL,
            name        TEXT NOT NULL,
            created     TEXT NOT NULL,
            expiry      TEXT,
            active      INTEGER DEFAULT 1,
            uses        INTEGER DEFAULT 0,
            daily_limit INTEGER DEFAULT 0,
            daily_uses  INTEGER DEFAULT 0,
            last_reset  TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL,
            api_id         TEXT NOT NULL,
            api_hash       TEXT NOT NULL,
            session_string TEXT NOT NULL,
            active         INTEGER DEFAULT 1,
            created        TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    print("[DB] Initialized")

def get_config(key, default=""):
    conn = get_db()
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_config(key, value):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()

def get_bot_username():
    return get_config("bot_username", BOT_USERNAME)

def get_leak_bot():
    return get_config("leak_bot", LEAK_BOT)

# ==================== AUTO-SEED SESSIONS FROM ENV ====================
def seed_permanent_keys():
    conn = get_db()
    existing = conn.execute("SELECT key FROM api_keys WHERE name='RAJFFLIVE'").fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO api_keys (key,name,created,expiry,active,uses,daily_limit) VALUES (?,?,?,?,1,0,0)",
            ("RAJFFLIVE", "RAJFFLIVE", datetime.now().isoformat(), None)
        )
        conn.commit()
        print("[SEED] Permanent key RAJFFLIVE created")
    conn.close()

def seed_sessions_from_env():
    conn = get_db()
    existing = [r["name"] for r in conn.execute("SELECT name FROM accounts").fetchall()]
    conn.close()
    session_keys = [
        ("STRING_SESSION",   "API_ID",   "API_HASH",   "Account 1"),
        ("STRING_SESSION_2", "API_ID_2", "API_HASH_2", "Account 2"),
        ("STRING_SESSION_3", "API_ID_3", "API_HASH_3", "Account 3"),
        ("STRING_SESSION_4", "API_ID_4", "API_HASH_4", "Account 4"),
        ("STRING_SESSION_5", "API_ID_5", "API_HASH_5", "Account 5"),
    ]
    for sess_env, id_env, hash_env, default_name in session_keys:
        sess  = os.environ.get(sess_env, "").strip()
        if not sess: continue
        aid   = os.environ.get(id_env,   os.environ.get("API_ID",   "")).strip()
        ahash = os.environ.get(hash_env, os.environ.get("API_HASH", "")).strip()
        if not aid or not ahash: continue
        if default_name in existing:
            continue
        conn = get_db()
        conn.execute(
            "INSERT INTO accounts (name, api_id, api_hash, session_string, active, created) VALUES (?,?,?,?,1,?)",
            (default_name, aid, ahash, sess, datetime.now().isoformat())
        )
        conn.commit(); conn.close()
        print(f"[SEED] Seeded {default_name}")

# ==================== API KEY AUTH ====================
def check_api_key(key):
    if not key: return False
    conn = get_db()
    row = conn.execute("SELECT * FROM api_keys WHERE key=? AND active=1", (key,)).fetchone()
    conn.close()
    if not row: return False
    if row["expiry"]:
        if datetime.now() > datetime.fromisoformat(row["expiry"]):
            return False
    try:
        daily_limit = row["daily_limit"] if "daily_limit" in row.keys() else 0
        if daily_limit > 0:
            today      = datetime.now().strftime("%Y-%m-%d")
            last_reset = row["last_reset"] if "last_reset" in row.keys() else None
            daily_uses = row["daily_uses"] if "daily_uses" in row.keys() else 0
            if last_reset != today: daily_uses = 0
            if daily_uses >= daily_limit: return False
            conn = get_db()
            conn.execute("UPDATE api_keys SET uses=uses+1, daily_uses=?, last_reset=? WHERE key=?",
                         (daily_uses + 1, today, key))
            conn.commit(); conn.close()
            return True
    except Exception:
        pass
    conn = get_db()
    conn.execute("UPDATE api_keys SET uses=uses+1 WHERE key=?", (key,))
    conn.commit(); conn.close()
    return True

def require_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.args.get("key", "")
        if key != API_KEY and not check_api_key(key):
            return jsonify({
                "status": False,
                "error": "Invalid or expired API key",
                **make_footer()
            }), 401
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = (request.args.get("key", "")
               or (request.json or {}).get("key", "")
               or request.cookies.get("adm_key", ""))
        if key != ADMIN_KEY:
            return jsonify({"success": False, "error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated

# ==================== MULTI-ACCOUNT MANAGER ====================
class AccountManager:
    def __init__(self):
        self._clients  = {}
        self._rr_index = 0
        self._lock     = threading.Lock()

    def get_active_ids(self):
        conn = get_db()
        rows = conn.execute("SELECT id FROM accounts WHERE active=1").fetchall()
        conn.close()
        return [r["id"] for r in rows]

    def get_client(self, acc_id):
        return self._clients.get(acc_id)

    def set_client(self, acc_id, client):
        self._clients[acc_id] = client

    def remove_client(self, acc_id):
        return self._clients.pop(acc_id, None)

    def next_client(self):
        with self._lock:
            active_ids = [
                aid for aid in self.get_active_ids()
                if aid in self._clients and self._clients[aid].is_connected()
            ]
            if not active_ids: return None, None
            idx = self._rr_index % len(active_ids)
            self._rr_index = (self._rr_index + 1) % len(active_ids)
            return active_ids[idx], self._clients[active_ids[idx]]

acc_manager = AccountManager()

# ==================== UTILS ====================
def clean_num(n):
    s = str(n).strip()
    digits = re.sub(r'[^\d]', '', s)
    if digits:
        if len(digits) == 12 and digits[:2] in ('91', '92'): digits = digits[2:]
        return digits
    return s

def valid_num(n):
    c = clean_num(n) if re.search(r'\d', str(n)) else str(n).strip()
    if not c: c = str(n).strip()
    country = "Pakistan" if (len(c) == 11 and c.startswith('03')) else "India"
    return True, c if c else str(n).strip(), country

def find_link(text):
    m = re.search(r'https?://\S+', text or "")
    return m.group(0) if m else None

def btn_link(msg):
    if msg and msg.buttons:
        for row in msg.buttons:
            for b in row:
                if hasattr(b, 'url') and b.url: return b.url
    return None

def extract_field(line, *keywords):
    clean = re.sub(r'^[\W]+', '', line).strip()
    for kw in keywords:
        m = re.search(rf'{re.escape(kw)}\s*[:\-]\s*(.+)', clean, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            val = re.sub(r'[`\'\"\\]', '', val)
            val = re.sub(r'[^\w\s,.\-@/&]', '', val)
            val = re.sub(r'\s+', ' ', val).strip()
            if val: return val
    return None

# ==================== HTML LEAK PARSER ====================
# Emoji + symbol regex for stripping from field names
_EMOJI_STRIP_RE = re.compile(
    r'[\U00010000-\U0010ffff'
    r'\u2000-\u26FF'
    r'\u2700-\u27BF'
    r'\U0001F000-\U0001FFFF'
    r'\U00002702-\U000027B0'
    r'\U000024C2-\U0001F251'
    r'\u200d\ufe0f\u20e3'
    r'\u00a9\u00ae'
    r']+',
    re.UNICODE
)

def _clean_field_name(raw: str) -> str:
    """Strip emoji, asterisks, colons, extra spaces from a field key."""
    s = raw.strip()
    s = _EMOJI_STRIP_RE.sub('', s)
    s = re.sub(r'[\*\_\[\]\(\)\#\~\`]', '', s)
    s = s.strip().strip(':').strip('-').strip()
    s = re.sub(r'\s+', ' ', s)
    return s

def _clean_source_name(raw: str) -> str:
    """Strip emoji + markdown from source title for display."""
    s = raw.strip()
    s = re.sub(r'[\*\_]', '', s)
    s = _EMOJI_STRIP_RE.sub('', s)
    return s.strip()

def parse_html_leak(html_content: bytes) -> tuple:
    """
    Parse HTML leak report (as sent by OSINTINFOSBOT / LeakBase bot) into
    structured sources with descriptions and records.

    Returns: (all_records: list, sources: list)
    Each source: { source, source_clean, description, records: [{field:val,...}] }
    """
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
    except Exception as e:
        print(f"[HTML PARSE] BeautifulSoup error: {e}")
        return [], []

    sources = []

    for block in soup.find_all('div', class_='block'):
        # ── Source name ──
        title_el = block.find('div', class_='block-title')
        raw_title = title_el.get_text(separator='', strip=True) if title_el else 'Unknown'
        source_display = raw_title.strip()           # with emoji (for display)
        source_clean   = _clean_source_name(raw_title)  # without emoji

        text_el = block.find('div', class_='block-text')
        if not text_el:
            sources.append({
                'source': source_display,
                'source_clean': source_clean,
                'description': '',
                'records': []
            })
            continue

        # ── Parse fields from block-text ──
        # Strategy: walk through direct children, collect (key, value) pairs
        # Record boundaries: two consecutive <br> tags with no field between them
        description_parts = []
        in_description    = True   # True until first field is found
        records           = []
        current_rec       = {}
        prev_was_br       = False

        def flush_record():
            nonlocal current_rec
            if current_rec:
                records.append(dict(current_rec))
                current_rec = {}

        children = list(text_el.children)
        i = 0
        while i < len(children):
            child = children[i]

            if isinstance(child, NavigableString):
                text = str(child).strip()
                if text:
                    if in_description:
                        description_parts.append(text)
                    prev_was_br = False
                i += 1
                continue

            if not isinstance(child, Tag):
                i += 1
                continue

            if child.name == 'br':
                if prev_was_br and not in_description:
                    # Double <br> = record separator
                    flush_record()
                prev_was_br = True
                i += 1
                continue

            if child.name == 'b':
                raw_key = child.get_text(separator='', strip=True)
                key = _clean_field_name(raw_key)
                if not key:
                    i += 1
                    continue

                in_description = False
                prev_was_br    = False

                # Collect value: siblings until next <b> or <br>
                val_parts = []
                j = i + 1
                while j < len(children):
                    sib = children[j]
                    if isinstance(sib, NavigableString):
                        t = str(sib).strip()
                        if t:
                            val_parts.append(t)
                        j += 1
                    elif isinstance(sib, Tag):
                        if sib.name == 'b':
                            break   # next field starts
                        elif sib.name == 'br':
                            j += 1
                            break   # end of this field's line
                        elif sib.name in ('code', 'em', 'strong', 'span', 'a', 'i'):
                            t = sib.get_text(strip=True)
                            if t:
                                val_parts.append(t)
                            j += 1
                        else:
                            j += 1
                    else:
                        j += 1

                val = ' '.join(val_parts).strip()
                # Remove leftover markdown chars from value
                val = re.sub(r'[`]', '', val).strip()

                if key and val:
                    if key in current_rec:
                        existing = current_rec[key]
                        if isinstance(existing, list):
                            existing.append(val)
                        else:
                            current_rec[key] = [existing, val]
                    else:
                        current_rec[key] = val

                i = j  # advance to where inner loop ended
                continue

            # Any other tag: extract text if we're in description
            if in_description:
                t = child.get_text(strip=True)
                if t:
                    description_parts.append(t)
            prev_was_br = False
            i += 1

        flush_record()

        # Build clean description (max ~300 chars, no duplicates)
        raw_desc = ' '.join(description_parts).strip()
        # Collapse repeated spaces/newlines
        raw_desc = re.sub(r'\s+', ' ', raw_desc).strip()

        sources.append({
            'source':       source_display,
            'source_clean': source_clean,
            'description':  raw_desc,
            'records':      records
        })

    # Flat list of all records with _source injected
    all_records = []
    for src in sources:
        for rec in src['records']:
            flat = dict(rec)
            flat['_source']       = src['source']
            flat['_source_clean'] = src['source_clean']
            all_records.append(flat)

    return all_records, sources


# ==================== TRUECALLER TEXT PARSER ====================
def parse_response(text, number):
    if not text: return None
    tl = text.lower()
    if "access has been expired" in tl or "don't have access" in tl:
        return {"_status": "ACCESS_EXPIRED", "link": find_link(text)}
    if "click the button" in tl or "get 1 hour access" in text:
        return {"_status": "ACCESS_NEEDED", "link": find_link(text)}
    if "unlocked 1-hour" in tl or "congrats" in tl:
        return {"_status": "ACCESS_GRANTED"}

    lines = [l.strip() for l in text.split('\n')]
    record_starts = []
    for i, line in enumerate(lines):
        if re.match(r'^[^\w]*Record\s+\d+\s*:', line, re.IGNORECASE):
            record_starts.append(i)

    records = []
    if record_starts:
        for idx, start in enumerate(record_starts):
            end = record_starts[idx+1] if idx+1 < len(record_starts) else len(lines)
            rec = parse_block(lines[start:end], number)
            if rec: records.append(rec)
    else:
        rec = parse_block(lines, number)
        if rec: records.append(rec)

    total_results = len(records)
    m = re.search(r'Total\s+Results?\s*[:\-]\s*(\d+)', text, re.IGNORECASE)
    if m: total_results = int(m.group(1))

    country = "Pakistan" if len(clean_num(number)) == 11 else "India"
    return {
        "_status":       "OK",
        "status":        True,
        "country":       country,
        "number":        number,
        "total_records": len(records),
        "total_results": total_results,
        "records":       records,
        "made_by":       get_bot_username()
    }

def parse_block(lines, default_number):
    rec = {}; addr_lines = []; in_addr = False
    for line in lines:
        if not line: continue
        if re.match(r'^[━─\-=\s]+$', line): continue
        if re.match(r'^[^\w]*Record\s+\d+\s*:', line, re.IGNORECASE): continue
        ll = line.lower()
        if any(x in ll for x in ['search results','total records','total results','made by','india mobile','pakistan mobile']):
            in_addr = False; continue
        if re.match(r'^[\U0001F1E0-\U0001F1FF\s]+$', line): continue

        if re.search(r'number\s*[:\-]', ll) and 'alt' not in ll:
            in_addr = False; v = extract_field(line, 'Number')
            if v: rec['number'] = clean_num(v); continue
        if re.search(r'\bname\s*[:\-]', ll):
            in_addr = False; v = extract_field(line, 'Name')
            if v: rec['name'] = v; continue
        if re.search(r'father\s*[:\-]', ll):
            in_addr = False; v = extract_field(line, 'Father')
            if v: rec['father_name'] = v; continue
        if re.search(r'alt.*number\s*[:\-]', ll) or re.search(r'alt_number\s*[:\-]', ll):
            in_addr = False; v = extract_field(line, 'Alt Number', 'Alt_Number', 'Alt')
            if v: rec['alt_number'] = clean_num(v); continue
        if re.search(r'\baddress\s*[:\-]', ll):
            in_addr = True; addr_lines = []
            v = extract_field(line, 'Address')
            if v: addr_lines.append(v); continue
        if re.search(r'mobile\s*[:\-]', ll):
            in_addr = False; v = extract_field(line, 'Mobile')
            if v and 'number' not in rec: rec['number'] = clean_num(v); continue
        if re.search(r'cnic\s*[:\-]', ll):
            in_addr = False; v = extract_field(line, 'CNIC')
            if v: rec['cnic'] = v; continue
        if re.search(r'circle\s*[:\-]', ll) or re.search(r'operator\s*[:\-]', ll):
            if addr_lines: rec['address'] = ' '.join(addr_lines); addr_lines = []
            in_addr = False; v = extract_field(line, 'Circle', 'Operator')
            if v: rec['circle'] = v; continue
        if re.search(r'\bid\s*[:\-]', ll) and 'aid' not in ll:
            if addr_lines: rec['address'] = ' '.join(addr_lines); addr_lines = []
            in_addr = False; v = extract_field(line, 'ID')
            if v: rec['id'] = v; continue
        if re.search(r'email\s*[:\-]', ll):
            in_addr = False; v = extract_field(line, 'Email')
            if v: rec['email'] = v; continue
        if in_addr:
            cl = re.sub(r'^[-•*📍🏠]\s*', '', line).strip()
            if cl and re.search(r'[a-zA-Z0-9]', cl): addr_lines.append(cl)

    if addr_lines: rec['address'] = ' '.join(addr_lines)
    if not (rec.get('name') or rec.get('id') or rec.get('number')): return None
    if 'number' not in rec: rec['number'] = default_number
    ordered = {}
    for key in ['number','name','father_name','alt_number','cnic','address','circle','id','email']:
        if key in rec: ordered[key] = rec[key]
    return ordered

# ==================== ACCESS REFRESH ====================
async def refresh_access(link, session_id, orig_number, acc_id=None):
    print(f"[REFRESH] Starting for {orig_number}, link={link}")
    try:
        if acc_id is not None:
            ac = acc_manager.get_client(acc_id)
            if not ac or not ac.is_connected():
                _, ac = acc_manager.next_client()
        else:
            _, ac = acc_manager.next_client()
        if not ac: return False

        sent_ts = int(time.time())
        await ac.send_message(NICK_BOT, link)
        await asyncio.sleep(5)

        bypass = None
        for attempt in range(3):
            msgs = await ac.get_messages(NICK_BOT, limit=8)
            for msg in msgs:
                if msg.date and int(msg.date.timestamp()) < sent_ts: continue
                if msg.text:
                    m = re.search(r'https://t\.me/[^\s\)\]]+', msg.text)
                    if m: bypass = m.group(0).rstrip('.'); break
                bl = btn_link(msg)
                if bl and "t.me" in bl: bypass = bl.rstrip('.'); break
            if bypass: break
            if attempt < 2: await asyncio.sleep(2)

        if not bypass: return False
        m = re.search(r'[?&]start=([^&\s\)\]]+)', bypass, re.IGNORECASE)
        if not m: return False
        start_payload = re.sub(r'[.*_~)`\'"]+$', '', m.group(1).strip())
        await ac.send_message(TRUECALLER_BOT, f"/start {start_payload}")
        await asyncio.sleep(2)
        await ac.send_message(TRUECALLER_BOT, clean_num(orig_number))
        return True
    except Exception as e:
        print(f"[REFRESH ERROR] {e}")
        return False

# ==================== TRUECALLER EVENT HANDLER ====================
@events.register(events.NewMessage)
async def on_message(event):
    msg = event.message
    if not msg or not msg.text: return
    sender = await event.get_sender()
    uname  = (getattr(sender, 'username', '') or "").lower()
    if 'truecaller_redbot' not in uname: return

    matched_id = None; oldest_ts = float('inf')
    for rid, req in list(pending.items()):
        if req.get("done"): continue
        age = time.time() - req["ts"]
        if age > 180: pending.pop(rid, None); continue
        if req["ts"] < oldest_ts: oldest_ts = req["ts"]; matched_id = rid
    if not matched_id: return

    req    = pending[matched_id]
    result = parse_response(msg.text, req["number"])
    if not result: return
    status = result.get("_status", "OK")

    if status in ("ACCESS_EXPIRED", "ACCESS_NEEDED"):
        link = btn_link(msg) or result.get("link")
        if link:
            pending[matched_id]["ts"] = time.time()
            asyncio.create_task(refresh_access(link, req["session_id"], req["number"], req.get("acc_id")))
        return
    if status == "ACCESS_GRANTED":
        if matched_id in pending: pending[matched_id]["ts"] = time.time()
        return

    pending[matched_id]["result"] = result
    pending[matched_id]["done"]   = True

# ==================== LEAK: HTML DOCUMENT HANDLER ====================
@events.register(events.NewMessage)
async def on_leak_document(event):
    """
    Handle when leak bot sends an HTML file as document/attachment.
    This is the FAST path — no pagination, direct HTML download + parse.
    """
    msg = event.message
    if not msg: return

    sender = await event.get_sender()
    uname  = (getattr(sender, 'username', '') or "").lower()
    lb     = get_leak_bot().lstrip('@').lower()
    if not lb or lb not in uname: return

    # Only handle document messages (bot sends HTML as file)
    if not msg.document: return

    # Check it's an HTML file by mime or filename
    doc  = msg.document
    mime = getattr(doc, 'mime_type', '') or ''
    fname = ''
    if hasattr(doc, 'attributes'):
        for attr in doc.attributes:
            fn = getattr(attr, 'file_name', None)
            if fn:
                fname = fn.lower()
                break

    is_html = ('html' in mime) or fname.endswith('.html') or fname.endswith('.htm')
    if not is_html:
        print(f"[LEAK DOC] Not HTML — mime={mime} fname={fname}, skipping")
        return

    # Find oldest pending leak request
    matched_id = None; oldest_ts = float('inf')
    for rid, req in list(leak_pending.items()):
        if req.get("done"): continue
        age = time.time() - req["ts"]
        if age > 180: leak_pending.pop(rid, None); continue
        if req["ts"] < oldest_ts: oldest_ts = req["ts"]; matched_id = rid
    if not matched_id:
        print("[LEAK DOC] No pending request found, ignoring HTML document")
        return

    print(f"[LEAK DOC] HTML file received ({getattr(doc, 'size', '?')} bytes), downloading...")
    try:
        html_bytes = await event.message.download_media(file=bytes)
    except Exception as e:
        print(f"[LEAK DOC] Download failed: {e}")
        return

    if not html_bytes:
        print("[LEAK DOC] Empty download, skipping")
        return

    print(f"[LEAK DOC] Downloaded {len(html_bytes)} bytes, parsing HTML...")
    try:
        all_records, sources = parse_html_leak(html_bytes)
    except Exception as e:
        print(f"[LEAK DOC] Parse error: {e}")
        return

    # Also extract any buttons (Download, Functions, etc.)
    buttons_data = []
    try:
        if msg.buttons:
            for row in msg.buttons:
                for btn in row:
                    b = {"text": btn.text}
                    if hasattr(btn, 'url') and btn.url:
                        b["url"] = btn.url
                    buttons_data.append(b)
    except Exception:
        pass

    leak_pending[matched_id]["html_records"] = all_records
    leak_pending[matched_id]["html_sources"] = sources
    leak_pending[matched_id]["buttons"]      = buttons_data
    leak_pending[matched_id]["has_data"]     = True
    leak_pending[matched_id]["mode"]         = "html"
    leak_pending[matched_id]["done"]         = True
    print(f"[LEAK DOC] Parsed {len(all_records)} records from {len(sources)} sources")


# ==================== LEAK TEXT FALLBACK PARSER ====================
def parse_leak_text(all_text):
    """
    Fallback: parse raw text (if bot sends text instead of HTML).
    Handles OSINTINFOSBOT text format.
    """
    _EMOJI_RE = re.compile(
        r'^[\U00010000-\U0010ffff\u2000-\u26FF\u2700-\u27BF'
        r'\U0001F300-\U0001F9FF\U0001FA00-\U0001FA9F'
        r'📱📞🏠👤👨📄🌐🔑💾🗂️\s\*\[\]()•\-_=~]+',
        re.UNICODE
    )
    _FIELD_RE = re.compile(r'^(.{1,50}?)\s{0,3}[:\-]\s{1,5}(.+)$')

    def strip_emoji(line):
        return _EMOJI_RE.sub('', line.strip()).strip()

    def is_prose(line):
        stripped = strip_emoji(line)
        if len(stripped) > 120: return True
        if not re.search(r'[:\-]', stripped): return True
        return False

    def try_field(line):
        stripped = strip_emoji(line)
        if not stripped: return None
        m = _FIELD_RE.match(stripped)
        if not m: return None
        key = m.group(1).strip().rstrip(':- ')
        val = m.group(2).strip()
        if len(key) > 50 or len(key) < 2: return None
        if not val: return None
        return key, val

    sources = []
    current_source = None
    current_rec    = {}
    in_data        = False

    def flush_rec():
        nonlocal current_rec
        if current_rec and current_source is not None:
            sources[current_source]["records"].append(dict(current_rec))
            current_rec = {}

    def new_source(name):
        nonlocal current_source, in_data, current_rec
        flush_rec()
        current_rec = {}
        in_data = False
        sources.append({"source": name, "source_clean": _clean_source_name(name),
                        "description": "", "records": []})
        current_source = len(sources) - 1

    for line in all_text.split('\n'):
        raw = line.strip()
        if not raw:
            flush_rec(); continue

        header_candidate = re.sub(r'[\*_\`]', '', strip_emoji(raw)).strip()
        if (header_candidate and len(header_candidate) < 60
                and not re.search(r'[:\-]\s', header_candidate)
                and not is_prose(raw)
                and re.search(r'[A-Za-z]{3,}', header_candidate)
                and raw.startswith('**')):
            flush_rec()
            new_source(header_candidate)
            continue

        if current_source is None:
            new_source("Unknown")

        field = try_field(raw)
        if field:
            key, val = field
            in_data = True
            if key in current_rec:
                existing = current_rec[key]
                if isinstance(existing, list):
                    existing.append(val)
                else:
                    current_rec[key] = [existing, val]
            else:
                current_rec[key] = val
        else:
            if in_data and current_rec:
                flush_rec()

    flush_rec()

    all_records = []
    for src in sources:
        for rec in src["records"]:
            rec["_source"]       = src["source"]
            rec["_source_clean"] = src.get("source_clean", src["source"])
            all_records.append(rec)

    return all_records, sources


# ==================== LEAK TEXT EVENT HANDLERS (fallback) ====================
NEXT_BTN_LABELS = {"▶", "▶️", "→", "➡", "➡️", "Next", "next", ">", ">>", "⇒"}

def _extract_buttons(msg):
    buttons_data = []
    try:
        if msg.buttons:
            for row in msg.buttons:
                for btn in row:
                    b = {"text": btn.text}
                    if hasattr(btn, 'data') and btn.data:
                        b["callback"] = btn.data.decode('utf-8', errors='ignore')
                    if hasattr(btn, 'url') and btn.url:
                        b["url"] = btn.url
                    buttons_data.append(b)
    except Exception:
        pass
    return buttons_data

async def _process_leak_text_event(event, is_edit=False):
    """
    Handle text messages from leak bot.
    PRIMARY PATH: detect 'Download' button → click it → HTML file will arrive.
    FALLBACK: collect text pages if no Download button found.
    """
    msg = event.message
    if not msg: return
    txt = msg.text or ""
    if not txt: return

    sender = await event.get_sender()
    uname  = (getattr(sender, 'username', '') or "").lower()
    lb     = get_leak_bot().lstrip('@').lower()
    if not lb or lb not in uname: return

    matched_id = None; oldest_ts = float('inf')
    for rid, req in list(leak_pending.items()):
        if req.get("done"): continue
        age = time.time() - req["ts"]
        if age > 180: leak_pending.pop(rid, None); continue
        if req["ts"] < oldest_ts: oldest_ts = req["ts"]; matched_id = rid
    if not matched_id: return

    req = leak_pending[matched_id]

    # Already handled via HTML document path — skip all text
    if req.get("mode") == "html":
        return
    # Already clicked Download, wait for HTML document to arrive
    if req.get("download_clicked"):
        return

    # ── Skip pure summary/stats messages (the "faltu" first message) ──
    SUMMARY_MARKERS = [
        "Subjects made:", "The number of leaks:", "Search time:",
        "Number of results:", "A lot of results were found",
        "You are shown", "Mirror (in case", "InfrvsBot",
        "Please note that you use", "subscription reduces"
    ]
    has_summary = any(x in txt for x in SUMMARY_MARKERS)
    has_data    = bool(re.search(
        r'(Telephone|Email|Password|Phone|Nick|Full name|Address|CNIC'
        r'|Login|IP|Hash|Username|Document|Region|Adres|Date)\s*[:\-]',
        txt, re.IGNORECASE
    ))
    if has_summary and not has_data:
        print(f"[LEAK TEXT] Skipping stats/summary message")
        return

    # ── PRIMARY: Look for 'Download' button and click it ──
    buttons_data = _extract_buttons(msg)
    DOWNLOAD_LABELS = {"download", "📥 download", "⬇️ download",
                       "⬇download", "📥download", "télécharger", "скачать"}
    download_btn = next(
        (b for b in buttons_data
         if b.get("text", "").strip().lower() in DOWNLOAD_LABELS),
        None
    )

    if download_btn and not req.get("download_clicked"):
        req["download_clicked"] = True
        req["has_data"]         = True
        print(f"[LEAK TEXT] 'Download' button found — clicking now")
        try:
            if msg.buttons:
                for ri, row in enumerate(msg.buttons):
                    for ci, btn in enumerate(row):
                        if btn.text.strip().lower() in DOWNLOAD_LABELS:
                            await msg.click(ri, ci)
                            print(f"[LEAK TEXT] Clicked Download [{ri},{ci}] ✓")
                            return  # Wait for on_leak_document to fire
        except Exception as e:
            print(f"[LEAK TEXT] Download click failed: {e} — falling back to text collection")
            req["download_clicked"] = False  # allow retry or text fallback

    # ── FALLBACK: Collect text pages (if no Download button / click failed) ──
    has_next = any(b["text"].strip() in NEXT_BTN_LABELS for b in buttons_data)

    if "messages" not in req:
        req["messages"] = []
    seen = req.get("seen_texts", set())
    if txt not in seen:
        req["messages"].append(txt)
        seen.add(txt)
        req["seen_texts"] = seen

    req["current_msg"] = msg
    req["buttons"]     = buttons_data
    req["has_data"]    = True
    req["mode"]        = "text"

    if not has_next:
        req["done"] = True
        print(f"[LEAK TEXT] Last page collected — text fallback done")

@events.register(events.NewMessage)
async def on_leak_message(event):
    await _process_leak_text_event(event, is_edit=False)

@events.register(events.MessageEdited)
async def on_leak_edited(event):
    await _process_leak_text_event(event, is_edit=True)


# ==================== BOT FOOTER (added to every leak response) ====================
def make_footer():
    """Return the bot/dev attribution block for every response."""
    return {
        "bot": get_bot_username(),
        "developer": DEVELOPER_TAG,
        "channel": "t.me/RAJFFLIVE"
    }


# ==================== LEAK LOOKUP ====================
def leak_lookup():
    import json as _json
    from flask import Response as _Resp
    t_start = time.time()

    query = (request.args.get('q') or request.args.get('num') or
             request.args.get('number') or request.args.get('text') or
             request.args.get('email') or '').strip()
    if not query:
        return jsonify({
            "status": False,
            "error": "Missing parameter. Use ?q=anything OR ?num=number",
            **make_footer()
        }), 400

    lb = get_leak_bot()
    if not lb:
        return jsonify({
            "status": False,
            "error": "Leak bot not configured.",
            **make_footer()
        }), 503

    acc_id, acc_client = acc_manager.next_client()
    if not acc_client:
        return jsonify({
            "status": False,
            "error": "No active Telegram accounts",
            **make_footer()
        }), 503

    # Build send message
    fmt = request.args.get('fmt', '').strip()
    if fmt:
        send_msg = fmt.replace('{q}', query).replace('{num}', query)
    else:
        digits = re.sub(r'[^\d]', '', query)
        if len(digits) == 10:
            send_msg = f"+91{digits}"
        elif len(digits) == 11 and digits.startswith('03'):
            send_msg = f"+92{digits[1:]}"
        elif len(digits) == 12 and digits.startswith('91'):
            send_msg = f"+{digits}"
        elif len(digits) == 12 and digits.startswith('92'):
            send_msg = f"+{digits}"
        else:
            send_msg = query

    req_id = f"leak_{int(time.time()*1000)}_{re.sub(r'[^a-zA-Z0-9]','_',query)[:20]}"
    leak_pending[req_id] = {
        "query": query, "ts": time.time(), "done": False,
        "messages": [], "seen_texts": set(), "buttons": [],
        "has_data": False, "mode": None,
        "download_clicked": False,          # True once Download button clicked
        "html_records": None, "html_sources": None
    }

    async def _send():
        await acc_client.send_message(lb, send_msg)

    try:
        asyncio.run_coroutine_threadsafe(_send(), loop).result(timeout=10)
    except Exception as e:
        leak_pending.pop(req_id, None)
        return jsonify({
            "status": False,
            "error": f"Send failed: {e}",
            **make_footer()
        }), 500

    def _source_tag():
        """Attribution tag string placed before and after every source block."""
        bot  = get_bot_username()          # e.g. @RAJFFLIVEBOT
        dev  = DEVELOPER_TAG               # e.g. 👤 @RAJFFLIVE | 📢 t.me/RAJFFLIVE
        return f"🤖 {bot} | {dev}"

    def _format_sources(sources):
        """Wrap every source with tag_before / tag_after attribution."""
        tag = _source_tag()
        out = []
        for src in sources:
            out.append({
                "tag_before":  tag,
                "source":      src.get("source_clean") or src.get("source"),
                "description": src.get("description", ""),
                "records":     src.get("records", []),
                "tag_after":   tag,
            })
        return out

    def build_html_response(req, elapsed):
        """Build response from parsed HTML (fast path)."""
        sources  = req.get("html_sources") or []
        all_recs = req.get("html_records") or []

        nav_labels = {
            "▶","▶️","◀","◀️",
            "←","→","<",">","<<",">>",
            "➡","➡️","⬅","⬅️",
            "⇒","⇐","Next","next","Prev","prev"
        }
        _page_re = re.compile(r"^\d+\\?\d+$")
        final_btns = [
            b for b in req.get("buttons", [])
            if b.get("text","").strip() not in nav_labels
            and not _page_re.match(b.get("text","").strip())
        ]

        return {
            "status": True,
            "query":  query,
            "data": {
                "mode":           "html",
                "total_records":  len(all_recs),
                "sources_count":  len(sources),
                "sources":        _format_sources(sources),
                "action_buttons": final_btns,
            },
            "response_time": elapsed,
            **make_footer()
        }

    def build_text_response(req, elapsed):
        """Build response from text fallback (paginated text pages)."""
        messages = req.get("messages") or []
        nav_labels = {
            "▶","▶️","◀","◀️",
            "←","→","<",">","<<",">>",
            "➡","➡️","⬅","⬅️",
            "⇒","⇐","Next","next","Prev","prev"
        }
        _page_re = re.compile(r"^\d+\\?\d+$")
        final_btns = [
            b for b in req.get("buttons", [])
            if b.get("text","").strip() not in nav_labels
            and not _page_re.match(b.get("text","").strip())
        ]
        all_text    = "\n\n".join(messages)
        all_records, sources = parse_leak_text(all_text)

        return {
            "status": True,
            "query":  query,
            "data": {
                "mode":           "text",
                "total_records":  len(all_records),
                "sources_count":  len(sources),
                "sources":        _format_sources(sources),
                "action_buttons": final_btns,
            },
            "response_time": elapsed,
            **make_footer()
        }

    deadline = time.time() + 60
    while time.time() < deadline:
        req = leak_pending.get(req_id, {})
        if req.get("done"):
            leak_pending.pop(req_id, None)
            elapsed = f"{(time.time() - t_start):.2f}s"
            if req.get("mode") == "html":
                resp_data = build_html_response(req, elapsed)
            else:
                resp_data = build_text_response(req, elapsed)
            return _Resp(_json.dumps(resp_data, ensure_ascii=False), mimetype='application/json')
        time.sleep(0.3)

    # Timeout
    req = leak_pending.pop(req_id, {})
    elapsed = f"{(time.time() - t_start):.2f}s"
    if req.get("has_data"):
        if req.get("mode") == "html":
            return _Resp(_json.dumps(build_html_response(req, elapsed), ensure_ascii=False),
                         mimetype='application/json')
        else:
            return _Resp(_json.dumps(build_text_response(req, elapsed), ensure_ascii=False),
                         mimetype='application/json')

    return jsonify({
        "status": False,
        "error": "Timeout — leak bot didn't respond",
        "response_time": elapsed,
        **make_footer()
    }), 504


@app.route('/leak', methods=['GET'])
@require_key
def api_leak():
    return leak_lookup()

# ==================== COUNTRY-SPECIFIC ENDPOINTS ====================

@app.route('/ind', methods=['GET'])
@require_key
def api_india():
    number = (request.args.get('num') or request.args.get('number', '')).strip()
    if not number:
        return jsonify({"status": False, "error": "Missing num parameter", **make_footer()}), 400
    digits = re.sub(r'[^\d]', '', number)
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    if len(digits) != 10:
        return jsonify({"status": False,
                        "error": "Indian number format galat hai. 10 digit ya +91XXXXXXXXXX dein",
                        **make_footer()}), 400
    g.override_num = digits
    return num_lookup()

@app.route('/pak', methods=['GET'])
@require_key
def api_pakistan():
    number = (request.args.get('num') or request.args.get('number', '')).strip()
    if not number:
        return jsonify({"status": False, "error": "Missing num parameter", **make_footer()}), 400
    digits = re.sub(r'[^\d]', '', number)
    if len(digits) == 12 and digits.startswith('92'):
        digits = '0' + digits[2:]
    if not (len(digits) == 11 and digits.startswith('03')):
        return jsonify({"status": False,
                        "error": "Pakistani number format galat hai. 03XXXXXXXXX ya +923XXXXXXXX dein",
                        **make_footer()}), 400
    g.override_num = digits
    return num_lookup()

# ==================== TRUECALLER LOOKUP ====================
def num_lookup():
    t_start = time.time()
    number  = getattr(g, 'override_num', None) or request.args.get('num') or request.args.get('number', '')
    if not number:
        return jsonify({"status": False, "error": "Missing number", **make_footer()}), 400

    _, num_c, country = valid_num(number)
    if not num_c:
        return jsonify({"status": False, "error": "Empty number", **make_footer()}), 400

    # Cache check
    cached = cache_get(num_c)
    if cached:
        stats["cache_hits"] += 1
        import json as _json
        from flask import Response as _Resp
        resp_data = {
            "status":        True,
            "query":         num_c,
            "data":          {k: v for k, v in cached.items() if k not in ("status", "made_by")},
            "response_time": f"{(time.time() - t_start):.2f}s",
            "cached":        True,
            **make_footer()
        }
        return _Resp(_json.dumps(resp_data, ensure_ascii=False), mimetype='application/json')

    stats["total"] += 1
    session_id = f"s_{int(time.time()*1000)}"
    req_id     = f"{session_id}_{num_c}"

    pending[req_id] = {
        "session_id": session_id,
        "number":     num_c,
        "ts":         time.time(),
        "done":       False,
        "result":     None
    }

    acc_id, acc_client = acc_manager.next_client()
    if not acc_client:
        pending.pop(req_id, None); stats["failed"] += 1
        return jsonify({"status": False, "error": "No active Telegram accounts", **make_footer()}), 503

    pending[req_id]["acc_id"] = acc_id

    async def _send():
        await acc_client.send_message(TRUECALLER_BOT, num_c)

    try:
        asyncio.run_coroutine_threadsafe(_send(), loop).result(timeout=10)
    except Exception as e:
        pending.pop(req_id, None); stats["failed"] += 1
        return jsonify({"status": False, "error": f"Send failed: {e}", **make_footer()}), 500

    deadline = time.time() + 90
    while time.time() < deadline:
        req = pending.get(req_id, {})
        if req.get("done"):
            result = req["result"]
            pending.pop(req_id, None)
            elapsed = f"{(time.time() - t_start):.2f}s"

            if result and result.get("status"):
                stats["success"] += 1
                import json as _json
                from flask import Response as _Resp
                inner = {
                    "country":       result.get("country", country),
                    "number":        num_c,
                    "total_records": result.get("total_records", 0),
                    "total_results": result.get("total_results", 0),
                    "records":       result.get("records", [])
                }
                data = {
                    "status":        True,
                    "query":         num_c,
                    "data":          inner,
                    "response_time": elapsed,
                    **make_footer()
                }
                cache_set(num_c, inner)
                return _Resp(_json.dumps(data, ensure_ascii=False), mimetype='application/json')
            else:
                stats["failed"] += 1
                return jsonify({"status": False,
                                "error": result.get("error", "No data") if result else "No data",
                                "response_time": elapsed,
                                **make_footer()}), 500
        time.sleep(0.3)

    pending.pop(req_id, None); stats["failed"] += 1
    elapsed = f"{(time.time() - t_start):.2f}s"
    return jsonify({"status": False, "error": "Timeout — bot didn't respond in 90s",
                    "response_time": elapsed, **make_footer()}), 504

# ==================== TG LOOKUP ====================
USERID_API     = "https://username-usrid-to-num.onrender.com"
USERID_API_KEY = os.environ.get("USERID_API_KEY", "")

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=15)

async def _parse_user(entity, fallback_id=None):
    name = " ".join(filter(None, [
        getattr(entity, 'first_name', '') or '',
        getattr(entity, 'last_name',  '') or ''
    ])).strip() or getattr(entity, 'title', '') or (f"User {fallback_id}" if fallback_id else "Unknown")
    uname = getattr(entity, 'username', None)
    return {
        "name": name,
        "username":     f"@{uname}" if uname else None,
        "telegram_id":  str(entity.id),
        "public_phone": str(getattr(entity, 'phone', None) or '') or None
    }

async def resolve_username(username):
    uname_clean = username.lstrip('@')
    _, ac = acc_manager.next_client()
    if not ac: return None
    try:
        entity = await ac.get_entity(uname_clean)
        info = await _parse_user(entity, uname_clean)
        if not info["username"]: info["username"] = f"@{uname_clean}"
        return info
    except Exception as e:
        print(f"[TG] resolve_username failed: {e}"); return None

async def resolve_userid(user_id):
    uid = int(user_id)
    _, ac = acc_manager.next_client()
    if not ac: return {"name": f"User {user_id}", "username": None, "telegram_id": str(uid), "public_phone": None}
    try:
        entity = await ac.get_entity(uid)
        return await _parse_user(entity, uid)
    except Exception: pass
    try:
        full = await ac(GetFullUserRequest(uid))
        u = full.users[0] if full and full.users else None
        if u: return await _parse_user(u, uid)
    except Exception: pass
    return {"name": f"User {user_id}", "username": None, "telegram_id": str(uid), "public_phone": None}

def fetch_phone_from_apis(tg_id):
    if USERID_API_KEY:
        try:
            r = requests.get(f"{USERID_API}/userid={tg_id}", params={"key": USERID_API_KEY}, timeout=12)
            if r.status_code == 200:
                d = r.json()
                if d.get("status"):
                    for src_val in d.get("data", {}).values():
                        for rec in src_val.get("records", []):
                            phone = str(rec.get("phone", "")).strip()
                            if phone and phone not in ("None", "", "null"):
                                return {"country": rec.get("country","Unknown"),
                                        "country_code": rec.get("country_code",""),
                                        "phone_number": phone}
        except Exception as e:
            print(f"[TG] userid-api failed: {e}")
    return None

def tg_lookup():
    import json as _json
    from flask import Response as _Resp
    tg = request.args.get('tg', '').strip()
    if not tg: return jsonify({"success": False, "error": "Missing tg param", **make_footer()}), 400

    is_username = not tg.lstrip('@').isdigit()
    if is_username:
        tg_info = run_async(resolve_username(tg))
        if not tg_info: return jsonify({"success": False, "error": "Could not resolve username", **make_footer()}), 404
    else:
        tg_info = run_async(resolve_userid(tg))
        if not tg_info: tg_info = {"name": f"User {tg}", "username": None, "telegram_id": tg}

    tg_id    = tg_info["telegram_id"]
    location = fetch_phone_from_apis(tg_id)
    if not location:
        pub = tg_info.get("public_phone")
        location = {"country": "Unknown", "country_code": "", "phone_number": pub or "Not found"}

    info_key     = "username_info" if is_username else "userid_info"
    phone_num    = location["phone_number"]
    country_code = location["country_code"]
    country      = location["country"]

    if phone_num and phone_num != "Not found" and not country_code:
        PHONE_CC = [("+880","Bangladesh"),("+977","Nepal"),("+94","Sri Lanka"),("+971","UAE"),
                    ("+966","Saudi Arabia"),("+92","Pakistan"),("+91","India"),("+1","USA"),
                    ("+44","UK"),("+98","Iran"),("+90","Turkey"),("+7","Russia"),
                    ("+86","China"),("+81","Japan"),("+82","South Korea"),("+49","Germany"),
                    ("+33","France"),("+39","Italy"),("+55","Brazil"),("+61","Australia")]
        for cc, cname in PHONE_CC:
            digits = cc.replace("+", "")
            if phone_num.startswith(digits):
                country_code = cc
                if country == "Unknown": country = cname
                phone_num = phone_num[len(digits):]
                break

    result = {
        info_key:   {"name": tg_info["name"], "username": tg_info.get("username") or "N/A", "telegram_id": tg_id},
        "location": {"country": country, "country_code": country_code, "phone_number": phone_num},
        **make_footer()
    }
    return _Resp(_json.dumps(result, ensure_ascii=False), mimetype='application/json')

@app.route('/api', methods=['GET'])
@require_key
def api_tg_check():
    tg = request.args.get('tg', '').strip()
    return tg_lookup() if tg else num_lookup()

@app.route('/api/tg', methods=['GET'])
@require_key
def api_tg_direct():
    return tg_lookup()

@app.route('/api/health')
def health():
    active_ids = acc_manager.get_active_ids()
    connected  = [aid for aid in active_ids if acc_manager.get_client(aid) and acc_manager.get_client(aid).is_connected()]
    return jsonify({
        "status": "ok",
        "accounts_active":    len(active_ids),
        "accounts_connected": len(connected),
        "pending":            len(pending),
        "stats":              stats,
        "cache":              cache_stats(),
        **make_footer()
    })

@app.route('/')
def home():
    from flask import Response as _R
    bot_un = get_bot_username()
    return _R(
        json.dumps({
            "status": True,
            "name": "RAJFF API",
            "version": "3.0",
            "endpoints": {
                "/api":   "Truecaller lookup (?num=NUMBER&key=KEY)",
                "/ind":   "India number lookup (?num=10DIGIT&key=KEY)",
                "/pak":   "Pakistan number lookup (?num=03XXXXXXXXX&key=KEY)",
                "/leak":  "Leak database lookup (?q=NUMBER_OR_EMAIL&key=KEY)",
                "/api/tg":"Telegram username/ID lookup (?tg=@username&key=KEY)"
            },
            "bot":       bot_un,
            "developer": DEVELOPER_TAG,
            "channel":   "t.me/RAJFFLIVE"
        }, ensure_ascii=False),
        mimetype='application/json'
    )

# ==================== ADMIN LOGIN ====================

@app.route('/admin/login', methods=['POST'])
def admin_login():
    data = request.json or {}
    key  = data.get("key", "")
    if key != ADMIN_KEY:
        return jsonify({"success": False, "error": "Wrong admin key"}), 403
    resp = make_response(jsonify({"success": True}))
    resp.set_cookie("adm_key", key, max_age=86400 * 7, httponly=True, samesite="Lax")
    return resp

@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    resp = make_response(jsonify({"success": True}))
    resp.delete_cookie("adm_key")
    return resp

@app.route('/admin')
def admin_panel():
    from flask import Response as _R
    return _R(ADMIN_HTML, mimetype='text/html')

# ==================== ADMIN API ENDPOINTS ====================

@app.route('/admin/stats')
@require_admin
def admin_stats():
    conn = get_db()
    total_keys     = conn.execute("SELECT COUNT(*) FROM api_keys WHERE active=1").fetchone()[0]
    total_accounts = conn.execute("SELECT COUNT(*) FROM accounts WHERE active=1").fetchone()[0]
    conn.close()
    return jsonify({
        "success":        True,
        "total_keys":     total_keys,
        "total_accounts": total_accounts,
        "pending":        len(pending),
        "stats":          stats,
        "cache":          cache_stats(),
        "bot_username":   get_bot_username(),
        "leak_bot":       get_leak_bot()
    })

@app.route('/admin/keys')
@require_admin
def admin_list_keys():
    conn = get_db()
    rows = conn.execute("SELECT * FROM api_keys ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify({"success": True, "keys": [dict(r) for r in rows]})

@app.route('/admin/keys/create', methods=['POST'])
@require_admin
def admin_create_key():
    data  = request.json or {}
    name  = data.get("name", "").strip()
    days  = int(data.get("days", 0))
    limit = int(data.get("daily_limit", 0))
    if not name: return jsonify({"success": False, "error": "Name required"}), 400
    key    = str(uuid.uuid4()).replace("-", "")
    expiry = (datetime.now() + timedelta(days=days)).isoformat() if days > 0 else None
    conn = get_db()
    conn.execute("INSERT INTO api_keys (key,name,created,expiry,active,uses,daily_limit) VALUES (?,?,?,?,1,0,?)",
                 (key, name, datetime.now().isoformat(), expiry, limit))
    conn.commit(); conn.close()
    return jsonify({"success": True, "key": key, "expiry": expiry, "daily_limit": limit})

@app.route('/admin/keys/revoke', methods=['POST'])
@require_admin
def admin_revoke_key():
    data = request.json or {}
    key  = data.get("key", "")
    if not key: return jsonify({"success": False, "error": "Key required"}), 400
    conn = get_db()
    conn.execute("UPDATE api_keys SET active=0 WHERE key=?", (key,))
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route('/admin/accounts')
@require_admin
def admin_list_accounts():
    conn = get_db()
    rows = conn.execute("SELECT id,name,api_id,active,created FROM accounts").fetchall()
    conn.close()
    accounts = []
    for r in rows:
        d = dict(r)
        d["connected"] = bool(acc_manager.get_client(r["id"]) and acc_manager.get_client(r["id"]).is_connected())
        accounts.append(d)
    return jsonify({"success": True, "accounts": accounts})

@app.route('/admin/accounts/add', methods=['POST'])
@require_admin
def admin_add_account():
    data           = request.json or {}
    name           = data.get("name","").strip()
    api_id         = data.get("api_id","").strip()
    api_hash       = data.get("api_hash","").strip()
    session_string = data.get("session_string","").strip()
    if not all([name, api_id, api_hash, session_string]):
        return jsonify({"success": False, "error": "All fields required"}), 400
    conn = get_db()
    try:
        conn.execute("INSERT INTO accounts (name,api_id,api_hash,session_string,active,created) VALUES (?,?,?,?,1,?)",
                     (name, api_id, api_hash, session_string, datetime.now().isoformat()))
        conn.commit()
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400
    finally:
        conn.close()
    return jsonify({"success": True})

@app.route('/admin/accounts/remove', methods=['POST'])
@require_admin
def admin_remove_account():
    data   = request.json or {}
    acc_id = data.get("id")
    if not acc_id: return jsonify({"success": False, "error": "ID required"}), 400
    client = acc_manager.remove_client(acc_id)
    if client: asyncio.run_coroutine_threadsafe(client.disconnect(), loop)
    conn = get_db()
    conn.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route('/admin/accounts/start', methods=['POST'])
@require_admin
def admin_start_account():
    data   = request.json or {}
    acc_id = data.get("id")
    if not acc_id: return jsonify({"success": False, "error": "ID required"}), 400
    conn = get_db()
    row  = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    conn.close()
    if not row: return jsonify({"success": False, "error": "Account not found"}), 404

    async def _start():
        client = TelegramClient(StringSession(row["session_string"]), int(row["api_id"]), row["api_hash"])
        await client.connect()
        if not await client.is_user_authorized(): return False
        acc_manager.set_client(acc_id, client)
        client.add_event_handler(on_message)
        client.add_event_handler(on_leak_document)
        client.add_event_handler(on_leak_message)
        client.add_event_handler(on_leak_edited)
        return True

    try:
        ok = asyncio.run_coroutine_threadsafe(_start(), loop).result(timeout=20)
        return jsonify({"success": ok, "error": None if ok else "Auth failed"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/admin/cache/clear', methods=['POST'])
@require_admin
def admin_clear_cache():
    cache_clear()
    return jsonify({"success": True, "message": "Cache cleared"})

@app.route('/admin/config', methods=['GET'])
@require_admin
def admin_get_config():
    return jsonify({"success": True, "bot_username": get_bot_username(),
                    "leak_bot": get_leak_bot(), "cache_ttl": CACHE_TTL})

@app.route('/admin/config', methods=['POST'])
@require_admin
def admin_set_config():
    data = request.json or {}
    if "bot_username" in data:
        val = str(data["bot_username"]).strip()
        if not val.startswith("@"): val = "@" + val
        set_config("bot_username", val)
    if "leak_bot" in data:
        val = str(data["leak_bot"]).strip()
        if val and not val.startswith("@"): val = "@" + val
        set_config("leak_bot", val)
    return jsonify({"success": True, "bot_username": get_bot_username(), "leak_bot": get_leak_bot()})

# ==================== ADMIN HTML ====================

ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>RAJFF API — Admin</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --pu:#7c5cfc;--pu2:#5b3fd8;--pu-glow:rgba(124,92,252,.25);
  --bg:#0d0d0f;--bg2:#13131a;--bg3:#1a1a24;--bg4:#1f1f2e;
  --brd:#252535;--brd2:#2e2e42;
  --gr:#22c55e;--rd:#ef4444;--yw:#f59e0b;
  --tx:#8888aa;--tx2:#c0c0d8;
}
body{background:var(--bg);color:#e2e2f0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
#loginPage{display:flex;align-items:center;justify-content:center;min-height:100vh;
  background:radial-gradient(ellipse at 50% 0%,rgba(124,92,252,.12) 0%,transparent 70%)}
.login-box{background:var(--bg2);border:1px solid var(--brd2);border-radius:20px;
  padding:40px 36px;width:100%;max-width:380px;text-align:center;
  box-shadow:0 0 60px rgba(124,92,252,.15)}
.login-logo{width:56px;height:56px;background:linear-gradient(135deg,var(--pu),#a855f7);
  border-radius:16px;display:flex;align-items:center;justify-content:center;
  font-size:1.6rem;margin:0 auto 20px;box-shadow:0 0 24px var(--pu-glow)}
.login-box h2{font-size:1.3rem;font-weight:800;margin-bottom:6px}
.login-box p{font-size:.8rem;color:var(--tx);margin-bottom:28px}
.login-input{width:100%;background:var(--bg3);border:1px solid var(--brd2);border-radius:12px;
  padding:13px 16px;color:#e2e2f0;font-size:.9rem;outline:none;text-align:center;
  letter-spacing:1px;transition:border .2s,box-shadow .2s;margin-bottom:14px}
.login-input:focus{border-color:var(--pu);box-shadow:0 0 0 3px var(--pu-glow)}
.login-btn{width:100%;background:linear-gradient(135deg,var(--pu),var(--pu2));color:#fff;
  border:none;border-radius:12px;padding:13px;font-size:.9rem;font-weight:700;
  cursor:pointer;transition:all .2s;box-shadow:0 4px 16px rgba(92,63,216,.4)}
.login-btn:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(92,63,216,.5)}
.login-err{color:#f87171;font-size:.8rem;margin-top:10px;min-height:20px}
#mainPanel{display:none}
header{background:linear-gradient(135deg,#110d2e 0%,#0d0d0f 60%);border-bottom:1px solid var(--brd);
  padding:16px 28px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:99;backdrop-filter:blur(10px)}
.logo{width:36px;height:36px;background:linear-gradient(135deg,var(--pu),#a855f7);border-radius:10px;
  display:flex;align-items:center;justify-content:center;box-shadow:0 0 16px var(--pu-glow);font-size:1.1rem}
header h1{font-size:1.2rem;font-weight:700;flex:1}
.hbadge{font-size:.7rem;color:var(--tx);background:var(--bg3);border:1px solid var(--brd2);padding:3px 10px;border-radius:20px}
.pulse{width:8px;height:8px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 2px rgba(34,197,94,.3);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 2px rgba(34,197,94,.3)}50%{box-shadow:0 0 0 5px rgba(34,197,94,.1)}}
.wrap{max-width:960px;margin:0 auto;padding:24px 16px}
.stats-row{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:24px}
.stat{background:var(--bg2);border:1px solid var(--brd);border-radius:14px;padding:16px 10px;
  text-align:center;transition:border .2s,transform .15s}
.stat:hover{border-color:var(--brd2);transform:translateY(-2px)}
.stat .val{font-size:1.6rem;font-weight:800;background:linear-gradient(135deg,#a78bfa,#7c5cfc);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat .lbl{font-size:.63rem;color:var(--tx);margin-top:4px;text-transform:uppercase;letter-spacing:1px}
.stat .ico{font-size:1.3rem;margin-bottom:5px}
.card{background:var(--bg2);border:1px solid var(--brd);border-radius:16px;padding:24px;margin-bottom:20px}
.card:hover{border-color:var(--brd2)}
.card-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
.card-hd h2{font-size:.75rem;font-weight:700;letter-spacing:1.8px;color:var(--tx);
  text-transform:uppercase;display:flex;align-items:center;gap:8px}
.card-hd h2 .dot{width:6px;height:6px;border-radius:50%;background:var(--pu)}
label{display:block;font-size:.71rem;color:var(--tx);margin-bottom:6px;margin-top:14px;
  text-transform:uppercase;letter-spacing:.9px;font-weight:600}
label:first-of-type{margin-top:0}
input,textarea{width:100%;background:var(--bg3);border:1px solid var(--brd2);border-radius:10px;
  padding:11px 14px;color:#e2e2f0;font-size:.87rem;outline:none;transition:border .2s,box-shadow .2s}
input:focus,textarea:focus{border-color:var(--pu);box-shadow:0 0 0 3px var(--pu-glow)}
textarea{resize:vertical;min-height:80px;font-family:monospace;font-size:.78rem}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
.btn-row{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap;align-items:center}
button{background:linear-gradient(135deg,var(--pu),var(--pu2));color:#fff;border:none;border-radius:10px;
  padding:10px 20px;font-size:.84rem;font-weight:600;cursor:pointer;display:inline-flex;align-items:center;
  gap:7px;transition:all .2s;box-shadow:0 4px 12px rgba(92,63,216,.3)}
button:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(92,63,216,.45)}
button:active{transform:scale(.97);box-shadow:none}
button.danger{background:linear-gradient(135deg,#dc2626,#991b1b);box-shadow:0 4px 12px rgba(220,38,38,.25)}
button.danger:hover{box-shadow:0 6px 18px rgba(220,38,38,.4)}
button.success{background:linear-gradient(135deg,#16a34a,#15803d);box-shadow:0 4px 12px rgba(22,163,74,.25)}
button.ghost{background:transparent;border:1px solid var(--brd2);color:var(--tx2);box-shadow:none}
button.ghost:hover{border-color:var(--pu);color:#e2e2f0;background:var(--bg3)}
button.sm{padding:6px 12px;font-size:.75rem;border-radius:7px}
.toast{position:fixed;top:20px;right:20px;z-index:999;padding:12px 18px;border-radius:12px;
  font-size:.85rem;font-weight:600;display:flex;align-items:center;gap:10px;
  transform:translateX(120%);transition:transform .35s cubic-bezier(.34,1.56,.64,1);
  max-width:360px;box-shadow:0 8px 32px rgba(0,0,0,.5)}
.toast.show{transform:translateX(0)}
.toast.ok{background:#14532d;color:#4ade80;border:1px solid #166534}
.toast.err{background:#450a0a;color:#f87171;border:1px solid #7f1d1d}
.toast.info{background:#1e1b4b;color:#a78bfa;border:1px solid #3730a3}
.key-result{background:var(--bg4);border:1px solid var(--pu);border-radius:12px;padding:16px 18px;
  margin-top:16px;display:none;animation:fadeIn .3s ease}
.key-result.show{display:block}
.key-label{font-size:.68rem;color:var(--tx);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.key-value{font-family:'Courier New',monospace;font-size:.9rem;color:#a78bfa;word-break:break-all;
  background:var(--bg3);border:1px solid var(--brd2);border-radius:8px;padding:10px 14px;margin-bottom:10px}
.key-meta{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.key-meta span{font-size:.72rem;color:var(--tx2);background:var(--bg3);border:1px solid var(--brd2);padding:3px 10px;border-radius:20px}
@keyframes fadeIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}
.tbl-wrap{overflow-x:auto;border-radius:10px;border:1px solid var(--brd)}
table{width:100%;border-collapse:collapse;font-size:.82rem;min-width:560px}
th{color:var(--tx);font-weight:700;text-transform:uppercase;font-size:.67rem;letter-spacing:1px;
  padding:10px 14px;border-bottom:1px solid var(--brd);text-align:left;background:var(--bg3)}
td{padding:11px 14px;border-bottom:1px solid var(--brd);vertical-align:middle;color:var(--tx2)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(124,92,252,.05)}
.badge{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:20px;font-size:.68rem;font-weight:700}
.badge.green{background:rgba(34,197,94,.12);color:#4ade80;border:1px solid rgba(34,197,94,.2)}
.badge.red{background:rgba(239,68,68,.12);color:#f87171;border:1px solid rgba(239,68,68,.2)}
.badge.blue{background:rgba(96,165,250,.12);color:#60a5fa;border:1px solid rgba(96,165,250,.2)}
.key-cell{display:flex;align-items:center;gap:8px}
.key-code{font-family:monospace;font-size:.78rem;color:#a78bfa;cursor:pointer;background:var(--bg3);
  border:1px solid var(--brd2);padding:3px 8px;border-radius:6px;max-width:160px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.key-code:hover{border-color:var(--pu)}
.copy-icon{cursor:pointer;opacity:.5;font-size:.85rem;transition:opacity .2s;flex-shrink:0}
.copy-icon:hover{opacity:1}
.empty{text-align:center;padding:32px;color:var(--tx)}
.empty .ei{font-size:2rem;margin-bottom:8px}
.empty p{font-size:.82rem}
.divider{border:none;border-top:1px solid var(--brd);margin:20px 0}
.info-box{background:var(--bg3);border:1px solid var(--brd2);border-radius:10px;padding:12px 16px;
  font-size:.8rem;color:var(--tx2);margin-top:10px;line-height:1.6}
.info-box code{color:#a78bfa;background:var(--bg4);padding:1px 5px;border-radius:4px;font-size:.75rem}
.cache-bar{display:flex;align-items:center;gap:12px;background:var(--bg3);border:1px solid var(--brd2);
  border-radius:10px;padding:12px 16px;margin-top:12px}
.cache-bar .ci{font-size:.75rem;color:var(--tx2)}
.cache-bar .cv{font-size:.85rem;font-weight:700;color:#a78bfa}
@media(max-width:640px){.stats-row{grid-template-columns:1fr 1fr}.row2,.row3{grid-template-columns:1fr}.wrap{padding:16px 12px}}
</style>
</head>
<body>
<div class="toast" id="toast"></div>
<div id="loginPage">
  <div class="login-box">
    <div class="login-logo">⚡</div>
    <h2>RAJFF Admin</h2>
    <p>Enter your admin key to continue</p>
    <input class="login-input" id="loginKey" type="password" placeholder="Admin Key" onkeydown="if(event.key==='Enter')doLogin()"/>
    <button class="login-btn" onclick="doLogin()">🔐 Login</button>
    <div class="login-err" id="loginErr"></div>
  </div>
</div>
<div id="mainPanel">
<header>
  <div class="logo">⚡</div>
  <h1>RAJFF API</h1>
  <div class="hbadge">Admin Panel v3.0</div>
  <div style="flex:1"></div>
  <button class="ghost sm" onclick="doLogout()" style="margin-right:8px">🚪 Logout</button>
  <div class="pulse" title="Online"></div>
</header>
<div class="wrap">
  <div class="stats-row">
    <div class="stat"><div class="ico">🔑</div><div class="val" id="st-keys">—</div><div class="lbl">API Keys</div></div>
    <div class="stat"><div class="ico">👤</div><div class="val" id="st-accs">—</div><div class="lbl">Accounts</div></div>
    <div class="stat"><div class="ico">⏳</div><div class="val" id="st-pend">—</div><div class="lbl">Pending</div></div>
    <div class="stat"><div class="ico">💾</div><div class="val" id="st-cache">—</div><div class="lbl">Cached</div></div>
    <div class="stat"><div class="ico">📈</div><div class="val" id="st-hits">—</div><div class="lbl">Cache Hits</div></div>
  </div>
  <div class="card">
    <div class="card-hd"><h2><span class="dot"></span>Bot Settings</h2></div>
    <label>Bot Username (shown in all API responses)</label>
    <input id="cfgBotUn" placeholder="@RAJFFLIVEBOT"/>
    <label>Leak Bot Username (/leak endpoint — HTML document mode)</label>
    <input id="cfgLeakBot" placeholder="@LeakBotUsername"/>
    <div class="info-box">
      💡 <code>/leak?q=7985470106&key=KEY</code> — Bot ke HTML file ko directly download karke parse karta hai. Text pagination nahi hoti.<br/>
      📌 Leak bot ko configure karo jo HTML file send karta ho (jaise OSINTINFOSBOT).
    </div>
    <div class="btn-row"><button onclick="saveConfig()">💾 Save Settings</button></div>
  </div>
  <div class="card">
    <div class="card-hd"><h2><span class="dot"></span>Cache</h2></div>
    <p style="font-size:.82rem;color:var(--tx2)">Results are cached in memory. Cache is cleared on server restart.</p>
    <div class="cache-bar">
      <span class="ci">Valid entries:</span><span class="cv" id="cacheValid">—</span>
      <span class="ci" style="margin-left:12px">Total entries:</span><span class="cv" id="cacheTotal">—</span>
      <span class="ci" style="margin-left:12px">TTL:</span><span class="cv" id="cacheTtl">—</span>
    </div>
    <div class="btn-row"><button class="danger" onclick="clearCache()">🗑️ Clear Cache</button></div>
  </div>
  <div class="card">
    <div class="card-hd"><h2><span class="dot"></span>Generate API Key</h2></div>
    <div class="row3">
      <div><label>Key Name / Owner</label><input id="kName" placeholder="e.g. My App"/></div>
      <div><label>Expiry Days (0 = Forever)</label><input id="kDays" type="number" value="30" min="0"/></div>
      <div><label>Daily Limit (0 = Unlimited)</label><input id="kLimit" type="number" value="100" min="0"/></div>
    </div>
    <div class="btn-row"><button onclick="genKey()">⚡ Generate Key</button></div>
    <div class="key-result" id="keyResult">
      <div class="key-label">✅ Key Generated</div>
      <div class="key-value" id="keyDisplay">—</div>
      <div class="key-meta" id="keyMeta"></div>
      <div class="btn-row">
        <button style="background:linear-gradient(135deg,#1d4ed8,#1e40af)" onclick="copyKey()">📋 Copy Key</button>
        <button class="ghost sm" onclick="document.getElementById('keyResult').classList.remove('show')">✕</button>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="card-hd">
      <h2><span class="dot"></span>Active API Keys</h2>
      <button class="ghost sm" onclick="loadKeys()">↻ Refresh</button>
    </div>
    <div class="tbl-wrap"><table>
      <thead><tr><th>API Key</th><th>Name</th><th>Expiry</th><th>Daily Limit</th><th>Uses</th><th>Status</th><th>Action</th></tr></thead>
      <tbody id="keysTbl"><tr><td colspan="7"><div class="empty"><div class="ei">⏳</div><p>Loading...</p></div></td></tr></tbody>
    </table></div>
  </div>
  <hr class="divider"/>
  <div class="card">
    <div class="card-hd"><h2><span class="dot"></span>Add Telegram Account</h2></div>
    <div class="row2">
      <div><label>Account Name</label><input id="aName" placeholder="e.g. Account 2"/></div>
      <div><label>API ID</label><input id="aApiId" placeholder="12345678"/></div>
    </div>
    <label>API Hash</label><input id="aApiHash" placeholder="32 char hash"/>
    <label>Session String (Telethon)</label>
    <textarea id="aSession" placeholder="Paste Telethon StringSession here..."></textarea>
    <div class="info-box">
      💡 Env vars: <code>STRING_SESSION</code>, <code>STRING_SESSION_2</code> ... <code>STRING_SESSION_5</code>
    </div>
    <div class="btn-row"><button class="success" onclick="addAccount()">➕ Add Account</button></div>
  </div>
  <div class="card">
    <div class="card-hd">
      <h2><span class="dot"></span>Telegram Accounts</h2>
      <button class="ghost sm" onclick="loadAccounts()">↻ Refresh</button>
    </div>
    <div class="tbl-wrap"><table>
      <thead><tr><th>Name</th><th>API ID</th><th>Active</th><th>Connected</th><th>Actions</th></tr></thead>
      <tbody id="accsTbl"><tr><td colspan="5"><div class="empty"><div class="ei">⏳</div><p>Loading...</p></div></td></tr></tbody>
    </table></div>
  </div>
</div>
</div>
<script>
let ADM=''; let _lastKey='';
function toast(msg,type='ok'){
  const el=document.getElementById('toast');
  el.className='toast '+type;
  el.innerHTML=(type==='ok'?'✅':type==='err'?'❌':'ℹ️')+' <span>'+msg+'</span>';
  el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'),3500);
}
function copyText(text,label){
  navigator.clipboard.writeText(text).then(()=>toast((label||'Text')+' copied!'))
  .catch(()=>{const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta);toast((label||'Text')+' copied!');});
}
function copyKey(){if(_lastKey)copyText(_lastKey,'API Key');}
async function apiGet(path){
  try{const sep=path.includes('?')?'&':'?';const r=await fetch(path+sep+'key='+ADM,{credentials:'include'});return r.json();}
  catch(e){return{success:false,error:'Network error'};}
}
async function apiPost(path,body){
  try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,key:ADM}),credentials:'include'});return r.json();}
  catch(e){return{success:false,error:'Network error'};}
}
async function doLogin(){
  const key=document.getElementById('loginKey').value.trim();
  if(!key){document.getElementById('loginErr').textContent='Enter your admin key';return;}
  const btn=document.querySelector('.login-btn');btn.disabled=true;btn.textContent='🔄 Checking...';
  try{
    const r=await fetch('/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key}),credentials:'include'});
    const d=await r.json();
    if(d.success){ADM=key;document.getElementById('loginPage').style.display='none';document.getElementById('mainPanel').style.display='block';loadAll();}
    else document.getElementById('loginErr').textContent='❌ Wrong admin key';
  }catch(e){document.getElementById('loginErr').textContent='Network error';}
  btn.disabled=false;btn.textContent='🔐 Login';
}
async function doLogout(){
  await fetch('/admin/logout',{method:'POST',credentials:'include'});
  ADM='';document.getElementById('mainPanel').style.display='none';document.getElementById('loginPage').style.display='flex';document.getElementById('loginKey').value='';
}
async function loadStats(){
  const d=await apiGet('/admin/stats');if(!d.success)return;
  document.getElementById('st-keys').textContent=d.total_keys??'—';
  document.getElementById('st-accs').textContent=d.total_accounts??'—';
  document.getElementById('st-pend').textContent=d.pending??'—';
  document.getElementById('st-cache').textContent=d.cache?.valid??'—';
  document.getElementById('st-hits').textContent=d.stats?.cache_hits??'—';
  document.getElementById('cacheValid').textContent=d.cache?.valid??'—';
  document.getElementById('cacheTotal').textContent=d.cache?.total??'—';
  document.getElementById('cacheTtl').textContent=(d.cache?.ttl_hours??'—')+'h';
  const inp=document.getElementById('cfgBotUn');if(inp&&!inp.value)inp.value=d.bot_username||'';
  const inp2=document.getElementById('cfgLeakBot');if(inp2&&!inp2.value)inp2.value=d.leak_bot||'';
}
async function saveConfig(){
  const val=document.getElementById('cfgBotUn').value.trim();
  const val2=document.getElementById('cfgLeakBot').value.trim();
  if(!val){toast('Enter a bot username','err');return;}
  const d=await apiPost('/admin/config',{bot_username:val,leak_bot:val2});
  if(d.success){toast('Settings saved!','ok');loadStats();}else toast(d.error||'Failed','err');
}
async function clearCache(){
  if(!confirm('Clear all cached results?'))return;
  const d=await apiPost('/admin/cache/clear',{});
  if(d.success){toast('Cache cleared','ok');loadStats();}else toast(d.error||'Failed','err');
}
async function loadKeys(){
  const d=await apiGet('/admin/keys');const tb=document.getElementById('keysTbl');
  if(!d.success){tb.innerHTML='<tr><td colspan="7"><div class="empty"><div class="ei">🔐</div><p>Auth failed</p></div></td></tr>';return;}
  if(!d.keys.length){tb.innerHTML='<tr><td colspan="7"><div class="empty"><div class="ei">🗝️</div><p>No API keys</p></div></td></tr>';return;}
  tb.innerHTML=d.keys.map(k=>{
    const expiry=k.expiry?k.expiry.split('T')[0]:'<span style="color:#4ade80">Forever</span>';
    const limit=k.daily_limit>0?k.daily_limit:'<span style="color:#4ade80">∞</span>';
    const sk=k.key.slice(0,8)+'...'+k.key.slice(-4);
    return`<tr><td><div class="key-cell"><span class="key-code" title="${k.key}" onclick="copyText('${k.key}','API Key')">${sk}</span><span class="copy-icon" onclick="copyText('${k.key}','API Key')">📋</span></div></td><td><b style="color:#e2e2f0">${k.name}</b></td><td>${expiry}</td><td>${limit}</td><td><span class="badge blue">${k.uses}</span></td><td><span class="badge ${k.active?'green':'red'}">${k.active?'● Active':'● Off'}</span></td><td><button class="danger sm" onclick="delKey('${k.key}')">Revoke</button></td></tr>`;
  }).join('');
}
async function genKey(){
  const name=document.getElementById('kName').value.trim();
  const days=parseInt(document.getElementById('kDays').value)||0;
  const limit=parseInt(document.getElementById('kLimit').value)||0;
  if(!name){toast('Enter a key name','err');return;}
  const btn=event.target.closest('button');btn.disabled=true;btn.textContent='⏳...';
  const d=await apiPost('/admin/keys/create',{name,days,daily_limit:limit});
  btn.disabled=false;btn.innerHTML='⚡ Generate Key';
  if(d.success){_lastKey=d.key;document.getElementById('keyDisplay').textContent=d.key;document.getElementById('keyMeta').innerHTML=`<span>👤 ${name}</span><span>📅 ${d.expiry?'Expires '+d.expiry.split('T')[0]:'Never'}</span><span>🔢 ${limit>0?limit+'/day':'Unlimited'}</span>`;document.getElementById('keyResult').classList.add('show');toast('Key generated for '+name,'ok');loadKeys();loadStats();}
  else toast(d.error||'Failed','err');
}
async function delKey(key){
  if(!confirm('Revoke this key?'))return;
  const d=await apiPost('/admin/keys/revoke',{key});
  if(d.success){toast('Key revoked','ok');loadKeys();loadStats();}else toast(d.error||'Failed','err');
}
async function loadAccounts(){
  const d=await apiGet('/admin/accounts');const tb=document.getElementById('accsTbl');
  if(!d.success){tb.innerHTML='<tr><td colspan="5"><div class="empty"><div class="ei">🔐</div><p>Auth failed</p></div></td></tr>';return;}
  if(!d.accounts.length){tb.innerHTML='<tr><td colspan="5"><div class="empty"><div class="ei">👤</div><p>No accounts</p></div></td></tr>';return;}
  tb.innerHTML=d.accounts.map(a=>`<tr><td><b style="color:#e2e2f0">${a.name}</b></td><td><span class="badge blue">${a.api_id}</span></td><td><span class="badge ${a.active?'green':'red'}">${a.active?'● Active':'● Off'}</span></td><td><span class="badge ${a.connected?'green':'red'}">${a.connected?'🟢 Online':'🔴 Offline'}</span></td><td style="display:flex;gap:6px"><button class="success sm" onclick="startAcc(${a.id})">▶ Start</button><button class="danger sm" onclick="delAcc(${a.id})">✕ Remove</button></td></tr>`).join('');
}
async function addAccount(){
  const name=document.getElementById('aName').value.trim();const api_id=document.getElementById('aApiId').value.trim();
  const api_hash=document.getElementById('aApiHash').value.trim();const session_string=document.getElementById('aSession').value.trim();
  if(!name||!api_id||!api_hash||!session_string){toast('Fill all fields','err');return;}
  const btn=event.target.closest('button');btn.disabled=true;btn.textContent='⏳ Adding...';
  const d=await apiPost('/admin/accounts/add',{name,api_id,api_hash,session_string});
  btn.disabled=false;btn.innerHTML='➕ Add Account';
  if(d.success){toast('Account added!','ok');loadAccounts();loadStats();['aName','aApiId','aApiHash','aSession'].forEach(id=>document.getElementById(id).value='');}
  else toast(d.error||'Failed','err');
}
async function startAcc(id){const d=await apiPost('/admin/accounts/start',{id});if(d&&d.success)toast('Account started!','ok');else toast('Start: '+(d&&d.error?d.error:'Restart if needed'),'info');setTimeout(loadAccounts,2000);}
async function delAcc(id){if(!confirm('Remove this account?'))return;const d=await apiPost('/admin/accounts/remove',{id});if(d.success){toast('Account removed','ok');loadAccounts();loadStats();}else toast(d.error||'Failed','err');}
function loadAll(){loadStats();loadKeys();loadAccounts();}
setInterval(()=>{if(ADM)loadStats();},15000);
</script>
</body>
</html>"""

# ==================== STARTUP ====================
async def start_all_clients():
    conn = get_db()
    rows = conn.execute("SELECT * FROM accounts WHERE active=1").fetchall()
    conn.close()
    for row in rows:
        try:
            client = TelegramClient(StringSession(row["session_string"]), int(row["api_id"]), row["api_hash"])
            await client.connect()
            if not await client.is_user_authorized():
                print(f"[ACC] {row['name']} — NOT authorized"); continue
            acc_manager.set_client(row["id"], client)
            client.add_event_handler(on_message)
            client.add_event_handler(on_leak_document)   # HTML fast path (NEW)
            client.add_event_handler(on_leak_message)    # Text fallback
            client.add_event_handler(on_leak_edited)     # Text edit fallback
            print(f"[ACC] {row['name']} — connected OK")
        except Exception as e:
            print(f"[ACC] {row['name']} — failed: {e}")

def run_telegram():
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_all_clients())
    loop.run_forever()

if __name__ == '__main__':
    init_db()
    seed_permanent_keys()
    seed_sessions_from_env()
    t = threading.Thread(target=run_telegram, daemon=True)
    t.start()
    while loop is None: time.sleep(0.1)
    port = int(os.environ.get("PORT", 5000))
    print(f"[RAJFF API v3.0] Starting on port {port}")
    print(f"[RAJFF API] Bot: {BOT_USERNAME} | Dev: {DEVELOPER_TAG}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
