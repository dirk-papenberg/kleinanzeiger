"""Telegram bot that turns photos into Kleinanzeigen.de drafts via Claude Vision."""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic, AsyncAnthropicBedrock
import httpx
from dotenv import load_dotenv

load_dotenv()
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from background_worker import BackgroundWorker
from queue_manager import QueueManager, JobStatus

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("kleinanzeigen-agent")
logging.getLogger("httpx").setLevel(logging.WARNING)

ALBUM_DEBOUNCE_SECONDS = 1.5

# Provider selection.
#   LLM_PROVIDER=bedrock  -> AWS Bedrock (auth via AWS_BEARER_TOKEN_BEDROCK)
#   LLM_PROVIDER=anthropic -> direct Anthropic API (auth via ANTHROPIC_API_KEY)
# Default: auto-detect (bedrock if AWS_BEARER_TOKEN_BEDROCK is set, else anthropic).
def _detect_provider() -> str:
    p = os.environ.get("LLM_PROVIDER", "").lower()
    if p in ("bedrock", "anthropic"):
        return p
    return "bedrock" if os.environ.get("AWS_BEARER_TOKEN_BEDROCK") else "anthropic"


LLM_PROVIDER = _detect_provider()
# Model id depends on provider. On Bedrock you need an inference-profile ID,
# e.g. "us.anthropic.claude-sonnet-4-5-20250929-v1:0".
CLAUDE_MODEL = os.environ.get(
    "CLAUDE_MODEL",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    if LLM_PROVIDER == "bedrock"
    else "claude-sonnet-4-6",
)
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

KLEINANZEIGEN_BOT_CMD = os.environ.get("KLEINANZEIGEN_BOT_CMD", "")
KLEINANZEIGEN_CONFIG = os.environ.get(
    "KLEINANZEIGEN_CONFIG",
    str(Path.home() / ".kleinanzeigen-agent" / "config.yaml"),
)
WORK_DIR = Path(
    os.environ.get(
        "KLEINANZEIGEN_WORK_DIR",
        str(Path.home() / ".kleinanzeigen-agent" / "ads"),
    )
)
QUEUE_DIR = Path(
    os.environ.get(
        "KLEINANZEIGEN_QUEUE_DIR",
        str(Path.home() / ".kleinanzeigen-agent" / "queue"),
    )
)
DEFAULT_SHIPPING_TYPE = os.environ.get("KLEINANZEIGEN_SHIPPING", "SHIPPING").upper()

LUNCH_PLAN_BASE_URL = os.environ.get(
    "LUNCH_PLAN_URL",
    "http://ubuntu.fritz.box:880/resources/plan",
)
LUNCH_PLAN_TZ = ZoneInfo("Europe/Berlin")

# Access control – comma-separated Telegram user IDs, e.g. "123456789,987654321"
_allowed_raw = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USER_IDS: set[int] = {
    int(uid.strip()) for uid in _allowed_raw.split(",") if uid.strip().isdigit()
}

# Queue and worker (initialized in main())
QUEUE_MANAGER: QueueManager | None = None
BACKGROUND_WORKER: BackgroundWorker | None = None


def _price_type_to_bot(s: str) -> str:
    s = (s or "").strip().upper()
    if s in ("FP", "FESTPREIS", "FIXED"):
        return "FIXED"
    if s in ("ZU VERSCHENKEN", "GIVE_AWAY", "GIVEAWAY", "VERSCHENKEN"):
        return "GIVE_AWAY"
    return "NEGOTIABLE"  # VB, VHB, default

