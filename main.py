import os, uuid, json, traceback, requests, re, time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="GTM Agency Lead Magnet")
app.mount("/files", StaticFiles(directory=OUTPUT_DIR), name="files")

env = Environment(loader=FileSystemLoader(BASE_DIR))

def _split_sentences(text: str) -> list[str]:
    """Split a paragraph into individual sentences for spaced rendering."""
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z\"\'\(])', text.strip())
    return [p.strip() for p in parts if p.strip()]

def _bold_offer(sentence: str) -> str:
    """Bold 'we build AI-assisted X' and the 45-day guarantee in the intro."""
    # Bold "we build AI-assisted X" up to the next clause boundary
    sentence = re.sub(
        r'(we build AI-assisted [\w\s]+?)(\s+for\b|\s+to\b|\s+at\b|,)',
        r'<strong>\1</strong>\2',
        sentence, flags=re.IGNORECASE
    )
    # Bold the 45-day guarantee sentence
    sentence = re.sub(
        r'(If by day 45[^.]*\.)',
        r'<strong>\1</strong>',
        sentence, flags=re.IGNORECASE
    )
    return sentence


# ── COMPANY NAME + INTRO NORMALISATION ────────────────────────────────────────
_DESCRIPTOR = ("Technologies|Technology|Solutions|Systems|Labs|Corporation|Analytics|"
               "Global|Digital|Group|Consulting|International|Ventures|Studios|Studio|"
               "Works|Media|Networks|Network|Platform|Platforms|Holdings")
_DESCRIPTOR_RE = re.compile(rf"\s+({_DESCRIPTOR})\s*$", re.IGNORECASE)
_ALL_CAPS_RE = re.compile(r"^[A-Z0-9\s&.'\-]+$")
_ALL_LOWER_RE = re.compile(r"^[a-z0-9\s&.'\-]+$")


def clean_company_name(raw: str) -> str:
    """Normalise a company name for conversational use: strip legal suffixes,
    taglines, trailing ' AI'/'.io' etc., and fix casing. 'Volt, Inc.' -> 'Volt'."""
    s = (raw or "").strip()
    s = re.sub(r"\s+is now\s+.+$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*[:|/]\s*.+$", "", s)                    # drop taglines after : | /
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)                  # drop trailing (...)
    # strip legal suffixes, with or without a leading comma, possibly repeated
    for _ in range(2):
        s = re.sub(r",?\s+(Inc\.?|LLC\.?|L\.L\.C\.?|Corp(oration)?\.?|Ltd\.?|Co\.?|"
                   r"LP|LLP|PLC|GmbH|S\.?A\.?|Pty\.?|Limited|Incorporated)\s*$",
                   "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\.(com|io|net|co|ai|app)\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+AI\s*$", "", s)                         # trailing " AI"
    s = _DESCRIPTOR_RE.sub("", s).strip()
    s = s.rstrip(" ,.-").strip()
    if s and _ALL_CAPS_RE.match(s) and len(s) > 4:
        s = s.title()
    elif s and _ALL_LOWER_RE.match(s):
        s = s[:1].upper() + s[1:]
    return s


GUARANTEE_LINE = ("If by day 45 you aren't seeing qualified demos booked in your "
                  "calendar, we work completely for free until you do.")

# Any sentence the AI writes about the offer/guarantee/timeline/pricing gets
# force-replaced with GUARANTEE_LINE. This must NOT depend on one exact phrase
# (Clay's AI phrases it differently every lead - "90-day sprint", "running in
# 90 days", "money back", "no questions asked", etc.). Matching ANY of these
# markers is what makes the replacement bulletproof.
_OFFER_MARKERS = re.compile(
    r'\b(retainer|money[\s-]?back|moneyback|refund(?:ed|s)?|guarantee[ds]?|'
    r'\d+[\s-]?day sprint|sprint|\d+[\s-]?days?|no questions asked|'
    r'for free|free until|get your money|money[\s-]?back guarantee|'
    r'running in\s+\d+|up and running|no small print)\b',
    re.IGNORECASE,
)
_CLOSING_RE = re.compile(r'\b(with that in mind|here are (the |)?(three|3|a few|some)|'
                         r'here(?:\'s| is) (how|what|three|3))\b', re.IGNORECASE)
# If an offer sentence also greets/identifies (offer is inline with the intro),
# keep the greeting - only cut the offer clause off the end, never drop it whole.
_IDENTITY_RE = re.compile(r"(i'?m leo|gtm agency|go-to-market|we build ai|"
                          r"backed by|^\s*(hi|hey|hello)\b)", re.IGNORECASE)


def _strip_offer_clause(sentence: str) -> str:
    """Cut a trailing offer/guarantee clause off a sentence, keeping the head."""
    m = _OFFER_MARKERS.search(sentence)
    if not m:
        return sentence.strip()
    head = sentence[:m.start()]
    head = re.sub(r'[\s,;-]*(installed in a|and have it running in|and have it|'
                  r'we get in[^,]*|in a|with a|and)?\s*$', '', head, flags=re.IGNORECASE)
    return head.rstrip(' ,;-')


def normalise_intro(text: str) -> str:
    """Force the intro to carry EXACTLY our locked 45-day guarantee, no matter
    how Clay's AI phrased the offer. Any sentence with offer/guarantee/timeline/
    pricing language is replaced in place by GUARANTEE_LINE; extra such
    sentences are dropped. A greeting/identity sentence is never dropped whole -
    only its offer clause is cut. If the AI wrote no offer sentence at all, the
    guarantee is inserted just before the 'here are three plays' closing line."""
    sentences = _split_sentences((text or "").strip())
    out, inserted = [], False
    for s in sentences:
        if _OFFER_MARKERS.search(s):
            if _IDENTITY_RE.search(s):
                head = _strip_offer_clause(s)
                if head:
                    out.append(head if head.endswith((".", "!", "?")) else head + ".")
            if not inserted:
                out.append(GUARANTEE_LINE)
                inserted = True
            # any further offer-only sentences are dropped
        else:
            out.append(s)
    if not inserted:
        pos = next((i for i, s in enumerate(out) if _CLOSING_RE.search(s)), len(out))
        out.insert(pos, GUARANTEE_LINE)
    return " ".join(out)


env.filters["sentences"]   = _split_sentences
env.filters["bold_offer"]  = _bold_offer
template = env.get_template("template.html")
template_lookalike = env.get_template("template_lookalike.html")

