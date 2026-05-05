import asyncio
import base64
import json
import os
import re
import secrets
import uuid
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from supabase import Client, create_client

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

_supabase_url = os.getenv("SUPABASE_URL")
_supabase_key = os.getenv("SUPABASE_KEY")
supabase: Client | None
if _supabase_url and _supabase_key:
    supabase = create_client(_supabase_url, _supabase_key)
else:
    supabase = None

_anthropic_key = os.getenv("ANTHROPIC_API_KEY")
anthropic_client: anthropic.Anthropic | None
if _anthropic_key:
    anthropic_client = anthropic.Anthropic(api_key=_anthropic_key)
else:
    anthropic_client = None

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

pages = APIRouter()


@pages.get("/donate")
async def donate_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="donate.html",
        context={
            "title": "Donate — Maryland & Pittsburgh Food Banks"
        }
    )

@pages.get("/leaderboards")
async def leaderboards_page():
    return RedirectResponse(url="/donate", status_code=307)

app.include_router(pages)

BUCKET_MAP_PHOTOS = "map-photos"
MAX_IMAGE_BYTES = 12 * 1024 * 1024
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}

http_basic = HTTPBasic() 

def require_admin(credentials: HTTPBasicCredentials = Depends(http_basic)):
    user = os.getenv("ADMIN_USERNAME", "").encode("utf-8")
    password = os.getenv("ADMIN_PASSWORD", "").encode("utf-8")
    
    if not user or not password:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Admin credentials not configured in .env"
        )

    is_correct_username = secrets.compare_digest(credentials.username.encode("utf-8"), user)
    is_correct_password = secrets.compare_digest(credentials.password.encode("utf-8"), password)
    
    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials

# Define the router
admin_router = APIRouter()

# Attach the dependency directly to the route instead of the whole router
@admin_router.get("/admin", dependencies=[Depends(require_admin)])
async def admin_dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"title": "Admin — map pins"},
    )

def _safe_image_ext(filename: str | None) -> str:
    suf = Path(filename or "").suffix.lower()
    if suf in ALLOWED_IMAGE_EXT:
        return suf
    return ".jpg"


def _guess_content_type(ext: str, declared: str | None) -> str:
    if declared and declared.startswith("image/"):
        return declared
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }.get(ext, "application/octet-stream")


@admin_router.post("/api/admin/add-pin")
async def admin_add_pin(
    title: str = Form(...),
    description: str = Form(...),
    lat: float = Form(...),
    lng: float = Form(...),
    photos: list[UploadFile] | None = File(None),
):
    if supabase is None:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Set SUPABASE_URL and SUPABASE_KEY in .env.",
        )

    pin_insert = (
        supabase.table("map_pins")
        .insert(
            {
                "title": title.strip(),
                "description": description.strip(),
                "lat": lat,
                "lng": lng,
            }
        )
        .execute()
    )
    if not pin_insert.data:
        raise HTTPException(status_code=500, detail="Failed to create map pin.")
    pin_id = pin_insert.data[0]["id"]

    uploaded_urls: list[str] = []
    if photos is None:
        photo_list: list[UploadFile] = []
    elif isinstance(photos, UploadFile):
        photo_list = [photos]
    else:
        photo_list = list(photos)

    for upload in photo_list:
        raw = await upload.read()
        if not raw:
            continue
        if len(raw) > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Image too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB): {upload.filename!r}",
            )

        ext = _safe_image_ext(upload.filename)
        unique_name = f"{uuid.uuid4().hex}{ext}"
        storage_path = f"pins/{pin_id}/{unique_name}"
        ctype = _guess_content_type(ext, upload.content_type)

        try:
            supabase.storage.from_(BUCKET_MAP_PHOTOS).upload(
                storage_path,
                raw,
                file_options={"content-type": ctype, "upsert": "true"},
            )
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Storage upload failed for {upload.filename!r}: {e!s}",
            ) from e

        public_url = supabase.storage.from_(BUCKET_MAP_PHOTOS).get_public_url(storage_path)
        uploaded_urls.append(public_url)

        supabase.table("pin_photos").insert({"pin_id": pin_id, "image_url": public_url}).execute()

    return {
        "success": True,
        "pin_id": pin_id,
        "photo_urls": uploaded_urls,
        "photos_count": len(uploaded_urls),
    }