SYSTEM_PROMPT = """Du bist Assistent für Kleinanzeigen.de-Inserate. Auf Fotos siehst du einen Gegenstand, den jemand verkaufen will.

Erstelle ein realistisches Inserat in deutscher Sprache und antworte ausschliesslich als JSON-Objekt mit diesen Feldern:

{
  "title": "kurz, max. 65 Zeichen, praegnant, mit Marke/Modell falls erkennbar",
  "category": "passende Kleinanzeigen-Kategorie als Vorschlag, z.B. 'Elektronik > Audio & Hifi'",
  "condition": "Neu | Sehr gut | Gut | In Ordnung | Defekt",
  "description": "3-6 Saetze: was ist es, Zustand, Besonderheiten, Masse/Groesse falls erkennbar. Sachlich, freundlich, ohne Uebertreibung. Kein Kontaktdaten-Geblubber. Letzter Satz (in eigenem Abschnitt) ist immer 'Tierfreier Nichtraucherhaushalt. Versand bei Kostenübernahme möglich.'",
  "price_eur": 25,
  "price_type": "FP | VB | VHB | zu verschenken",
  "price_reasoning": "1 Satz: warum dieser Preis (z.B. 'gebraucht ca. 40% vom Neupreis ~60 EUR')",
  "missing_info": ["Liste der Dinge, die du fuer ein besseres Inserat noch wissen solltest, z.B. 'Groesse', 'Funktioniert noch?'"],
  "photo_order": [0, 2, 1]
}

Das Feld photo_order enthaelt die 0-basierten Indizes aller uebergebenen Fotos in der Reihenfolge, in der sie im Inserat erscheinen sollen – das beste Uebersichtsfoto zuerst, Detail- oder Zusatzfotos danach. Bei nur einem Foto: [0].

Wenn du den Gegenstand nicht erkennst, setze title auf '?' und beschreibe in description, was du siehst. Antworte NUR mit dem JSON, keine Markdown-Codefences, kein Fliesstext drumherum."""


EDIT_SYSTEM_PROMPT = """Du bekommst ein bestehendes Kleinanzeigen-Inserat als JSON und einen Aenderungswunsch des Nutzers in deutscher Sprache.

Wende den Wunsch an und gib das **vollstaendige aktualisierte JSON** mit denselben Feldern zurueck (title, category, condition, description, price_eur, price_type, price_reasoning, missing_info). Aendere nur, was der Nutzer angefragt hat; alle anderen Felder uebernimmst du unveraendert. Wenn der Wunsch unklar oder unmoeglich ist, gib das JSON unveraendert zurueck und schreib eine kurze Erklaerung in price_reasoning.

Antworte NUR mit dem JSON, keine Codefences, kein Fliesstext."""


@dataclass
class Draft:
    photos: list[bytes] = field(default_factory=list)
    title: str = ""
    category: str = ""
    condition: str = ""
    description: str = ""
    price_eur: int = 0
    price_type: str = "VB"
    price_reasoning: str = ""
    missing_info: list[str] = field(default_factory=list)


DRAFTS: dict[int, Draft] = {}
PENDING_ALBUMS: dict[tuple[int, str], dict] = {}
CHAT_PHOTO_POOL: dict[int, list[bytes]] = {}  # accumulated photos per chat (cleared on reset)
ACTIVE_GENERATION_TASKS: dict[int, asyncio.Task] = {}  # in-progress generation tasks per chat


def get_anthropic() -> AsyncAnthropic | AsyncAnthropicBedrock:
    if LLM_PROVIDER == "bedrock":
        if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            raise RuntimeError(
                "LLM_PROVIDER=bedrock requires AWS_BEARER_TOKEN_BEDROCK"
            )
        # The SDK reads AWS_BEARER_TOKEN_BEDROCK from env automatically.
        return AsyncAnthropicBedrock(aws_region=AWS_REGION)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return AsyncAnthropic(api_key=key)


async def analyze_photos(photos: list[bytes]) -> dict:
    client = get_anthropic()
    content: list[dict] = []
    for img in photos[:8]:
        llm_img = img
        log.info("photo: %d KB -> %d KB for LLM", len(img) // 1024, len(llm_img) // 1024)
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(llm_img).decode(),
                },
            }
        )
    content.append({"type": "text", "text": "Erstelle das Inserat-JSON."})

    msg = await client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def _draft_to_json(d: Draft) -> str:
    return json.dumps(
        {
            "title": d.title,
            "category": d.category,
            "condition": d.condition,
            "description": d.description,
            "price_eur": d.price_eur,
            "price_type": d.price_type,
            "price_reasoning": d.price_reasoning,
            "missing_info": d.missing_info,
        },
        ensure_ascii=False,
        indent=2,
    )