# ── ENV VARS ──────────────────────────────────────────────────────────────────
CALENDLY_LINK        = os.environ.get("CALENDLY_LINK",        "https://calendly.com/thegtmagency/30min")
CALENDLY_API_TOKEN   = os.environ.get("CALENDLY_API_TOKEN",   "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
DRIVE_FOLDER_ID      = os.environ.get("DRIVE_FOLDER_ID",      "")
SLACK_WEBHOOK_URL    = os.environ.get("SLACK_WEBHOOK_URL",    "")
PUBLIC_URL           = os.environ.get("PUBLIC_URL",           "").rstrip("/")
LEO_LINKEDIN_URL     = os.environ.get("LEO_LINKEDIN_URL",     "https://www.linkedin.com/in/leo-bosuener1/")
INSTANTLY_API_KEY    = os.environ.get("INSTANTLY_API_KEY",    "").strip()
INSTANTLY_BASE_URL   = "https://api.instantly.ai/api/v2"
# Smartlead — the campaigns moved off Instantly, so the microsite auto-reply
# threads through here. The webhook payload carries only name/email/company/
# website, so campaign_id and email_stats_id have to be looked back up.
SMARTLEAD_API_KEY    = os.environ.get("SMARTLEAD_API_KEY",    "").strip()
SMARTLEAD_BASE_URL   = "https://server.smartlead.ai/api/v1"
# Cloudflare 1010s urllib/python default agents on this host.
SMARTLEAD_HEADERS    = {"User-Agent": "curl/8.4.0"}
# Ocean.io lookalike search — powers the lookalike lead magnet (/generate-lookalike).
# Reuses the same key/filters as the standalone lookalike agent.
OCEAN_API_KEY        = os.environ.get("OCEAN_API_KEY",        "").strip()
OCEAN_BASE_URL       = "https://api.ocean.io"
OCEAN_COUNTRIES      = ["US", "GB", "CA", "FR", "DE", "ES", "NL", "BE", "AU"]
OCEAN_COMPANY_SIZES  = ["11-50", "51-200", "201-500", "501-1000"]
OCEAN_FIELDS         = ["name", "domain", "companySize", "industries",
                        "industryCategories", "linkedinIndustry",
                        "employeeCountOcean", "locations.primary",
                        "locations.locality", "locations.region",
                        "locations.country"]
AUTOSEND_DRY_RUN     = os.environ.get("AUTOSEND_DRY_RUN", "false").strip().lower() == "true"
# Close CRM: write the generated PDF link onto the lead's record so it lives
# on the one centralised Close lead alongside the reply/phone/brief/debrief.
CLOSE_API_KEY        = os.environ.get("CLOSE_API_KEY",        "").strip()
CLOSE_BASE_URL       = "https://api.close.com/api/v1"
# Custom field "Lead Magnet PDF" on Close leads (created via API 2026-07-15).
CLOSE_PDF_FIELD_ID   = "cf_q5HFmIkiYRfdAuvckomB6PleRrtySLyUZCEYxJAhdD5"
# "Lead Magnet Follow Up" custom Lead Label in Instantly — its interest_status
# value, confirmed live via GET /api/v2/lead-labels. Setting a lead's interest
# status to this value is what puts it into that label / triggers the
# 1-day-after-no-response follow-up subsequence.
LEAD_MAGNET_FOLLOWUP_INTEREST_VALUE = 52

# ── STATIC PROOF STATS ────────────────────────────────────────────────────────
PROOF_STATS = [
    {"value": "$1.53M", "label": "Revenue generated for AirOps"},
    {"value": "100/mo", "label": "Meetings booked for Peoplelogic"},
    {"value": "500+",   "label": "SaaS companies scaled"},
]

# ── VC LOGO DOWNLOAD AT STARTUP ───────────────────────────────────────────────
VC_LOGO_DIR = os.path.join(BASE_DIR, "assets", "vc")
os.makedirs(VC_LOGO_DIR, exist_ok=True)

VC_LOGOS = {
    "sequoia":    "https://logo.clearbit.com/sequoiacap.com",
    "a16z":       "https://logo.clearbit.com/a16z.com",
    "yc":         "https://logo.clearbit.com/ycombinator.com",
    "techstars":  "https://logo.clearbit.com/techstars.com",
    "lightspeed": "https://logo.clearbit.com/lsvp.com",
    "wing":       "https://logo.clearbit.com/wing.vc",
    "boldstart":  "https://logo.clearbit.com/boldstart.vc",
}

def download_vc_logos():
    for name, url in VC_LOGOS.items():
        dest = os.path.join(VC_LOGO_DIR, f"{name}.png")
        if os.path.exists(dest) and os.path.getsize(dest) > 100:
            continue
        try:
            r = requests.get(f"{url}?size=40", timeout=8)
            if r.status_code == 200 and len(r.content) > 100:
                with open(dest, "wb") as f:
                    f.write(r.content)
                print(f"Downloaded VC logo: {name}")
        except Exception as e:
            print(f"Could not download {name} logo: {e}")

download_vc_logos()

# ── DATA MODELS ───────────────────────────────────────────────────────────────
class Strategy(BaseModel):
    strategyName:      str
    goal:              str
    whyThisFitsYou:    str
    triggerDefinition: str
    technologyUsed:    str
    targetPersona:     str
    channel:           str
    execution:         list[str]

class PayloadIn(BaseModel):
    first_name:       str
    company_name:     str
    company_logo_url: str
    intro_text:       str
    buyer_personas:   list[str]
    strategies:       list[Strategy]
    email:            str = ""
    campaign_id:      str = ""


class LookalikePayloadIn(BaseModel):
    first_name:         str
    company_name:       str
    seed_domain:        str            # the prospect's own best customer's domain (the Ocean seed)
    seed_customer_name: str            # display name for that customer, e.g. "Ramp"
    company_logo_url:   str = ""
    email:              str = ""
    campaign_id:        str = ""
    country:            str = ""       # seed customer's 2-letter country (from Clay); locks lookalikes to that market
    size:               int = 60       # how many lookalikes to request from Ocean


# ── OCEAN.IO LOOKALIKE ─────────────────────────────────────────────────────────
def call_ocean_lookalike(domain: str, size: int, countries: list[str] | None = None) -> list[dict]:
    """Ocean.io V3 company lookalike search. Returns the raw companies list
    (each item: {"company": {...}, "relevance": ...}), ranked closest-first.

    `countries` restricts results by primary location. Pass the seed customer's
    own country (from Clay) so the list is a realistic target set in the market
    the prospect actually sells into - Ocean's similarity ignores geography, so
    without this a US company's lookalikes come back spread across the UK, AU,
    etc. Falls back to the default multi-country list when not provided."""
    if not OCEAN_API_KEY:
        raise RuntimeError("OCEAN_API_KEY not set")
    include = [c.strip().upper() for c in (countries or []) if c and c.strip()] or OCEAN_COUNTRIES
    payload = {
        "size": size,
        "companiesFilters": {
            "lookalikeDomains": [domain],
            "companySizes": OCEAN_COMPANY_SIZES,
            "primaryLocations": {"includeCountries": include},
        },
        "fields": OCEAN_FIELDS,
    }
    resp = requests.post(
        f"{OCEAN_BASE_URL}/v3/search/companies",
        json=payload,
        headers={"x-api-token": OCEAN_API_KEY, "Content-Type": "application/json"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json().get("companies", []) or []


def _map_company(item: dict) -> dict:
    """Flatten one Ocean result into the fields the PDF row needs."""
    c = item.get("company", {}) or {}
    locs = c.get("locations", []) or []
    p = next((l for l in locs if l.get("primary")), locs[0] if locs else {})
    city = (p.get("locality", "") or "").strip()
    # Ocean's region can be pipe-joined ("England|Southern|Oxford (OX)") - take
    # the first segment. Country comes as a lowercase code - uppercase it.
    region = (p.get("region", "") or "").split("|")[0].strip()
    country = (p.get("country", "") or "").strip()
    if 0 < len(country) <= 3:
        country = country.upper()
    tail = region or country
    location = ", ".join([x for x in [city, tail] if x]) or country or "-"
    ind_list = c.get("industries") or c.get("industryCategories") or [c.get("linkedinIndustry", "")]
    industry = next((x for x in ind_list if x), "-")
    emp = c.get("employeeCountOcean")
    size = f"{emp} staff" if emp else (c.get("companySize", "") or "-")
    return {
        "name": (c.get("name", "") or c.get("domain", "")).strip(),
        "domain": c.get("domain", ""),
        "industry": industry,
        "size": size,
        "location": location,
    }


def tier_companies(companies: list[dict]) -> tuple[list[dict], int]:
    """Map, dedupe by domain, and split into 3 relevance-ranked tiers
    (~30% / ~35% / rest). Adds a global row number. Returns (tiers, total)."""
    mapped = [_map_company(x) for x in companies
              if (x.get("company") or {}).get("name") or (x.get("company") or {}).get("domain")]
    seen, uniq = set(), []
    for m in mapped:
        k = (m["domain"] or m["name"]).lower()
        if not k or k in seen:
            continue
        seen.add(k); uniq.append(m)
    n = len(uniq)
    c1 = max(1, round(n * 0.30)) if n else 0
    c2 = max(1, round(n * 0.35)) if n else 0
    t1, t2, t3 = uniq[:c1], uniq[c1:c1 + c2], uniq[c1 + c2:]
    tiers = [
        {"n": 1, "name": "Tier 1 · Closest match", "sub": "Start here - nearest fit on size, industry and market", "companies": t1},
        {"n": 2, "name": "Tier 2 · Strong match",  "sub": "Very close, worth a first-touch",                     "companies": t2},
        {"n": 3, "name": "Tier 3 · Worth a look",  "sub": "Adjacent fit, good for a second wave",                "companies": t3},
    ]
    i = 1
    for t in tiers:
        for co in t["companies"]:
            co["n"] = i; i += 1
    return tiers, n

# ── GOOGLE DRIVE ──────────────────────────────────────────────────────────────
def upload_to_drive(file_path: str, company_name: str) -> str | None:
    if not GOOGLE_CREDENTIALS_JSON or not DRIVE_FOLDER_ID:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDENTIALS_JSON),
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        service = build("drive", "v3", credentials=creds)
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        f = service.files().create(
            body={"name": f"Outbound Playbook — {company_name} ({ts}).pdf", "parents": [DRIVE_FOLDER_ID]},
            media_body=MediaFileUpload(file_path, mimetype="application/pdf"),
            fields="id",
            supportsAllDrives=True,
        ).execute()
        service.permissions().create(
            fileId=f["id"],
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()
        return f"https://drive.google.com/file/d/{f['id']}/view"
    except Exception as e:
        print(f"Drive upload failed: {e}")
        return None

# ── CALENDLY ──────────────────────────────────────────────────────────────────
# Never pitch a time before this hour, Eastern. Leo's Calendly availability is
# set in his own timezone, and he is currently 6+ hours ahead of ET, so his
# ordinary morning converts to the middle of the night for a US prospect.
# Without this floor the reply cheerfully suggests a 4am call.
CALENDLY_MIN_HOUR_ET = int(os.environ.get("CALENDLY_MIN_HOUR_ET", "9"))


def get_calendly_slots(min_hour: int | None = None, want: int = 2) -> list[str]:
    if not CALENDLY_API_TOKEN:
        return ["CALENDLY_ERROR: no token set"]
    min_hour = CALENDLY_MIN_HOUR_ET if min_hour is None else min_hour
    headers = {"Authorization": f"Bearer {CALENDLY_API_TOKEN}", "Content-Type": "application/json"}
    try:
        me = requests.get("https://api.calendly.com/users/me", headers=headers, timeout=10)
        if me.status_code != 200:
            return [f"CALENDLY_ERROR: /users/me returned {me.status_code} — {me.text[:120]}"]
        user_uri = me.json()["resource"]["uri"]

        et = requests.get("https://api.calendly.com/event_types", headers=headers,
                          params={"user": user_uri, "active": "true"}, timeout=10)
        if et.status_code != 200:
            return [f"CALENDLY_ERROR: /event_types returned {et.status_code} — {et.text[:120]}"]
        types = et.json().get("collection", [])
        if not types:
            return ["CALENDLY_ERROR: no active event types found"]

        now = datetime.now(timezone.utc)
        start = now + timedelta(minutes=15)   # must be in the future
        # 7 days is Calendly's maximum range per request. The old 72h window
        # plus the 9am floor could easily return nothing at all.
        end   = now + timedelta(days=7)
        avail = requests.get(
            "https://api.calendly.com/event_type_available_times",
            headers=headers,
            params={
                "event_type": types[0]["uri"],
                "start_time": start.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
                "end_time":   end.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
            },
            timeout=10,
        )
        if avail.status_code != 200:
            return [f"CALENDLY_ERROR: /available_times returned {avail.status_code} — {avail.text[:200]}"]

        slots = avail.json().get("collection", [])
        if not slots:
            return ["CALENDLY_ERROR: no slots in next 72h"]

        eastern = ZoneInfo("America/New_York")
        out = []
        for slot in slots:
            dt = datetime.fromisoformat(slot["start_time"].replace("Z", "+00:00")).astimezone(eastern)
            if dt.hour < min_hour:
                continue
            out.append(f"{dt.strftime('%A')} at {dt.strftime('%-I:%M%p').lower()} {dt.strftime('%Z')}")
            if len(out) >= want:
                break
        if not out:
            return [f"CALENDLY_ERROR: no slots at or after {min_hour}:00 ET in the next 7 days"]
        return out
    except Exception as e:
        return [f"CALENDLY_ERROR: exception — {str(e)[:150]}"]

# ── REPLY TEXT ────────────────────────────────────────────────────────────────
def reply_text_to_html(plain_text: str) -> str:
    """Convert plain text with \\n\\n paragraph breaks (and single \\n line
    breaks within a paragraph, e.g. a sign-off) into real HTML. Instantly's
    own HTML auto-generation just wraps the whole string in one <body> tag
    with no line breaks at all, collapsing every paragraph into a single
    block — this is what actually controls spacing in the sent email."""
    paragraphs = [p.strip() for p in plain_text.split("\n\n") if p.strip()]
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)


def build_reply_text(first_name: str, company_name: str, drive_url: str) -> str:
    slots   = get_calendly_slots()
    is_err  = any(s.startswith("CALENDLY_ERROR") for s in slots)
    if not is_err and len(slots) >= 2:
        time_pitch = f"Does {slots[0]} or {slots[1]} work to have a chat?"
    elif not is_err and len(slots) == 1:
        time_pitch = f"Does {slots[0]} work to have a chat?"
    else:
        time_pitch = f"Would love to find a time to chat. [DEBUG: {' | '.join(slots)}]"

    return (
        f"Hey {first_name}, great to hear back from you.\n\n"
        f"Here's the map I put together for {company_name}: {drive_url}\n\n"
        f"Would love to walk you through it and see where we could get your GTM moving.\n\n"
        f"{time_pitch}\n\n"
        f"If neither works, grab any time here: {CALENDLY_LINK}\n\n"
        f"Look forward to speaking soon.\nBest, Leo"
    )


def build_reply_lookalike_text(first_name: str, seed_customer_name: str,
                               total_count: int, drive_url: str) -> str:
    slots  = get_calendly_slots()
    is_err = any(s.startswith("CALENDLY_ERROR") for s in slots)
    if not is_err and len(slots) >= 2:
        time_pitch = f"Does {slots[0]} or {slots[1]} work to have a chat?"
    elif not is_err and len(slots) == 1:
        time_pitch = f"Does {slots[0]} work to have a chat?"
    else:
        time_pitch = f"Would love to find a time to chat. [DEBUG: {' | '.join(slots)}]"

    return (
        f"Hey {first_name}, great to hear back from you.\n\n"
        f"Here's the list I put together - {total_count} companies that look just like "
        f"{seed_customer_name}, ranked closest-match first: {drive_url}\n\n"
        f"Tier 1 is where I'd start. Happy to walk you through how I'd turn it into "
        f"booked demos.\n\n"
        f"{time_pitch}\n\n"
        f"If neither works, grab any time here: {CALENDLY_LINK}\n\n"
        f"Look forward to speaking soon.\nBest, Leo"
    )


def is_safe_to_autosend(reply_text: str) -> bool:
    """Refuse to auto-send if the reply carries a debug/error artifact or an
    unfilled placeholder that should never reach a real prospect."""
    red_flags = ["CALENDLY_ERROR", "[DEBUG", "[PASTE LOOM LINK]",
                 "[TIME 1]", "[TIME 2]"]
    return not any(flag in reply_text for flag in red_flags)


# ── SMARTLEAD (find thread + reply in thread) ────────────────────────────────
def sl_find_thread(lead_email: str) -> dict | None:
    """Resolve an email address to the thread we can reply on.

    The microsite webhook only carries the lead's email, so campaign_id and
    email_stats_id are recovered here: lead lookup -> its campaign -> the
    message history. READ-ONLY, no side effects. Returns None if the lead has
    not actually replied, because there is then no thread to reply into.
    """
    if not (SMARTLEAD_API_KEY and lead_email):
        return None

    def get(path, **params):
        r = requests.get(f"{SMARTLEAD_BASE_URL}/{path}",
                         params={"api_key": SMARTLEAD_API_KEY, **params},
                         timeout=30, headers=SMARTLEAD_HEADERS)
        return r.json() if r.status_code == 200 else None

    try:
        lead = get("leads/", email=lead_email)
        lead_id = (lead or {}).get("id")
        if not lead_id:
            print(f"smartlead: no lead for {lead_email}", flush=True)
            return None
        camps = get(f"leads/{lead_id}/campaigns") or []
        if not camps:
            print(f"smartlead: no campaign for {lead_email}", flush=True)
            return None
        # A lead can sit in more than one campaign. Take the one that actually
        # holds a reply, so we never thread onto a campaign they never answered.
        for c in camps:
            cid = c.get("id")
            hist = (get(f"campaigns/{cid}/leads/{lead_id}/message-history")
                    or {}).get("history") or []
            replies = [h for h in hist if (h.get("type") or "").upper() == "REPLY"]
            if replies and replies[-1].get("stats_id"):
                return {"campaign_id": cid, "lead_id": lead_id,
                        "stats_id": replies[-1]["stats_id"],
                        "campaign_name": c.get("name") or ""}
        print(f"smartlead: no REPLY in any thread for {lead_email}", flush=True)
        return None
    except Exception as e:
        print(f"smartlead thread lookup error: {e}", flush=True)
        return None


def sl_reply_in_thread(campaign_id, stats_id: str, body_html: str) -> bool:
    """Send the reply on the existing thread, from the mailbox that got it."""
    if not (SMARTLEAD_API_KEY and campaign_id and stats_id):
        return False
    try:
        r = requests.post(
            f"{SMARTLEAD_BASE_URL}/campaigns/{campaign_id}/reply-email-thread",
            params={"api_key": SMARTLEAD_API_KEY},
            json={"email_stats_id": stats_id, "email_body": body_html},
            timeout=30, headers=SMARTLEAD_HEADERS)
        if r.status_code >= 400:
            print(f"smartlead reply failed ({r.status_code}): {r.text[:300]}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"smartlead reply error: {e}", flush=True)
        return False


# ── INSTANTLY (find thread + send reply) ─────────────────────────────────────
def find_latest_inbound_email(lead_email: str, campaign_id: str = "") -> dict | None:
    """Find the most recent email in this lead's thread — this is what we
    reply to. READ-ONLY lookup, no side effects."""
    if not INSTANTLY_API_KEY or not lead_email:
        return None
    try:
        params = {
            "lead": lead_email,
            "latest_of_thread": "true",
            "sort_order": "desc",
            "limit": 1,
        }
        if campaign_id:
            params["campaign_id"] = campaign_id
        r = requests.get(
            f"{INSTANTLY_BASE_URL}/emails",
            headers={"Authorization": f"Bearer {INSTANTLY_API_KEY}"},
            params=params,
            timeout=15,
        )
        if r.status_code != 200:
            print(f"Instantly email lookup failed ({r.status_code}): {r.text[:200]}")
            return None
        items = r.json().get("items", [])
        return items[0] if items else None
    except Exception as e:
        print(f"Instantly email lookup error: {e}")
        return None


def send_reply(source_email: dict, reply_text: str) -> bool:
    """Actually send the reply, threaded into the existing conversation."""
    if not INSTANTLY_API_KEY:
        return False
    subject = source_email.get("subject") or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    payload = {
        "eaccount": source_email.get("eaccount", ""),
        "reply_to_uuid": source_email.get("id", ""),
        "subject": subject,
        "body": {"text": reply_text, "html": reply_text_to_html(reply_text)},
    }
    try:
        r = requests.post(
            f"{INSTANTLY_BASE_URL}/emails/reply",
            headers={"Authorization": f"Bearer {INSTANTLY_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if r.status_code not in (200, 201):
            print(f"Instantly reply send failed ({r.status_code}): {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"Instantly reply send error: {e}")
        return False


def tag_lead_magnet_followup(lead_email: str, campaign_id: str = "") -> bool:
    """Move the lead from its current interest status into the 'Lead Magnet
    Follow Up' label, once the auto-reply has actually gone out. This is what
    lets the separate follow-up subsequence trigger 1 day after no response."""
    if not INSTANTLY_API_KEY or not lead_email:
        return False
    payload = {
        "lead_email": lead_email,
        "interest_value": LEAD_MAGNET_FOLLOWUP_INTEREST_VALUE,
    }
    if campaign_id:
        payload["campaign_id"] = campaign_id
    try:
        r = requests.post(
            f"{INSTANTLY_BASE_URL}/leads/update-interest-status",
            headers={"Authorization": f"Bearer {INSTANTLY_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        # This endpoint is async and returns 202 Accepted ("background job
        # submitted"), not 200/201 - treat any 2xx as success. Rejecting 202
        # made a genuinely-applied tag report as FAILED in Slack.
        if not (200 <= r.status_code < 300):
            print(f"Instantly tag-followup failed ({r.status_code}): {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"Instantly tag-followup error: {e}")
        return False


# ── SLACK ─────────────────────────────────────────────────────────────────────
def push_pdf_to_close(lead_email: str, drive_url: str) -> str:
    """Write the lead-magnet PDF link onto the lead's Close record.

    Finds the lead by email (Clay's Create Lead in the positive-response table
    normally creates it first, and that quick API call finishes well before
    this PDF render does, so the lead is almost always already there). Retries
    the lookup a couple of times as insurance against the race, then PATCHes
    the 'Lead Magnet PDF' custom field. Never raises - this is best-effort and
    must not affect PDF generation or the auto-reply.
    """
    if not CLOSE_API_KEY or not lead_email or not drive_url:
        return "disabled"
    auth = (CLOSE_API_KEY, "")
    lead_id = None
    for attempt in range(3):
        try:
            r = requests.get(
                f"{CLOSE_BASE_URL}/lead/",
                auth=auth,
                params={"query": f"email_address:{lead_email}", "_limit": 1},
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json().get("data") or []
                if data:
                    lead_id = data[0].get("id")
                    break
        except Exception as e:
            print(f"Close lead lookup error: {e}")
        time.sleep(4)
    if not lead_id:
        print(f"Close: no lead found for {lead_email} after retries, PDF link not written")
        return "no_lead_found"
    try:
        r = requests.put(
            f"{CLOSE_BASE_URL}/lead/{lead_id}/",
            auth=auth,
            headers={"Content-Type": "application/json"},
            json={f"custom.{CLOSE_PDF_FIELD_ID}": drive_url},
            timeout=15,
        )
        if r.status_code not in (200, 201):
            print(f"Close PDF write failed ({r.status_code}): {r.text[:300]}")
            return "write_failed"
        return "written"
    except Exception as e:
        print(f"Close PDF write error: {e}")
        return "write_failed"


def close_lead_link(lead_email: str, first_name: str = "", company_name: str = "") -> str:
    """Return an app.close.com lead URL for this prospect, creating the lead
    if Close doesn't have one yet. The link goes on the Slack card so the SDR
    opens Close and dials through the CRM's dedicated number instead of a
    personal mobile. Best-effort: returns "" on any failure, never raises."""
    if not CLOSE_API_KEY or not lead_email:
        return ""
    auth = (CLOSE_API_KEY, "")
    try:
        for attempt in range(3):
            r = requests.get(
                f"{CLOSE_BASE_URL}/lead/",
                auth=auth,
                params={"query": f"email_address:{lead_email}", "_limit": 1},
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json().get("data") or []
                if data:
                    return f"https://app.close.com/lead/{data[0]['id']}/"
                break
            time.sleep(3)
        payload = {
            "name": company_name or lead_email.split("@")[-1],
            "contacts": [{
                "name": first_name or lead_email.split("@")[0],
                "emails": [{"email": lead_email, "type": "office"}],
            }],
        }
        r = requests.post(f"{CLOSE_BASE_URL}/lead/", auth=auth,
                          headers={"Content-Type": "application/json"},
                          json=payload, timeout=15)
        if r.status_code in (200, 201):
            return f"https://app.close.com/lead/{r.json()['id']}/"
        print(f"Close lead create failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"close_lead_link error: {e}")
    return ""


def post_to_slack(first_name: str, company_name: str, drive_url: str, reply_text: str, autosend_status: str):
    if not SLACK_WEBHOOK_URL:
        return

    status_labels = {
        "sent":             ":white_check_mark: *Auto-sent to the lead and tagged 'Lead Magnet Follow Up'*",
        "sent_tag_failed":  ":warning: *Auto-sent to the lead, but tagging as 'Lead Magnet Follow Up' FAILED — needs manual status change, see Railway logs*",
        "dry_run":          ":test_tube: *DRY RUN — matched a real thread and would have sent this, but AUTOSEND_DRY_RUN is on. Nothing was sent.*",
        "unsafe":           ":warning: *NOT auto-sent — reply contained a debug/error artifact, needs manual review*",
        "no_email":         ":warning: *NOT auto-sent — no lead email on this payload yet*",
        "no_thread_found":  ":warning: *NOT auto-sent — couldn't find a matching email thread in Instantly*",
        "send_failed":      ":x: *Auto-send failed — see Railway logs*",
        "disabled":         "",  # INSTANTLY_API_KEY not set — old manual-copy behavior, no status line
    }
    status_line = status_labels.get(autosend_status, "")

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f":large_green_circle: *New lead magnet — {first_name} / {company_name}*"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Playbook:* <{drive_url}|Open in Google Drive>"}},
    ]
    if status_line:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": status_line}})
    blocks.append({"type": "divider"})
    reply_heading = "Reply sent" if autosend_status in ("sent", "sent_tag_failed") else "Reply to send"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{reply_heading}:*\n```{reply_text}```"}})

    try:
        requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=10)
    except Exception as e:
        print(f"Slack failed: {e}")

# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok"}

@app.post("/generate")
def generate(payload: PayloadIn):
    try:
        ctx = payload.dict()
        # Normalise the company name for every use in the doc (strip Inc/LLC/
        # taglines, fix casing) and lock the intro's guarantee wording.
        ctx["company_name"]     = clean_company_name(payload.company_name)
        ctx["intro_text"]       = normalise_intro(payload.intro_text)
        ctx["calendly_link"]    = CALENDLY_LINK
        ctx["proof_stats"]      = PROOF_STATS
        ctx["date"]             = datetime.now().strftime("%B %Y")
        ctx["leo_linkedin_url"] = LEO_LINKEDIN_URL

        html_str  = template.render(**ctx)
        filename  = f"{uuid.uuid4()}.pdf"
        out_path  = os.path.join(OUTPUT_DIR, filename)
        HTML(string=html_str, base_url=BASE_DIR).write_pdf(out_path)

        drive_url = upload_to_drive(out_path, payload.company_name)

        autosend_status = "disabled"
        reply_text = ""
        if drive_url:
            reply_text = build_reply_text(payload.first_name, payload.company_name, drive_url)

            if not INSTANTLY_API_KEY:
                autosend_status = "disabled"
            elif not payload.email:
                autosend_status = "no_email"
            elif not is_safe_to_autosend(reply_text):
                autosend_status = "unsafe"
            else:
                source_email = find_latest_inbound_email(payload.email, payload.campaign_id)
                if not source_email:
                    autosend_status = "no_thread_found"
                elif AUTOSEND_DRY_RUN:
                    print(f"[DRY RUN] Would reply to {payload.email} (thread {source_email.get('id')}, eaccount {source_email.get('eaccount')})")
                    autosend_status = "dry_run"
                else:
                    autosend_status = "sent" if send_reply(source_email, reply_text) else "send_failed"
                    if autosend_status == "sent":
                        tagged = tag_lead_magnet_followup(payload.email, payload.campaign_id)
                        autosend_status = "sent" if tagged else "sent_tag_failed"

            post_to_slack(payload.first_name, payload.company_name, drive_url, reply_text, autosend_status)

        # Best-effort: write the PDF link onto the lead's Close record so it
        # collects on the one centralised lead. Never blocks the response.
        close_pdf_status = "disabled"
        if drive_url and payload.email:
            try:
                close_pdf_status = push_pdf_to_close(payload.email, drive_url)
            except Exception as e:
                print(f"Close PDF push unexpected error: {e}")
                close_pdf_status = "write_failed"

        pdf_url = f"{PUBLIC_URL}/files/{filename}" if PUBLIC_URL else f"/files/{filename}"
        return JSONResponse({
            "drive_url": drive_url, "pdf_url": pdf_url, "filename": filename,
            "autosend_status": autosend_status, "close_pdf_status": close_pdf_status,
        })

    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": str(e), "error_type": type(e).__name__, "traceback": traceback.format_exc()
        })


@app.post("/generate-lookalike")
def generate_lookalike(payload: LookalikePayloadIn):
    """Lookalike lead magnet: given the prospect's own best customer (seed_domain),
    pull Ocean.io lookalikes, tier them, render a branded PDF, and deliver it the
    same way as /generate (Drive → threaded Instantly reply → Close → Slack).
    Clay routes here only when a valid seed customer was found; otherwise it calls
    /generate for the standard 3-plays PDF."""
    try:
        countries = [payload.country] if payload.country.strip() else None
        raw = call_ocean_lookalike(payload.seed_domain, payload.size, countries)
        tiers, total = tier_companies(raw)
        if total == 0:
            return JSONResponse(status_code=422, content={
                "error": "no_lookalikes", "seed_domain": payload.seed_domain,
                "detail": "Ocean.io returned no companies for this seed - route to the 3-plays /generate instead.",
            })

        company_name = clean_company_name(payload.company_name)
        seed = payload.seed_customer_name.strip() or payload.seed_domain
        ctx = {
            "company_name": company_name,
            "company_logo_url": payload.company_logo_url,
            "seed_customer_name": seed,
            "total_count": total,
            "intro_text": (
                f"You've already won {seed}. These are the companies that look most like "
                f"them - same shape, same size, same market. A ready-made target list for "
                f"{company_name}, ranked closest-match first."
            ),
            "proof_stats": PROOF_STATS,
            "tiers": tiers,
            "calendly_link": CALENDLY_LINK,
        }

        html_str = template_lookalike.render(**ctx)
        filename = f"{uuid.uuid4()}.pdf"
        out_path = os.path.join(OUTPUT_DIR, filename)
        HTML(string=html_str, base_url=BASE_DIR).write_pdf(out_path)

        drive_url = upload_to_drive(out_path, f"{company_name} lookalikes")

        autosend_status = "disabled"
        reply_text = ""
        if drive_url:
            reply_text = build_reply_lookalike_text(payload.first_name, seed, total, drive_url)

            if not INSTANTLY_API_KEY:
                autosend_status = "disabled"
            elif not payload.email:
                autosend_status = "no_email"
            elif not is_safe_to_autosend(reply_text):
                autosend_status = "unsafe"
            else:
                source_email = find_latest_inbound_email(payload.email, payload.campaign_id)
                if not source_email:
                    autosend_status = "no_thread_found"
                elif AUTOSEND_DRY_RUN:
                    print(f"[DRY RUN] Would reply to {payload.email} (thread {source_email.get('id')}, eaccount {source_email.get('eaccount')})")
                    autosend_status = "dry_run"
                else:
                    autosend_status = "sent" if send_reply(source_email, reply_text) else "send_failed"
                    if autosend_status == "sent":
                        tagged = tag_lead_magnet_followup(payload.email, payload.campaign_id)
                        autosend_status = "sent" if tagged else "sent_tag_failed"

            post_to_slack(payload.first_name, f"{company_name} (lookalikes of {seed})", drive_url, reply_text, autosend_status)

        close_pdf_status = "disabled"
        if drive_url and payload.email:
            try:
                close_pdf_status = push_pdf_to_close(payload.email, drive_url)
            except Exception as e:
                print(f"Close PDF push unexpected error: {e}")
                close_pdf_status = "write_failed"

        pdf_url = f"{PUBLIC_URL}/files/{filename}" if PUBLIC_URL else f"/files/{filename}"
        return JSONResponse({
            "drive_url": drive_url, "pdf_url": pdf_url, "filename": filename,
            "total_companies": total, "seed_domain": payload.seed_domain,
            "autosend_status": autosend_status, "close_pdf_status": close_pdf_status,
        })

    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": str(e), "error_type": type(e).__name__, "traceback": traceback.format_exc()
        })


# ─────────────────────────────────────────────────────────────────────────────
# MICROSITE SWAP: on a positive reply, generate the GTM Playbook Microsite and
# post the link to Slack for an SDR to film a Loom over. NO auto-reply is sent
# to the prospect (the SDR sends the Loom + link manually).
# ─────────────────────────────────────────────────────────────────────────────
import threading as _threading

MICROSITE_BASE = os.environ.get(
    "MICROSITE_BASE",
    "https://gtm-playbook-microsite-production-f986.up.railway.app",
).rstrip("/")


class MicrositeReq(BaseModel):
    first_name: str = ""
    company_name: str = ""
    domain: str = ""
    website: str = ""
    email: str = ""
    prepared_for: str = ""


def _ms_domain(req) -> str:
    d = (req.domain or req.website or "").strip()
    if not d and req.email and "@" in req.email:
        d = req.email.split("@")[-1]
    d = d.replace("https://", "").replace("http://", "").rstrip("/").split("/")[0]
    return d.lower()


def _ms_generate_url(domain: str, company_name: str, prepared_for: str):
    """Kick off async generation on the microsite service and poll for the URL."""
    job = None
    try:
        r = requests.post(f"{MICROSITE_BASE}/generate",
                          json={"domain": domain, "company_name": company_name,
                                "prepared_for": prepared_for}, timeout=30)
        job = r.json().get("job_id")
    except Exception as e:
        print(f"microsite /generate kickoff failed: {e}")
    if not job:
        try:
            r2 = requests.post(f"{MICROSITE_BASE}/generate/sync",
                              json={"domain": domain, "company_name": company_name,
                                    "prepared_for": prepared_for}, timeout=200)
            return r2.json().get("url")
        except Exception as e:
            print(f"microsite /generate/sync failed: {e}")
            return None
    for _ in range(48):  # up to ~4 min
        time.sleep(5)
        try:
            s = requests.get(f"{MICROSITE_BASE}/generate/{job}", timeout=15).json()
        except Exception:
            continue
        if s.get("status") == "completed":
            return s.get("url")
        if s.get("status") == "failed":
            print(f"microsite generation failed: {s.get('error')}")
            return None
    return None


def _ms_time_pitch():
    """Two live Calendly slots if the API cooperates, else a generic ask.

    Deliberately never returns a [TIME n] placeholder. This message sends
    itself, so a placeholder would trip is_safe_to_autosend and silently stop
    the whole flow. Falling back to a line that needs no times keeps the
    playbook going out on time; the Calendly link below it still converts.
    Returns (pitch_line, slots_ok).
    """
    try:
        slots = get_calendly_slots()
    except Exception as e:
        print(f"microsite calendly slots failed: {e}")
        slots = ["CALENDLY_ERROR: exception"]
    is_err = any(s.startswith("CALENDLY_ERROR") for s in slots)
    if is_err:
        print(f"microsite calendly: {slots[0]}", flush=True)
    if not is_err and len(slots) >= 2:
        return f"Does {slots[0]} or {slots[1]} work for a quick chat?", True
    if not is_err and len(slots) == 1:
        return f"Does {slots[0]} work for a quick chat?", True
    return "Are you free for a quick chat in the next few days?", False


def _ms_reply_text(first_name: str, company_name: str, url: str, time_pitch: str) -> str:
    """Message 1, sent automatically the moment the playbook is ready.

    Carries no Loom placeholder, because waiting on a human to paste one is
    exactly the weekend delay this replaces. It flags that a video is coming
    so the SDR's message a few minutes later reads as the promised follow-up
    rather than a second cold touch.
    """
    fn = first_name or "there"
    return (f"Hey {fn}, great to hear back.\n\n"
            f"Here's the playbook I put together for {company_name}, yours to keep.\n\n"
            f"{url}\n\n"
            f"I'm recording a short video walking through it, so that'll land in "
            f"a few minutes.\n\n"
            f"If it's useful, I'd be happy to talk it through live and show you how "
            f"we build the go-to-market engine behind it, with qualified demos booked "
            f"inside 45 days or you don't pay.\n\n"
            f"{time_pitch}\n\n"
            f"If neither suits, grab whatever's easiest here: {CALENDLY_LINK}")


def _ms_sdr_loom_text(first_name: str) -> str:
    """Message 2, pasted by the SDR with their Loom about 10 minutes later."""
    fn = first_name or "there"
    return (f"{fn}, sent the playbook over a few minutes ago.\n\n"
            f"Here's the short video I just shot walking through it:\n\n"
            f"[PASTE LOOM LINK]\n\n"
            f"Happy to run through any of it live if that's easier.")


_MS_SEND_LABEL = {
    "sent":        ":white_check_mark: *The playbook has already gone to the prospect.*",
    "dry_run":     ":test_tube: *DRY RUN. A real thread matched and this would have sent, but AUTOSEND_DRY_RUN is on. Nothing went out.*",
    "no_key":      ":warning: *NOT SENT - SMARTLEAD_API_KEY is not set. Send the playbook message below by hand, now.*",
    "no_thread":   ":warning: *NOT SENT - no reply thread found in Smartlead. Send the playbook message below by hand, now.*",
    "unsafe":      ":warning: *NOT SENT - the draft still had an unfilled placeholder. Fix it and send by hand.*",
    "send_failed": ":rotating_light: *NOT SENT - Smartlead rejected the reply. Send the playbook message below by hand, now.*",
}


def _ms_post_slack(first_name: str, company_name: str, url: str, reply_text: str, ok: bool,
                   slots_ok: bool = True, send_status: str = "no_key",
                   loom_text: str = "", close_url: str = ""):
    if not SLACK_WEBHOOK_URL:
        return
    if ok:
        sent = send_status in ("sent", "dry_run")
        header = (f":white_check_mark: *Playbook sent - {first_name} / {company_name}*"
                  if sent else
                  f":movie_camera: *Positive reply - {first_name} / {company_name}*")
        steps = ("*Your steps:*\n"
                 "1. Open the playbook and film a Loom walking over it\n"
                 "2. Copy the video message below and swap `[PASTE LOOM LINK]` for your Loom URL\n"
                 "3. Reply on the same thread about 10 minutes after the playbook went out\n"
                 "4. Send. Your signature appends automatically\n"
                 if sent else
                 "*Your steps, nothing was sent automatically:*\n"
                 "1. Send the playbook message below on the existing thread now\n"
                 "2. Film a Loom walking over the playbook\n"
                 "3. About 10 minutes later, send the video message with your Loom URL\n")
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f"{header}\n{_MS_SEND_LABEL.get(send_status, '')}"}},
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f"*GTM Playbook microsite:* <{url}|Open playbook>"
                        + (f"\n*Close CRM:* <{close_url}|Open lead - call via the CRM line>" if close_url else "")}},
            {"type": "section", "text": {"type": "mrkdwn", "text": steps}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn",
                "text": (f"*2. Video message, send this ~10 min later:*\n```{loom_text}```"
                         if sent else
                         f"*1. Playbook message, send this now:*\n```{reply_text}```")}},
        ]
        if not sent and loom_text:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": f"*2. Video message, ~10 min after that:*\n```{loom_text}```"}})
        if sent:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                "text": f"What the prospect already has:\n{reply_text[:280]}..."}]})
        if not slots_ok:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                "text": ":warning: Calendly slots could not be pulled, so the times were left blank."}]})
    else:
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f":warning: *Positive reply, microsite generation FAILED - {first_name} / {company_name}*\n"
                        f"Generate manually or check the microsite service. Nothing was auto-sent."
                        + (f"\n*Close CRM:* <{close_url}|Open lead - call via the CRM line>" if close_url else "")}},
        ]
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=10)
    except Exception as e:
        print(f"microsite Slack post failed: {e}")


