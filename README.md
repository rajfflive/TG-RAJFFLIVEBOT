# RAJFF Userbot API v3.0

Bot: **@RAJFFLIVEBOT** | Dev: **@RAJFFLIVE** | Channel: **t.me/RAJFFLIVE**

---

## Kya Badla? (v2 → v3)

### ✅ HTML Direct Download (FAST PATH)
- Pehle: Bot se text messages wait karta tha, page-by-page ▶ click karta tha → slow & unreliable
- Ab: Bot jo **HTML file** bhejta hai use directly download karke parse karta hai → instant response

### ✅ Bot Tags Har Response Mein
Har API response ke end mein ye aata hai:
```json
{
  "bot": "@RAJFFLIVEBOT",
  "developer": "👤 @RAJFFLIVE | 📢 t.me/RAJFFLIVE",
  "channel": "t.me/RAJFFLIVE"
}
```

### ✅ Clean JSON — Emoji Hata Diya Field Names Se
- Pehle: `"📞Telephone"`, `"🏘️Adres"`, `"👤Full name"`
- Ab: `"Telephone"`, `"Adres"`, `"Full name"`

### ✅ Source Description Alag Alag
Har source ka apna description hota hai (HiTeckGroop, 1Win, RailYatri etc.)

---

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
export API_ID=...
export API_HASH=...
export API_KEY=...
export ADMIN_KEY=...
export STRING_SESSION=...        # Account 1
export STRING_SESSION_2=...      # Account 2 (optional)
export LEAK_BOT=@OSINTINFOSBot   # Ya jo bhi leak bot hai
export BOT_USERNAME=@RAJFFLIVEBOT
export DEVELOPER_TAG="👤 @RAJFFLIVE | 📢 t.me/RAJFFLIVE"

python main.py
```

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api?num=NUMBER&key=KEY` | GET | Truecaller lookup |
| `/ind?num=10DIGIT&key=KEY` | GET | India number |
| `/pak?num=03XXXXXXX&key=KEY` | GET | Pakistan number |
| `/leak?q=NUMBER_OR_EMAIL&key=KEY` | GET | Leak DB lookup (HTML fast) |
| `/api/tg?tg=@username&key=KEY` | GET | Telegram user info |
| `/admin` | GET | Admin panel |

---

## `/leak` Response Format

```json
{
  "status": true,
  "query": "7985470106",
  "data": {
    "mode": "html",
    "total_records": 3,
    "sources_count": 3,
    "sources": [
      {
        "source": "HiTeckGroop.in",
        "description": "At the beginning of 2025, a huge leak...",
        "records": [
          {
            "Telephone": ["919335474660", "916388551056", "..."],
            "Full name": "Ram Ashray Prajapati",
            "The name of the father": "Brijesh Kumar Prajapati",
            "Adres": "S/O Ram Ashray Prajapati,551 Kha...",
            "Document number": "621582818238",
            "Region": "UPEAST;JIO UPE"
          }
        ]
      },
      {
        "source": "1Win",
        "description": "In November 2024, a huge leakage...",
        "records": [...]
      }
    ]
  },
  "response_time": "2.1s",
  "bot": "@RAJFFLIVEBOT",
  "developer": "👤 @RAJFFLIVE | 📢 t.me/RAJFFLIVE",
  "channel": "t.me/RAJFFLIVE"
}
```

---

## How It Works (HTML Mode)

1. `/leak?q=NUMBER` request aati hai
2. Leak bot ko message bheja jata hai
3. Bot HTML file document ke roop mein bhejta hai
4. `on_leak_document` handler fire hota hai
5. HTML file **directly download** hoti hai (bytes)
6. `parse_html_leak()` BeautifulSoup se HTML parse karta hai:
   - Har `<div class='block'>` = ek source
   - `<div class='block-title'>` = source naam
   - `<div class='block-text'>` = description + records
   - `<b>Field: </b> value` pattern se data extract hota hai
7. Clean JSON return hota hai with bot tags

**Fallback:** Agar bot HTML nahi bhejta aur text bhejta hai, to purana text parser bhi kaam karta hai.

---

## Notes
- `beautifulsoup4` dependency add ki hai (HTML parsing ke liye)
- Timeout 120s se 60s kar diya (HTML fast hota hai)
- Admin panel mein leak bot configure kar sakte ho: Bot Settings > Leak Bot Username