async def apply_edit(d: Draft, user_request: str) -> dict:
    """Ask Claude to apply a free-form edit request to the current draft."""
    client = get_anthropic()
    user_msg = (
        f"Aktuelles Inserat:\n```json\n{_draft_to_json(d)}\n```\n\n"
        f"Aenderungswunsch des Nutzers:\n{user_request}"
    )
    msg = await client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=EDIT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def _apply_dict_to_draft(d: Draft, data: dict) -> None:
    d.title = str(data.get("title", d.title))[:80]
    d.category = str(data.get("category", d.category))
    d.condition = str(data.get("condition", d.condition))
    d.description = str(data.get("description", d.description))
    d.price_eur = int(data.get("price_eur", d.price_eur) or 0)
    d.price_type = str(data.get("price_type", d.price_type))
    d.price_reasoning = str(data.get("price_reasoning", d.price_reasoning))
    d.missing_info = list(data.get("missing_info", d.missing_info) or [])


def escape_md(s: str) -> str:
    return s.replace("*", "·").replace("_", " ").replace("`", "'")


def render_draft(d: Draft) -> str:
    missing = ""
    if d.missing_info:
        bullets = "\n".join(f"• {escape_md(m)}" for m in d.missing_info)
        missing = f"\n\n_Noch unklar:_\n{bullets}"
    return (
        f"*{escape_md(d.title)}*\n"
        f"_{escape_md(d.category)} · {escape_md(d.condition)}_\n\n"
        f"{escape_md(d.description)}\n\n"
        f"*Preis:* {d.price_eur} EUR {escape_md(d.price_type)}\n"
        f"_{escape_md(d.price_reasoning)}_"
        f"{missing}"
    )


def render_final(d: Draft) -> str:
    return (
        "📋 *Zum Kopieren:*\n\n"
        "```\n"
        f"{d.title}\n\n"
        f"{d.description}\n\n"
        f"Preis: {d.price_eur} EUR {d.price_type}\n"
        f"Kategorie-Vorschlag: {d.category}\n"
        f"Zustand: {d.condition}\n"
        "```"
    )


def keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("✅ Copy-Paste", callback_data="ok"),
            InlineKeyboardButton("🔁 Neu generieren", callback_data="regen"),
        ],
        [InlineKeyboardButton("❌ Verwerfen", callback_data="cancel")],
    ]
    if KLEINANZEIGEN_BOT_CMD:
        rows.insert(
            0, [InlineKeyboardButton("🚀 Direkt schalten", callback_data="publish")]
        )
    return InlineKeyboardMarkup(rows)


def _yaml_escape(s: str) -> str:
    """Quote a string safely for YAML using JSON (which is valid YAML)."""
    return json.dumps(s, ensure_ascii=False)


def _extract_login_from_yaml(config_text: str) -> tuple[str, str]:
    """Extract login.username and login.password from a simple YAML config text."""
    m = re.search(
        r"(?ms)^\s*login\s*:\s*\n(?P<body>(?:^[ \t]+.*\n?)*)",
        config_text,
    )
    if not m:
        return "", ""
    body = m.group("body")

    def _get_value(key: str) -> str:
        km = re.search(rf"(?m)^\s*{key}\s*:\s*(.+?)\s*$", body)
        if not km:
            return ""
        v = km.group(1).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        return v.strip()

    return _get_value("username"), _get_value("password")


def _strip_top_level_key_block(config_text: str, key: str) -> str:
    """Remove a top-level YAML key block (best-effort, for simple YAML layouts)."""
    pattern = rf"(?ms)^\s*{re.escape(key)}\s*:\s*\n(?:^[ \t]+.*\n?)*"
    return re.sub(pattern, "", config_text)