def _ms_autosend(email: str, reply_text: str) -> str:
    """Send the playbook message immediately. Returns a _MS_SEND_LABEL key.

    Never raises - a send failure has to still produce a Slack card telling
    the SDR to send by hand, otherwise a silent failure loses the prospect.
    """
    if not SMARTLEAD_API_KEY:
        return "no_key"
    if not is_safe_to_autosend(reply_text):
        return "unsafe"
    thread = sl_find_thread(email)
    if not thread:
        return "no_thread"
    if AUTOSEND_DRY_RUN:
        print(f"microsite DRY RUN, would reply on campaign "
              f"{thread['campaign_id']} stats {thread['stats_id']}", flush=True)
        return "dry_run"
    ok = sl_reply_in_thread(thread["campaign_id"], thread["stats_id"],
                            reply_text_to_html(reply_text))
    return "sent" if ok else "send_failed"


def _ms_run(req):
    domain = _ms_domain(req)
    company = clean_company_name(req.company_name) if req.company_name else (
        domain.split(".")[0].title() if domain else "")
    prepared_for = (req.prepared_for or req.first_name or "").strip()
    url = _ms_generate_url(domain, company, prepared_for) if domain else None
    print(f"microsite playbook for {domain} ({company}): {url}", flush=True)
    time_pitch, slots_ok = _ms_time_pitch()
    reply = _ms_reply_text(req.first_name, company, url or "", time_pitch)
    loom = _ms_sdr_loom_text(req.first_name)

    # Send first, then post the card, so the card can state what actually
    # happened rather than what was intended.
    send_status = "no_thread"
    if url and req.email:
        send_status = _ms_autosend(req.email, reply)
        print(f"microsite autosend {req.email}: {send_status}", flush=True)

    close_url = close_lead_link(req.email or "", req.first_name or "", company)
    _ms_post_slack(req.first_name, company, url or "", reply, bool(url), slots_ok,
                   send_status, loom, close_url)
    if url and req.email:
        try:
            push_pdf_to_close(req.email, url)
        except Exception as e:
            print(f"microsite Close write failed: {e}")