app.include_router(admin_router)


@app.get("/")
async def read_root(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "title": "Pittsburgh to D.C. - 350 Miles for Food Security",
            "mapbox_token": os.getenv("MAPBOX_ACCESS_TOKEN")
        }
    )


@app.get("/leaderboard/maryland-food-bank")
async def legacy_leaderboard_maryland():
    return RedirectResponse(url="/donate", status_code=307)


@app.get("/leaderboard/pittsburgh-food-bank")
async def legacy_leaderboard_pittsburgh():
    return RedirectResponse(url="/donate", status_code=307)


@app.get("/api/pins")
async def api_pins():
    if supabase is None:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Set SUPABASE_URL and SUPABASE_KEY in .env.",
        )

    pins_res = supabase.table("map_pins").select("*").order("id", desc=False).execute()
    pins = pins_res.data or []

    if not pins:
        return []

    pin_ids = [p["id"] for p in pins]
    photos_res = (
        supabase.table("pin_photos")
        .select("pin_id, image_url")
        .in_("pin_id", pin_ids)
        .order("id", desc=False)
        .execute()
    )
    rows = photos_res.data or []

    by_pin: dict = {}
    for row in rows:
        pid = row.get("pin_id")
        url = row.get("image_url")
        if pid is None or url is None:
            continue
        by_pin.setdefault(pid, []).append(url)

    for pin in pins:
        pin["photos"] = by_pin.get(pin["id"], [])

    return pins


@app.get("/api/gallery-photos")
async def api_gallery_photos():
    if supabase is None:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Set SUPABASE_URL and SUPABASE_KEY in .env.",
        )

    photos_res = (
        supabase.table("pin_photos")
        .select("id, pin_id, image_url")
        .order("id", desc=False)
        .execute()
    )
    photo_rows = photos_res.data or []
    if not photo_rows:
        return []

    pin_ids = sorted({row.get("pin_id") for row in photo_rows if row.get("pin_id") is not None})
    pin_titles_by_id: dict[int, str] = {}
    if pin_ids:
        pins_res = (
            supabase.table("map_pins")
            .select("id, title")
            .in_("id", pin_ids)
            .execute()
        )
        for row in pins_res.data or []:
            pid = row.get("id")
            if pid is None:
                continue
            pin_titles_by_id[pid] = (row.get("title") or "").strip()

    gallery = []
    for row in photo_rows:
        pin_id = row.get("pin_id")
        image_url = row.get("image_url")
        if pin_id is None or not image_url:
            continue
        gallery.append(
            {
                "id": row.get("id"),
                "pin_id": pin_id,
                "image_url": image_url,
                "pin_title": pin_titles_by_id.get(pin_id, ""),
            }
        )

    return gallery


CLAUDE_RECEIPT_MODEL = "claude-sonnet-4-6"
MAX_PDF_BYTES = 15 * 1024 * 1024
TOP_DONORS_LIMIT = 100

RECEIPT_SYSTEM_PROMPT = """You are a precise data extractor for nonprofit donation receipts.

Read the attached PDF (donation receipt). Extract:
- donor_name: the donor's full name as shown on the receipt (string).
- amount: the donation total as a number only (float, no currency symbols).
- charity_choice: which organization received the gift. It MUST be exactly the string "Maryland" if the gift is for the Maryland Food Bank or clearly Maryland-based food bank context, OR exactly "Pittsburgh" if the gift is for the Greater Pittsburgh Community Food Bank or clearly Pittsburgh-area food bank context. If you cannot determine with high confidence, choose based on the organization name printed on the receipt; if still ambiguous, prefer "Maryland" only if the receipt explicitly names Maryland Food Bank, otherwise "Pittsburgh" only if it explicitly names Greater Pittsburgh Community Food Bank or similar.

Return ONLY a raw JSON object with exactly these three keys and no other text, no markdown, no code fences:
{"donor_name": "...", "amount": 0.0, "charity_choice": "Maryland"}
or
{"donor_name": "...", "amount": 0.0, "charity_choice": "Pittsburgh"}"""


