import os
import json
import uuid
import traceback
from datetime import datetime

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="Lead Magnet Render Service")
app.mount("/files", StaticFiles(directory=OUTPUT_DIR), name="files")

env = Environment(loader=FileSystemLoader(BASE_DIR))
template = env.get_template("template.html")

CALENDLY_LINK = os.environ.get("CALENDLY_LINK", "https://calendly.com/thegtmagency/")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

PROOF_STATS = [
    {"value": "$7.8M", "label": "Pipeline generated for AirOps"},
    {"value": "100/mo", "label": "Meetings booked for Peoplelogic"},
    {"value": "500+", "label": "SaaS companies scaled"},
]


def split_intro_paragraphs(text: str) -> list[str]:
    text = text.replace('\n', ' ').strip()
    sentences = [s.strip() for s in text.split('. ') if s.strip()]
    sentences = [s if s.endswith('.') else s + '.' for s in sentences]
    paragraphs = []
    for i in range(0, len(sentences), 2):
        group = sentences[i:min(i + 2, len(sentences))]
        paragraphs.append(' '.join(group))
    return paragraphs


def upload_to_drive(pdf_path: str, company_name: str) -> str:
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    service = build("drive", "v3", credentials=creds)

    file_metadata = {
        "name": f"{company_name} - Outbound Playbook.pdf",
        "parents": [DRIVE_FOLDER_ID] if DRIVE_FOLDER_ID else []
    }
    media = MediaFileUpload(pdf_path, mimetype="application/pdf")
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id",
        supportsAllDrives=True
    ).execute()

    file_id = file.get("id")

    service.permissions().create(
        fileId=file_id,
        body={"role": "reader", "type": "anyone"},
        supportsAllDrives=True
    ).execute()

    return f"https://drive.google.com/file/d/{file_id}/view"


class Strategy(BaseModel):
    strategyName: str
    goal: str
    whyThisFitsYou: str
    triggerDefinition: str
    technologyUsed: str
    targetPersona: str
    channel: str
    execution: list[str]


class PayloadIn(BaseModel):
    first_name: str
    company_name: str
    company_logo_url: str
    intro_text: str
    buyer_personas: list[str]
    strategies: list[Strategy]


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/generate")
def generate(payload: PayloadIn):
    try:
        context = payload.dict()
        context["calendly_link"] = CALENDLY_LINK
        context["proof_stats"] = PROOF_STATS
        context["date"] = datetime.now().strftime("%B %Y")
        context["intro_paragraphs"] = split_intro_paragraphs(payload.intro_text)

        html_str = template.render(**context)

        filename = f"{uuid.uuid4()}.pdf"
        out_path = os.path.join(OUTPUT_DIR, filename)
        HTML(string=html_str, base_url=BASE_DIR).write_pdf(out_path)

        drive_url = None
        if GOOGLE_CREDENTIALS_JSON and DRIVE_FOLDER_ID:
            drive_url = upload_to_drive(out_path, payload.company_name)

        base_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
        pdf_url = f"{base_url}/files/{filename}" if base_url else f"/files/{filename}"

        return JSONResponse({
            "drive_url": drive_url,
            "pdf_url": pdf_url,
            "filename": filename
        })

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": str(e),
                "error_type": type(e).__name__,
                "traceback": traceback.format_exc(),
            },
        )