@app.post("/generate-microsite")
def generate_microsite_endpoint(req: MicrositeReq):
    """Positive reply -> generate the GTM Playbook Microsite -> auto-reply the
    playbook to the prospect immediately -> Slack card asking the SDR for a
    Loom about 10 minutes later."""
    _threading.Thread(target=_ms_run, args=(req,), daemon=True).start()
    return JSONResponse({"status": "processing",
                         "message": "Generating microsite; playbook auto-replies to the prospect, "
                                    "then a Slack card asks the SDR for a Loom ~10 min later."})


# --- Smartlead webhook ------------------------------------------------------
# Smartlead posts its own payload shape, which does not match MicrositeReq, so
# pointing a webhook straight at /generate-microsite yields an empty microsite
# and no error. This maps the fields, then reuses the same _ms_run.

# Second gate on top of Smartlead's own category filter, so a mis-scoped
# webhook cannot fire the magnet at someone who said no.
_SL_POSITIVE = {c.strip().lower() for c in os.environ.get(
    "SMARTLEAD_POSITIVE_CATEGORIES",
    "interested,meeting request,information request,positive response but no reply"
).split(",") if c.strip()}


def _sl_pick(payload: dict, *names):
    """First non-empty value among names, checked at the top level then in any
    nested lead / custom-field dict. Smartlead moves these around by event type."""
    pools = [payload]
    for key in ("lead", "lead_data", "leadData", "custom_fields", "customFields", "metadata"):
        val = payload.get(key)
        if isinstance(val, dict):
            pools.append(val)
            inner = val.get("custom_fields") or val.get("customFields")
            if isinstance(inner, dict):
                pools.append(inner)
    for name in names:
        for pool in pools:
            v = pool.get(name)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


