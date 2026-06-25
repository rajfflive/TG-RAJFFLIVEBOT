"""
RAJFF Info Bot — @RAJFFLIVEBOT
- User sends phone number or email
- Bot queries the API and returns paginated results
- Download button generates HTML file
- Custom no-data message
- Admin: /setbotusername command
"""

import os, asyncio, logging, re, json, time, tempfile, requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

# ==================== CONFIG ====================
BOT_TOKEN    = os.environ["BOT_TOKEN"]
API_URL      = os.environ.get("API_URL", "http://localhost:5000")
API_KEY      = os.environ["API_KEY"]
ADMIN_KEY    = os.environ["ADMIN_KEY"]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "@RAJFFLIVEBOT")
ADMIN_IDS    = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# In-memory: user_id -> {"records": [...], "query": str, "page": int}
user_sessions: dict = {}

# ==================== HELPERS ====================

def get_bot_username():
    """Fetch from API config (live) or fallback to env."""
    try:
        r = requests.get(f"{API_URL}/admin/stats", params={"key": ADMIN_KEY}, timeout=5)
        if r.status_code == 200:
            return r.json().get("bot_username", BOT_USERNAME)
    except Exception:
        pass
    return BOT_USERNAME

def query_api(number: str) -> dict:
    """Call our API and return JSON result."""
    try:
        r = requests.get(
            f"{API_URL}/api",
            params={"key": API_KEY, "num": number},
            timeout=95
        )
        return r.json()
    except Exception as e:
        return {"success": False, "error": str(e)}

def format_record(rec: dict, source_label: str = "", index: int = 1, total: int = 1) -> str:
    """Format a single record into clean text."""
    lines = []

    if source_label:
        lines.append(f"📂 *{source_label}*\n")

    field_map = {
        "number":      ("📞", "Number"),
        "name":        ("👤", "Name"),
        "father_name": ("👨", "Father"),
        "alt_number":  ("📱", "Alt Number"),
        "cnic":        ("🪪", "CNIC"),
        "address":     ("📍", "Address"),
        "circle":      ("📡", "Circle"),
        "id":          ("🔖", "ID"),
        "email":       ("📧", "Email"),
    }

    for field, (emoji, label) in field_map.items():
        val = rec.get(field)
        if val:
            lines.append(f"{emoji} *{label}:* `{val}`")

    if total > 1:
        lines.append(f"\n📊 *Record {index} of {total}*")

    return "\n".join(lines)