def write_ad_files(d: Draft) -> Path:
    """Persist photos + ad.yaml into a fresh working directory. Returns ad.yaml path."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "_", d.title)[:40] or "ad"
    ad_dir = WORK_DIR / f"{ts}_{safe_title}"
    ad_dir.mkdir(parents=True)

    for i, img in enumerate(d.photos, start=1):
        (ad_dir / f"{i:02d}.jpg").write_bytes(img)

    title = d.title.replace("\n", " ").strip()
    desc = d.description.strip()
    yaml_text = (
        "active: true\n"
        f"title: {_yaml_escape(title)}\n"
        f"description: {_yaml_escape(desc)}\n"
        f"category: {_yaml_escape(d.category)}\n"
        f"price: {int(d.price_eur)}\n"
        f"price_type: {_price_type_to_bot(d.price_type)}\n"
        f"shipping_type: {DEFAULT_SHIPPING_TYPE}\n"
        'images:\n  - "*.jpg"\n'
    )
    ad_file = ad_dir / "ad.yaml"
    ad_file.write_text(yaml_text, encoding="utf-8")
    return ad_file


async def run_kleinanzeigen_bot(ad_file: Path) -> tuple[int, str]:
    """Run kleinanzeigen-bot publish on the given ad. Returns (rc, combined_output)."""
    ad_dir = ad_file.parent
    run_config = ad_dir / "_run_config.yaml"

    cfg_text = ""
    cfg_user = ""
    cfg_pw = ""
    cfg_path = Path(KLEINANZEIGEN_CONFIG)
    if cfg_path.exists():
        cfg_text = cfg_path.read_text(encoding="utf-8")
        cfg_user, cfg_pw = _extract_login_from_yaml(cfg_text)

    env_user = os.environ.get("KLEINANZEIGEN_USERNAME", "").strip()
    env_pw = os.environ.get("KLEINANZEIGEN_PASSWORD", "").strip()
    login_user = env_user or cfg_user
    login_pw = env_pw or cfg_pw
    if not login_user or not login_pw:
        raise RuntimeError(
            "Kleinanzeigen-Login unvollstaendig: username und password erforderlich "
            "(in KLEINANZEIGEN_CONFIG oder per KLEINANZEIGEN_USERNAME/KLEINANZEIGEN_PASSWORD)."
        )

    base_config_text = cfg_text
    if base_config_text:
        base_config_text = _strip_top_level_key_block(base_config_text, "login")
        base_config_text = _strip_top_level_key_block(base_config_text, "ad_files")
        base_config_text = _strip_top_level_key_block(base_config_text, "browser")
        base_config_text = base_config_text.rstrip() + "\n"

    run_config.write_text(
        base_config_text
        + "login:\n"
        + f"  username: {_yaml_escape(login_user)}\n"
        + f"  password: {_yaml_escape(login_pw)}\n"
        + 'ad_files:\n  - "ad.yaml"\n'
        + "browser:\n"
        + "  arguments:\n"
        + "    - --no-sandbox\n"
        + "    - --disable-dev-shm-usage\n",
        encoding="utf-8",
    )
    cmd = shlex.split(KLEINANZEIGEN_BOT_CMD) + [
        "--workspace-mode=xdg",
        "--config",
        str(run_config),
        "publish",
        "--ads",
        "new",
    ]
    log.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout_b, _ = await proc.communicate()
    return proc.returncode or 0, stdout_b.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Lunch plan
# ---------------------------------------------------------------------------

async def fetch_lunch_plan(date: datetime.date) -> list[dict]:
    """Fetch the meal plan for *date* from the planning API."""
    date_str = date.isoformat()
    url = f"{LUNCH_PLAN_BASE_URL}?startDate={date_str}&endDate={date_str}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    # API may return a list of day-entries or a single dict
    return data if isinstance(data, list) else [data]


def format_lunch_message(plan_entries: list[dict], date: datetime.date) -> str:
    """Format the meal plan as a Telegram-ready Markdown message."""
    date_label = date.strftime("%d.%m.%Y")
    today_str = date.isoformat()
    entry = next((e for e in plan_entries if e.get("date") == today_str), None)

    if not entry or not entry.get("recipes"):
        return f"🍽️ *Mittagessen am {date_label}*\n\nKein Mittagessen geplant."

    lines = [f"🍽️ *Mittagessen am {date_label}*\n"]
    for recipe in entry["recipes"]:
        name = escape_md(recipe.get("name", "?"))
        cat = recipe.get("category", "")
        duration = recipe.get("duration", "")
        parts = [f"*{name}*"]
        if cat:
            parts.append(escape_md(cat))
        if duration:
            parts.append(f"{duration} min")
        lines.append("• " + " · ".join(parts))
    return "\n".join(lines)


async def send_lunch_plan(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: send today's lunch plan to all allowed users at 08:30."""
    today = datetime.datetime.now(LUNCH_PLAN_TZ).date()
    log.info("Sending lunch plan for %s to %d user(s)", today, len(ALLOWED_USER_IDS))
    try:
        plan = await fetch_lunch_plan(today)
    except Exception as e:
        log.error("Failed to fetch lunch plan: %s", e)
        return

    msg = format_lunch_message(plan, today)
    for user_id in ALLOWED_USER_IDS:
        try:
            await context.bot.send_message(user_id, msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.error("Failed to send lunch plan to user %d: %s", user_id, e)


async def cmd_lunch(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Show today's lunch plan on demand via /lunch."""
    today = datetime.datetime.now(LUNCH_PLAN_TZ).date()
    await update.message.reply_text("🔍 Hole Mittagessen…")
    try:
        plan = await fetch_lunch_plan(today)
    except Exception as e:
        log.error("Failed to fetch lunch plan on demand: %s", e)
        await update.message.reply_text(f"Fehler beim Abrufen des Mittagessens: {e}")
        return
    msg = format_lunch_message(plan, today)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Schick mir ein Foto (oder mehrere als Album) von dem Ding, das du verkaufen willst. "
        "Ich mache dir Titel, Beschreibung und Preisvorschlag für Kleinanzeigen.de.\n\n"
        "Wenn dir was nicht passt, schreib es einfach in normaler Sprache, z.B.:\n"
        "• \"Preis auf 30 erhöhen\"\n"
        "• \"Beschreibung etwas kürzer\"\n"
        "• \"erwähne, dass es noch in OVP ist\"\n"
        "• \"lockerer formulieren\"\n\n"
        "Befehl: /neu — aktuellen Entwurf verwerfen"
    )


async def cmd_neu(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    had_draft = chat_id in DRAFTS
    DRAFTS.pop(chat_id, None)
    CHAT_PHOTO_POOL.pop(chat_id, None)
    _task = ACTIVE_GENERATION_TASKS.pop(chat_id, None)
    if _task and not _task.done():
        _task.cancel()
    log.info("[chat=%d] /neu — draft discarded=%s", chat_id, had_draft)
    await update.message.reply_text("Ok, Entwurf verworfen. Schick neue Fotos.")


async def cmd_queue_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current queue status."""
    pending_count = QUEUE_MANAGER.get_pending_count()
    backout_count = QUEUE_MANAGER.get_backout_count()
    
    await update.message.reply_text(
        f"📊 Queue-Status:\n"
        f"⏳ Ausstehend: {pending_count}\n"
        f"❌ Warteschlange (fehlgeschlagen): {backout_count}\n\n"
        f"Nutze /backout um fehlgeschlagene Jobs zu sehen."
    )


async def cmd_backout_jobs(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Show backout jobs for this chat."""
    chat_id = update.effective_chat.id
    backout_jobs = QUEUE_MANAGER.get_backout_jobs(chat_id=chat_id)
    
    if not backout_jobs:
        await update.message.reply_text("Keine fehlgeschlagenen Jobs für diesen Chat.")
        return
    
    msg_lines = ["❌ Fehlgeschlagene Jobs:\n"]
    for job in backout_jobs:
        retry_cmd = f"/retry_{job.job_id[:8]}"
        msg_lines.append(
            f"• Job `{job.job_id[:8]}` ({job.job_type})\n"
            f"  Fehler: {job.error}\n"
            f"  Versuche: {job.retry_count}/{job.max_retries}\n"
            f"  Befehl: `{retry_cmd}`\n"
        )
    
    await update.message.reply_text(
        "".join(msg_lines),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_retry_job(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Retry a specific backout job. Usage: /retry_<job_id_prefix>"""
    cmd_text = (update.message.text or "").strip()
    match = re.match(r"/retry_(\S+)", cmd_text)
    if not match:
        await update.message.reply_text("Befehl: /retry_<job_id>")
        return
    
    job_id_prefix = match.group(1)
    chat_id = update.effective_chat.id
    backout_jobs = QUEUE_MANAGER.get_backout_jobs(chat_id=chat_id)
    
    # Find matching job
    matching = [j for j in backout_jobs if j.job_id.startswith(job_id_prefix)]
    if not matching:
        await update.message.reply_text(f"Kein Job mit ID `{job_id_prefix}` gefunden.")
        return
    
    job = matching[0]
    if QUEUE_MANAGER.retry_backout_job(job.job_id):
        await update.message.reply_text(
            f"✅ Job `{job.job_id[:8]}` zur Wiederholung eingeplant."
        )
    else:
        await update.message.reply_text("Konnte Job nicht zur Wiederholung hinzufügen.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-form edit request for the current draft."""
    chat_id = update.effective_chat.id
    user_request = (update.message.text or "").strip()
    log.info("[chat=%d] text message: %r", chat_id, user_request[:200])
    d = DRAFTS.get(chat_id)
    if not d:
        log.info("[chat=%d] no active draft, ignoring text", chat_id)
        await update.message.reply_text(
            "Kein Entwurf aktiv — schick mir erst ein Foto."
        )
        return
    if not user_request:
        return

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    log.info("[chat=%d] applying edit: %r", chat_id, user_request[:200])
    try:
        data = await apply_edit(d, user_request)
    except Exception as e:
        log.exception("edit failed")
        await context.bot.send_message(chat_id, f"Konnte den Wunsch nicht umsetzen: {e}")
        return

    _apply_dict_to_draft(d, data)
    await context.bot.send_message(
        chat_id,
        render_draft(d),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard(),
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    chat_id = update.effective_chat.id

    photo = msg.photo[-1]
    log.info(
        "[chat=%d] photo received (file_id=%s, %dx%d, album=%s)",
        chat_id, photo.file_id, photo.width, photo.height,
        msg.media_group_id or "none",
    )
    file = await context.bot.get_file(photo.file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    img_bytes = buf.getvalue()

    if msg.media_group_id:
        key = (chat_id, msg.media_group_id)
        entry = PENDING_ALBUMS.get(key)
        if entry is None:
            entry = {"photos": [], "task": None}
            PENDING_ALBUMS[key] = entry
        entry["photos"].append(img_bytes)
        if entry["task"]:
            entry["task"].cancel()
        entry["task"] = asyncio.create_task(
            _process_album_after_delay(key, context)
        )
        return

    # Cancel any in-progress generation for this chat
    existing = ACTIVE_GENERATION_TASKS.get(chat_id)
    if existing and not existing.done():
        existing.cancel()

    # Accumulate photo: seed pool from existing draft if this is a fresh pool
    is_addition = chat_id in CHAT_PHOTO_POOL or chat_id in DRAFTS
    if chat_id not in CHAT_PHOTO_POOL:
        d = DRAFTS.get(chat_id)
        CHAT_PHOTO_POOL[chat_id] = list(d.photos) if d else []
    CHAT_PHOTO_POOL[chat_id].append(img_bytes)

    # Immediate feedback — differentiate first photo from additions
    total = len(CHAT_PHOTO_POOL[chat_id])
    if is_addition:
        await context.bot.send_message(
            chat_id,
            f"📸 Noch ein Foto – generiere das Inserat neu mit {total} Bildern…",
        )
    else:
        await context.bot.send_message(
            chat_id,
            "📸 Foto erhalten, einen Moment…",
        )

    # Schedule new generation with all accumulated photos
    photos_snapshot = list(CHAT_PHOTO_POOL[chat_id])
    task = asyncio.create_task(_process_photos(chat_id, photos_snapshot, context))
    ACTIVE_GENERATION_TASKS[chat_id] = task

    def _on_task_done(t: asyncio.Task) -> None:
        if ACTIVE_GENERATION_TASKS.get(chat_id) is t:
            ACTIVE_GENERATION_TASKS.pop(chat_id, None)
    task.add_done_callback(_on_task_done)


async def _process_album_after_delay(
    key: tuple[int, str], context: ContextTypes.DEFAULT_TYPE
) -> None:
    try:
        await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    entry = PENDING_ALBUMS.pop(key, None)
    if not entry:
        return
    await context.bot.send_message(key[0], "🤖 Analysiere Album…")
    await _process_photos(key[0], entry["photos"], context)


async def _process_photos(
    chat_id: int, photos: list[bytes], context: ContextTypes.DEFAULT_TYPE
) -> None:
    log.info("[chat=%d] processing %d photo(s) with LLM", chat_id, len(photos))
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        data = await analyze_photos(photos)
    except Exception as e:
        log.exception("analyze failed")
        await context.bot.send_message(chat_id, f"Fehler bei der Analyse: {e}")
        return

    d = Draft(photos=photos)
    _apply_dict_to_draft(d, data)

    # Reorder photos as Claude suggested (best overview first)
    order = data.get("photo_order")
    if order and isinstance(order, list) and len(order) == len(photos):
        try:
            reordered = [photos[int(i)] for i in order if 0 <= int(i) < len(photos)]
            if len(reordered) == len(photos):
                d.photos = reordered
        except (TypeError, ValueError, IndexError):
            pass  # keep original order on any error
    if not d.title:
        d.title = "?"
    if not d.price_type:
        d.price_type = "VB"
    DRAFTS[chat_id] = d
    log.info(
        "[chat=%d] draft created: title=%r price=%s %s",
        chat_id, d.title, d.price_eur, d.price_type,
    )
    await context.bot.send_message(
        chat_id,
        render_draft(d),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard(),
    )


async def handle_publish_job(job_data: dict) -> tuple[bool, str]:
    """
    Handler for publish_ad jobs in the background worker.
    
    Returns (success, message).
    """
    ad_file = Path(job_data["ad_file"])
    chat_id = job_data["chat_id"]

    try:
        rc, output = await run_kleinanzeigen_bot(ad_file)
    except Exception as e:
        log.exception("[chat=%d] publish handler failed", chat_id)
        return False, f"Exception: {e}"

    if rc == 0:
        log.info("[chat=%d] kleinanzeigen-bot succeeded:\n%s", chat_id, output)
        return True, "✅ Anzeige geschaltet!"
    else:
        log.error("[chat=%d] kleinanzeigen-bot failed (rc=%d):\n%s", chat_id, rc, output)
        return False, f"Kleinanzeigen-Bot Fehler (rc={rc})"


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    chat_id = update.effective_chat.id
    log.info("[chat=%d] button pressed: %r", chat_id, q.data)
    d = DRAFTS.get(chat_id)
    if not d:
        await q.edit_message_text("Kein Entwurf mehr aktiv.")
        return

    if q.data == "ok":
        await q.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id, render_final(d), parse_mode=ParseMode.MARKDOWN
        )
        await context.bot.send_message(
            chat_id,
            "Öffne jetzt die Kleinanzeigen-App, neues Inserat anlegen, "
            "Fotos anhängen und den Block oben reinkopieren. Viel Erfolg!",
        )
        DRAFTS.pop(chat_id, None)
        CHAT_PHOTO_POOL.pop(chat_id, None)
    elif q.data == "regen":
        await q.edit_message_reply_markup(reply_markup=None)
        await _process_photos(chat_id, d.photos, context)
    elif q.data == "publish":
        if not KLEINANZEIGEN_BOT_CMD:
            await context.bot.send_message(
                chat_id,
                "Direktes Schalten ist nicht konfiguriert (KLEINANZEIGEN_BOT_CMD fehlt).",
            )
            return
        await q.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        
        # Write ad files
        try:
            ad_file = write_ad_files(d)
        except Exception as e:
            log.exception("Failed to write ad files")
            await context.bot.send_message(chat_id, f"Fehler beim Speichern: {e}")
            return

        # Queue the publish job
        job_id = str(uuid.uuid4())
        QUEUE_MANAGER.enqueue(
            job_id=job_id,
            chat_id=chat_id,
            job_type="publish_ad",
            data={
                "ad_file": str(ad_file),
                "chat_id": chat_id,
            },
        )

        # Notify user that job is queued
        await context.bot.send_message(
            chat_id,
            f"⏳ Anzeige wird geschaltet…\n_Job-ID: {job_id[:8]}_",
            parse_mode=ParseMode.MARKDOWN,
        )
        log.info(
            "[chat=%d job=%s] Publish job queued",
            chat_id, job_id,
        )
        
        # Clear draft and photo pool so user can start fresh
        DRAFTS.pop(chat_id, None)
        CHAT_PHOTO_POOL.pop(chat_id, None)
        await context.bot.send_message(
            chat_id,
            "Du kannst jetzt das nächste Inserat vorbereiten. "
            "Ich informiere dich, wenn das Schalten abgeschlossen ist.",
        )

    elif q.data == "cancel":
        DRAFTS.pop(chat_id, None)
        CHAT_PHOTO_POOL.pop(chat_id, None)
        _task = ACTIVE_GENERATION_TASKS.pop(chat_id, None)
        if _task and not _task.done():
            _task.cancel()
        await q.edit_message_text("Verworfen.")


def main() -> None:
    global QUEUE_MANAGER, BACKGROUND_WORKER
    
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")
    if not ALLOWED_USER_IDS:
        raise SystemExit("ALLOWED_USERS not set – set a comma-separated list of Telegram user IDs")
    log.info("Access restricted to user IDs: %s", ALLOWED_USER_IDS)
    if LLM_PROVIDER == "bedrock":
        if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            raise SystemExit("AWS_BEARER_TOKEN_BEDROCK not set (LLM_PROVIDER=bedrock)")
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY not set")
    log.info("LLM provider=%s model=%s", LLM_PROVIDER, CLAUDE_MODEL)

    # Initialize queue manager and background worker
    QUEUE_MANAGER = QueueManager(QUEUE_DIR)
    log.info("Queue manager initialized at %s", QUEUE_DIR)

    # Job handlers
    job_handlers = {
        "publish_ad": handle_publish_job,
    }
    
    # Job completion callback (will be set after app is created)
    job_completion_callback = None

    app = Application.builder().token(token).build()
    
    # Create job completion callback that uses app.bot
    async def notify_job_completion(
        job_id: str, chat_id: int, success: bool, message: str
    ) -> None:
        """Notify user about job completion."""
        if success:
            notification = f"✅ {message}"
        else:
            notification = f"❌ Fehler: {message}"
        
        try:
            await app.bot.send_message(
                chat_id,
                notification,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            log.error(
                "[job=%s chat=%d] Failed to send completion notification: %s",
                job_id, chat_id, e,
            )

    # Re-create worker with callback
    BACKGROUND_WORKER = BackgroundWorker(
        QUEUE_MANAGER,
        job_handlers,
        on_job_completed=notify_job_completion,
    )
    
    # Register post_init to start the background worker
    async def post_init(app: Application) -> None:
        await BACKGROUND_WORKER.start()
        # Schedule daily lunch plan message at 08:30 Europe/Berlin
        app.job_queue.run_daily(
            send_lunch_plan,
            time=datetime.time(8, 30, tzinfo=LUNCH_PLAN_TZ),
            name="lunch_plan_daily",
        )
        log.info("Lunch plan job scheduled at 08:30 Europe/Berlin")

    # Register post_stop to stop the background worker
    async def post_stop(app: Application) -> None:
        await BACKGROUND_WORKER.stop()

    app.post_init = post_init
    app.post_stop = post_stop

    user_filter = filters.User(list(ALLOWED_USER_IDS))

    app.add_handler(CommandHandler("start", cmd_start, filters=user_filter))
    app.add_handler(CommandHandler("help", cmd_start, filters=user_filter))
    app.add_handler(CommandHandler("neu", cmd_neu, filters=user_filter))
    app.add_handler(CommandHandler("lunch", cmd_lunch, filters=user_filter))
    app.add_handler(CommandHandler("queue", cmd_queue_status, filters=user_filter))
    app.add_handler(CommandHandler("backout", cmd_backout_jobs, filters=user_filter))
    app.add_handler(CommandHandler("retry", cmd_retry_job, filters=user_filter))
    app.add_handler(MessageHandler(filters.PHOTO & user_filter, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, on_text))
    app.add_handler(CallbackQueryHandler(on_button, pattern=None))

    # Reject all other users
    async def _unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.warning("Unauthorized access attempt by user_id=%s", update.effective_user and update.effective_user.id)
        if update.message:
            await update.message.reply_text("⛔ Kein Zugriff.")

    app.add_handler(MessageHandler(filters.ALL, _unauthorized))
    log.info("Starting bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