@app.post("/smartlead-webhook")
async def smartlead_webhook(request: Request):
    payload = await request.json()

    # log the raw shape - field names vary by event type, and this is the only
    # way to confirm the mapping against a real fire
    print(f"smartlead-webhook raw: {json.dumps(payload)[:1500]}", flush=True)

    secret = os.environ.get("SMARTLEAD_WEBHOOK_SECRET", "")
    if secret and payload.get("secret_key") != secret:
        return JSONResponse({"status": "rejected", "reason": "bad secret"}, status_code=401)

    category = _sl_pick(payload, "lead_category", "leadCategory", "sl_lead_category",
                        "category", "new_category").lower()
    if category and category not in _SL_POSITIVE:
        print(f"smartlead-webhook skipped, category={category!r}", flush=True)
        return JSONResponse({"status": "skipped", "reason": f"category {category}"})

    email = _sl_pick(payload, "to_email", "lead_email", "sl_lead_email", "email", "toEmail")
    first = _sl_pick(payload, "firstName", "first_name", "to_name", "lead_first_name", "toName")
    company = _sl_pick(payload, "companyName", "company_name", "company")
    website = _sl_pick(payload, "website", "company_website", "companyWebsite", "domain")

    # to_name arrives as a full name on some events; the greeting only wants
    # the first word
    if first and " " in first:
        first = first.split()[0]

    if not (email or website):
        print("smartlead-webhook: no email or website, cannot resolve a domain", flush=True)
        return JSONResponse({"status": "skipped", "reason": "no email or website"})

    req = MicrositeReq(first_name=first, company_name=company, website=website,
                       email=email, prepared_for=first)
    print(f"smartlead-webhook -> first={first!r} company={company!r} "
          f"website={website!r} email={email!r}", flush=True)
    _threading.Thread(target=_ms_run, args=(req,), daemon=True).start()
    return JSONResponse({"status": "processing", "email": email})

