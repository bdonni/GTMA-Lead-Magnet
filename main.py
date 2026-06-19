import os, uuid, json, traceback, requests, re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI
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

env.filters["sentences"] = _split_sentences  # register BEFORE get_template
template = env.get_template("template.html")

# ── ENV VARS ──────────────────────────────────────────────────────────────────
CALENDLY_LINK        = os.environ.get("CALENDLY_LINK",        "https://calendly.com/thegtmagency/")
CALENDLY_API_TOKEN   = os.environ.get("CALENDLY_API_TOKEN",   "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
DRIVE_FOLDER_ID      = os.environ.get("DRIVE_FOLDER_ID",      "")
SLACK_WEBHOOK_URL    = os.environ.get("SLACK_WEBHOOK_URL",    "")
PUBLIC_URL           = os.environ.get("PUBLIC_URL",           "").rstrip("/")
LEO_LINKEDIN_URL     = os.environ.get("LEO_LINKEDIN_URL",     "https://www.linkedin.com/in/leo-bosuener1/")
SABA_LINKEDIN_URL    = os.environ.get("SABA_LINKEDIN_URL",    "https://www.linkedin.com/in/saba-bosuener/")

# ── STATIC PROOF STATS ────────────────────────────────────────────────────────
PROOF_STATS = [
    {"value": "$7.8M",  "label": "Pipeline generated for AirOps"},
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
def get_calendly_slots() -> list[str]:
    if not CALENDLY_API_TOKEN:
        return ["CALENDLY_ERROR: no token set"]
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
        avail = requests.get(
            "https://api.calendly.com/event_type_available_times",
            headers=headers,
            params={
                "event_type": types[0]["uri"],
                "start_time": now.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
                "end_time":   (now + timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
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
        for slot in slots[:2]:
            dt = datetime.fromisoformat(slot["start_time"].replace("Z", "+00:00")).astimezone(eastern)
            out.append(f"{dt.strftime('%A')} at {dt.strftime('%-I:%M%p').lower()} {dt.strftime('%Z')}")
        return out
    except Exception as e:
        return [f"CALENDLY_ERROR: exception — {str(e)[:150]}"]

# ── SLACK ─────────────────────────────────────────────────────────────────────
def post_to_slack(first_name: str, company_name: str, drive_url: str):
    if not SLACK_WEBHOOK_URL:
        return
    slots   = get_calendly_slots()
    is_err  = any(s.startswith("CALENDLY_ERROR") for s in slots)
    if not is_err and len(slots) >= 2:
        time_pitch = f"Does {slots[0]} or {slots[1]} work to have a chat?"
    elif not is_err and len(slots) == 1:
        time_pitch = f"Does {slots[0]} work to have a chat?"
    else:
        time_pitch = f"Would love to find a time to chat. [DEBUG: {' | '.join(slots)}]"

    reply = (
        f"Hey {first_name}, great to hear back from you.\n\n"
        f"Here's the map I put together for {company_name}: {drive_url}\n\n"
        f"Would love to walk you through it and see where we could get your GTM moving.\n\n"
        f"{time_pitch}\n\n"
        f"If neither works, grab any time here: {CALENDLY_LINK}\n\n"
        f"Look forward to speaking soon.\nBest, Leo"
    )
    payload = {"blocks": [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f":large_green_circle: *New lead magnet — {first_name} / {company_name}*"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Playbook:* <{drive_url}|Open in Google Drive>"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Reply to send:*\n```{reply}```"}},
    ]}
    try:
        requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
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
        ctx["calendly_link"]    = CALENDLY_LINK
        ctx["proof_stats"]      = PROOF_STATS
        ctx["date"]             = datetime.now().strftime("%B %Y")
        ctx["leo_linkedin_url"] = LEO_LINKEDIN_URL
        ctx["saba_linkedin_url"]= SABA_LINKEDIN_URL

        html_str  = template.render(**ctx)
        filename  = f"{uuid.uuid4()}.pdf"
        out_path  = os.path.join(OUTPUT_DIR, filename)
        HTML(string=html_str, base_url=BASE_DIR).write_pdf(out_path)

        drive_url = upload_to_drive(out_path, payload.company_name)
        if drive_url:
            post_to_slack(payload.first_name, payload.company_name, drive_url)

        pdf_url = f"{PUBLIC_URL}/files/{filename}" if PUBLIC_URL else f"/files/{filename}"
        return JSONResponse({"drive_url": drive_url, "pdf_url": pdf_url, "filename": filename})

    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": str(e), "error_type": type(e).__name__, "traceback": traceback.format_exc()
        })