def _strip_json_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _parse_claude_json(text: str) -> dict:
    raw = _strip_json_fences(text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude did not return valid JSON: {e}") from e


def _call_claude_for_receipt(pdf_b64: str) -> str:
    if anthropic_client is None:
        raise RuntimeError("Anthropic client is not configured.")

    msg = anthropic_client.messages.create(
        model=CLAUDE_RECEIPT_MODEL,
        max_tokens=1024,
        system="You output only valid JSON objects when extracting receipt data. No markdown.",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": RECEIPT_SYSTEM_PROMPT,
                    },
                ],
            }
        ],
    )
    parts: list[str] = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


def _form_bool_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("true", "1", "yes", "on")


@app.post("/api/upload-receipt")
async def upload_receipt(
    file: UploadFile = File(..., description="Donation receipt PDF"),
    message: str = Form(""),
    anonymous: str = Form(""),
):
    if supabase is None:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Set SUPABASE_URL and SUPABASE_KEY in .env.",
        )
    if anthropic_client is None:
        raise HTTPException(
            status_code=503,
            detail="Anthropic is not configured. Set ANTHROPIC_API_KEY in .env.",
        )

    filename = (file.filename or "").lower()
    content_type = (file.content_type or "").lower()
    if "pdf" not in content_type and not filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="File must be a PDF.")

    body = await file.read()
    if len(body) == 0:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(body) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF exceeds maximum allowed size.")

    pdf_b64 = base64.standard_b64encode(body).decode("ascii")

    try:
        claude_text = await asyncio.to_thread(_call_claude_for_receipt, pdf_b64)
        data = _parse_claude_json(claude_text)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Receipt parsing failed: {e!s}") from e

    donor_name = data.get("donor_name")
    amount_raw = data.get("amount")
    charity_choice = data.get("charity_choice")
    anonymous_opt_in = _form_bool_truthy(anonymous)

    if not anonymous_opt_in:
        if not isinstance(donor_name, str) or not donor_name.strip():
            raise HTTPException(status_code=422, detail="Missing or invalid donor_name in model output.")
    try:
        amount = float(amount_raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Missing or invalid amount in model output.")

    if charity_choice not in ("Maryland", "Pittsburgh"):
        raise HTTPException(
            status_code=422,
            detail='charity_choice must be exactly "Maryland" or "Pittsburgh".',
        )

    msg_clean = message.strip() if isinstance(message, str) else ""
    final_name = "Anonymous" if anonymous_opt_in else str(donor_name).strip()

    row = {
        "donor_name": final_name,
        "amount": amount,
        "charity_choice": charity_choice,
        "message": msg_clean or None,
    }

    ins = supabase.table("donations").insert(row).execute()
    inserted = ins.data[0] if ins.data else None

    return {
        "success": True,
        "donation": inserted,
    }


@app.get("/api/leaderboard-data")
async def leaderboard_data():
    if supabase is None:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Set SUPABASE_URL and SUPABASE_KEY in .env.",
        )

    res = supabase.table("donations").select("donor_name, amount, charity_choice, message, created_at").execute()
    rows: list = res.data or []

    def total_for(choice: str | None) -> float:
        if choice is None:
            return sum(float(r.get("amount") or 0) for r in rows)
        return sum(float(r.get("amount") or 0) for r in rows if r.get("charity_choice") == choice)

    def sorted_top(choice: str | None) -> list:
        if choice is None:
            subset = list(rows)
        else:
            subset = [r for r in rows if r.get("charity_choice") == choice]
        subset.sort(key=lambda r: float(r.get("amount") or 0), reverse=True)
        return subset[:TOP_DONORS_LIMIT]

    return {
        "totals": {
            "maryland": round(total_for("Maryland"), 2),
            "pittsburgh": round(total_for("Pittsburgh"), 2),
            "combined": round(total_for(None), 2),
        },
        "top_donors": {
            "maryland": sorted_top("Maryland"),
            "pittsburgh": sorted_top("Pittsburgh"),
            "combined": sorted_top(None),
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