# --- Smartlead reply notifications -------------------------------------------
# Replaces the Instantly -> Clay -> Slack table. Every categorised reply posts
# to its own channel with the phone number attached, so Leo can pick up the
# phone without going and looking the lead up.

REPLIES_SLACK_WEBHOOK = os.environ.get("REPLIES_SLACK_WEBHOOK_URL", "")

# Which categories post. Kept wide by default: seeing a "not interested" land
# is how you notice the AI is mis-categorising, and the old flow proved that
# happens - it labelled "No thank you" as a positive reply.
_SL_NOTIFY = {c.strip().lower() for c in os.environ.get(
    "SMARTLEAD_NOTIFY_CATEGORIES",
    "interested,meeting request,information request,positive response but no reply"
).split(",") if c.strip()}


def _sl_reply_text(payload: dict) -> str:
    for k in ("reply_body", "reply_message", "email_body", "body", "message",
              "reply_text", "text"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for kk in ("text", "html", "body"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()
    return ""


def _sl_clean_reply(text: str, limit: int = 600) -> str:
    """Strip quoted history so the card shows what they actually wrote."""
    if not text:
        return "(no reply body in the webhook)"
    text = re.sub(r"<[^>]+>", " ", text)
    # cut at the first quoted-reply marker
    for marker in (r"\nOn .{0,80} wrote:", r"\n>", r"\n-{2,}\s*Original Message",
                   r"\nFrom:\s"):
        m = re.search(marker, text)
        if m:
            text = text[:m.start()]
            break
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:limit] + "...") if len(text) > limit else text