def build_html_report(query: str, data: dict, bot_un: str) -> str:
    """Build a complete HTML report for download."""
    records = data.get("records", [])
    country = data.get("country", "")
    number  = data.get("number", query)

    rows_html = ""
    for i, rec in enumerate(records, 1):
        fields = ""
        field_map = [
            ("number",      "Number"),
            ("name",        "Name"),
            ("father_name", "Father Name"),
            ("alt_number",  "Alt Number"),
            ("cnic",        "CNIC"),
            ("address",     "Address"),
            ("circle",      "Circle/Operator"),
            ("id",          "ID"),
            ("email",       "Email"),
        ]
        for key, label in field_map:
            val = rec.get(key)
            if val:
                fields += f'<tr><td class="fl">{label}</td><td class="fv">{val}</td></tr>'

        rows_html += f"""
        <div class="rec">
          <div class="rec-hd">Record {i}</div>
          <table class="ftbl">{fields}</table>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>RAJFF Result — {query}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0d0d0f;color:#e2e2f0;font-family:'Segoe UI',system-ui,sans-serif;padding:20px}}
  .wrap{{max-width:700px;margin:0 auto}}
  .header{{background:linear-gradient(135deg,#1a0a3e,#0d0d0f);border:1px solid #252535;
    border-radius:16px;padding:24px;margin-bottom:20px;text-align:center}}
  .header h1{{font-size:1.4rem;font-weight:800;color:#a78bfa;margin-bottom:6px}}
  .header .sub{{font-size:.82rem;color:#8888aa}}
  .meta-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:20px}}
  .meta-card{{background:#13131a;border:1px solid #252535;border-radius:12px;padding:14px}}
  .meta-card .lbl{{font-size:.65rem;color:#8888aa;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}}
  .meta-card .val{{font-size:1rem;font-weight:700;color:#e2e2f0}}
  .rec{{background:#13131a;border:1px solid #252535;border-radius:12px;padding:20px;margin-bottom:14px}}
  .rec-hd{{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;
    color:#7c5cfc;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid #252535}}
  .ftbl{{width:100%;border-collapse:collapse}}
  .ftbl td{{padding:8px 4px;border-bottom:1px solid #1a1a24;vertical-align:top;font-size:.85rem}}
  .ftbl tr:last-child td{{border-bottom:none}}
  .fl{{color:#8888aa;width:140px;font-weight:600;padding-right:12px}}
  .fv{{color:#e2e2f0;word-break:break-all}}
  .footer{{text-align:center;margin-top:24px;padding:16px;font-size:.75rem;color:#555577;
    border-top:1px solid #252535}}
  .footer a{{color:#7c5cfc;text-decoration:none}}
  @media(max-width:500px){{.meta-grid{{grid-template-columns:1fr}}.fl{{width:110px}}}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>🔍 RAJFF Search Result</h1>
    <div class="sub">Query: <b>{query}</b> | Powered by {bot_un}</div>
  </div>
  <div class="meta-grid">
    <div class="meta-card"><div class="lbl">Number</div><div class="val">{number}</div></div>
    <div class="meta-card"><div class="lbl">Country</div><div class="val">{country}</div></div>
    <div class="meta-card"><div class="lbl">Total Records</div><div class="val">{len(records)}</div></div>
    <div class="meta-card"><div class="lbl">Source</div><div class="val">Truecaller DB</div></div>
  </div>
  {rows_html if rows_html else '<div class="rec"><div style="text-align:center;color:#8888aa;padding:20px">No records found</div></div>'}
  <div class="footer">
    Thanks for using our service <a href="https://t.me/{bot_un.lstrip('@')}" target="_blank">{bot_un}</a>
    &nbsp;|&nbsp; <a href="https://t.me/+QUg-JvyJizkxMzA1" target="_blank">📢 Join Channel</a>
  </div>
</div>
</body>
</html>"""

CHANNEL_LINK = "https://t.me/+QUg-JvyJizkxMzA1"

DATA_FOUND_NOTE = (
    "⚠️ *Note:* Some platforms or applications have leaked or sold this database\\. "
    "Here is your information that was sold by the web owner or application owner "
    "where you logged in with your personal details\\.\n\n"
    f"📢 Join our channel: {CHANNEL_LINK}"
)

def no_data_message(bot_un: str) -> str:
    return (
        "❌ *No data found*\n\n"
        "This user data has not been cached by someone, "
        "we can't provide it or he is using a new phone number\\.\n\n"
        f"📢 Join our channel: {CHANNEL_LINK}\n\n"
        f"Thanks for using our service {bot_un}"
    )

def build_keyboard(page: int, total: int, query_id: str) -> InlineKeyboardMarkup:
    """Build ← → navigation + Download button."""
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"pg:{query_id}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total}", callback_data="noop"))
    if page < total - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"pg:{query_id}:{page+1}"))

    download_btn = InlineKeyboardButton("📥 Download", callback_data=f"dl:{query_id}")
    return InlineKeyboardMarkup([nav, [download_btn]])