@app.post("/smartlead-reply")
async def smartlead_reply(request: Request):
    """Post a Smartlead reply into the replies channel, phone number included."""
    payload = await request.json()
    print(f"smartlead-reply raw: {json.dumps(payload)[:1500]}", flush=True)

    secret = os.environ.get("SMARTLEAD_WEBHOOK_SECRET", "")
    if secret and payload.get("secret_key") != secret:
        return JSONResponse({"status": "rejected"}, status_code=401)

    category = _sl_pick(payload, "lead_category", "leadCategory", "sl_lead_category",
                        "category", "new_category")
    if category and category.lower() not in _SL_NOTIFY:
        return JSONResponse({"status": "skipped", "reason": f"category {category}"})

    email = _sl_pick(payload, "to_email", "lead_email", "sl_lead_email", "email")
    first = _sl_pick(payload, "firstName", "first_name", "lead_first_name", "to_name")
    last = _sl_pick(payload, "lastName", "last_name", "lead_last_name")
    company = _sl_pick(payload, "companyName", "company_name", "company")
    website = _sl_pick(payload, "website", "company_website", "companyWebsite")
    phone = _sl_pick(payload, "phone_number", "phone", "phoneNumber", "mobile")
    campaign = _sl_pick(payload, "campaign_name", "campaignName")

    if first and not last and " " in first:
        first, last = first.split(" ", 1)
    name = " ".join(x for x in (first, last) if x) or (email.split("@")[0] if email else "unknown")
    if not website and email and "@" in email:
        website = "https://" + email.split("@")[-1]

    reply = _sl_clean_reply(_sl_reply_text(payload))
    when = _sl_pick(payload, "event_timestamp", "timestamp", "created_at", "reply_time")

    lines = [f"*Name:* {name}"]
    if email:    lines.append(f"*Email:* {email}")
    if company:  lines.append(f"*Company:* {company}")
    if website:  lines.append(f"*Website:* {website}")
    # the whole point of the card - no phone means nobody can act on it fast
    lines.append(f"*Phone:* {phone}" if phone else "*Phone:* _not on the lead record_")
    if campaign: lines.append(f"*Campaign:* {campaign}")
    if when:     lines.append(f"*Received:* {when}")
    if category: lines.append(f"*Smartlead category:* {category}")

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": ":star2: *New Reply Received* :star2:\n" + "\n".join(lines)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Reply:*\n>{reply}"}},
    ]

    hook = REPLIES_SLACK_WEBHOOK or SLACK_WEBHOOK_URL
    if not hook:
        print("smartlead-reply: no Slack webhook configured", flush=True)
        return JSONResponse({"status": "no_webhook"})
    try:
        requests.post(hook, json={"blocks": blocks}, timeout=10)
    except Exception as e:
        print(f"smartlead-reply Slack post failed: {e}", flush=True)
        return JSONResponse({"status": "slack_failed"}, status_code=502)
    return JSONResponse({"status": "posted", "email": email})