# ==================== HANDLERS ====================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot_un = get_bot_username()
    await update.message.reply_text(
        f"👋 *Welcome to RAJFF Search Bot*\n\n"
        f"Send me a *phone number* or *email* and I'll search for information.\n\n"
        f"Examples:\n"
        f"• `9305121760`\n"
        f"• `+919305121760`\n"
        f"• `user@email.com`\n\n"
        f"Thanks for using our service {bot_un}",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    bot_un = get_bot_username()

    if not query_text:
        return

    # Show searching message
    msg = await update.message.reply_text(
        f"🔍 *Searching for:* `{query_text}`\n\n⏳ Please wait...",
        parse_mode=ParseMode.MARKDOWN
    )

    # Query API
    result = query_api(query_text)

    if not result.get("success") or not result.get("records"):
        await msg.edit_text(
            no_data_message(bot_un),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    records = result.get("records", [])
    total   = len(records)

    # Store session
    query_id = f"{update.effective_user.id}_{int(time.time())}"
    user_sessions[query_id] = {
        "records": records,
        "data":    result,
        "query":   query_text,
        "page":    0
    }

    # Show first record
    text = format_record(records[0], index=1, total=total)
    text += f"\n\n{DATA_FOUND_NOTE}\n\nThanks for using our service {bot_un}"

    if total > 1:
        kb = build_keyboard(0, total, query_id)
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Download", callback_data=f"dl:{query_id}")
        ]])

    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    bot_un = get_bot_username()

    if data == "noop":
        return

    # ── Page navigation ──
    if data.startswith("pg:"):
        _, query_id, page_str = data.split(":", 2)
        page = int(page_str)

        session = user_sessions.get(query_id)
        if not session:
            await query.edit_message_text("❌ Session expired. Please search again.")
            return

        records = session["records"]
        total   = len(records)
        page    = max(0, min(page, total - 1))
        session["page"] = page

        rec  = records[page]
        text = format_record(rec, index=page+1, total=total)
        text += f"\n\n{DATA_FOUND_NOTE}\n\nThanks for using our service {bot_un}"

        kb = build_keyboard(page, total, query_id)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    # ── Download HTML ──
    elif data.startswith("dl:"):
        _, query_id = data.split(":", 1)

        session = user_sessions.get(query_id)
        if not session:
            await query.edit_message_text("❌ Session expired. Please search again.")
            return

        html_content = build_html_report(
            session["query"],
            session["data"],
            bot_un
        )

        # Send as HTML file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write(html_content)
            tmp_path = f.name

        try:
            with open(tmp_path, "rb") as f:
                await query.message.reply_document(
                    document=f,
                    filename=f"rajff_{session['query'].replace('+', '')}.html",
                    caption=f"📄 Search result for `{session['query']}`\n\nThanks for using our service {bot_un}",
                    parse_mode=ParseMode.MARKDOWN
                )
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

# ==================== ADMIN COMMANDS ====================

async def cmd_setusername(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return

    if not ctx.args:
        await update.message.reply_text("Usage: /setbotusername @YOURBOTUSERNAME")
        return

    new_un = ctx.args[0].strip()
    if not new_un.startswith("@"):
        new_un = "@" + new_un

    # Update via API
    try:
        r = requests.post(
            f"{API_URL}/admin/config",
            params={"key": ADMIN_KEY},
            json={"bot_username": new_un},
            timeout=10
        )
        if r.status_code == 200 and r.json().get("success"):
            await update.message.reply_text(f"✅ Bot username updated to *{new_un}*", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"❌ Failed: {r.text}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return

    try:
        r = requests.get(f"{API_URL}/admin/stats", params={"key": ADMIN_KEY}, timeout=10)
        d = r.json()
        bot_un = d.get("bot_username", "N/A")
        stats  = d.get("stats", {})
        text = (
            f"📊 *API Stats*\n\n"
            f"🔑 Active Keys: `{d.get('total_keys', 0)}`\n"
            f"👤 Accounts: `{d.get('total_accounts', 0)}`\n"
            f"⏳ Pending: `{d.get('pending', 0)}`\n"
            f"📈 Total Lookups: `{stats.get('total', 0)}`\n"
            f"✅ Success: `{stats.get('success', 0)}`\n"
            f"❌ Failed: `{stats.get('failed', 0)}`\n"
            f"🤖 Bot Username: `{bot_un}`"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ==================== MAIN ====================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setbotusername", cmd_setusername))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_query))

    print(f"[BOT] Starting RAJFF Bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
