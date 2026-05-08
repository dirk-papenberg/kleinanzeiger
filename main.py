"""Telegram bot that turns photos into Kleinanzeigen.de drafts via Claude Vision."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from anthropic import AsyncAnthropic, AsyncAnthropicBedrock
from dotenv import load_dotenv
from PIL import Image, ImageOps

load_dotenv()

MAX_LLM_EDGE = int(os.environ.get("LLM_IMAGE_MAX_EDGE", "1568"))
LLM_JPEG_QUALITY = int(os.environ.get("LLM_IMAGE_JPEG_QUALITY", "85"))


def downscale_for_llm(img_bytes: bytes) -> bytes:
    """Resize so the longest edge is <= MAX_LLM_EDGE, re-encode as JPEG.

    Honors EXIF orientation, strips metadata, converts to RGB.
    """
    with Image.open(BytesIO(img_bytes)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        long_edge = max(im.size)
        if long_edge > MAX_LLM_EDGE:
            scale = MAX_LLM_EDGE / long_edge
            new_size = (round(im.size[0] * scale), round(im.size[1] * scale))
            im = im.resize(new_size, Image.LANCZOS)
        out = BytesIO()
        im.save(out, format="JPEG", quality=LLM_JPEG_QUALITY, optimize=True)
        return out.getvalue()
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
DEFAULT_SHIPPING_TYPE = os.environ.get("KLEINANZEIGEN_SHIPPING", "PICKUP").upper()


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
  "description": "3-6 Saetze: was ist es, Zustand, Besonderheiten, Masse/Groesse falls erkennbar. Sachlich, freundlich, ohne Uebertreibung. Kein Kontaktdaten-Geblubber, kein 'Privatverkauf'-Disclaimer (das haengt der Nutzer selbst dran).",
  "price_eur": 25,
  "price_type": "FP | VB | VHB | zu verschenken",
  "price_reasoning": "1 Satz: warum dieser Preis (z.B. 'gebraucht ca. 40% vom Neupreis ~60 EUR')",
  "missing_info": ["Liste der Dinge, die du fuer ein besseres Inserat noch wissen solltest, z.B. 'Groesse', 'Funktioniert noch?'"]
}

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
        small = await asyncio.to_thread(downscale_for_llm, img)
        log.info("photo: %d KB -> %d KB for LLM", len(img) // 1024, len(small) // 1024)
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(small).decode(),
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


def ensure_kleinanzeigen_config() -> None:
    """Write config.yaml from env vars if username/password are set in .env.

    If the user maintains their own config.yaml manually, leave it alone:
    we only generate when KLEINANZEIGEN_USERNAME is provided.
    """
    user = os.environ.get("KLEINANZEIGEN_USERNAME")
    pw = os.environ.get("KLEINANZEIGEN_PASSWORD")
    if not user:
        return
    if not pw:
        raise RuntimeError(
            "KLEINANZEIGEN_USERNAME is set but KLEINANZEIGEN_PASSWORD is missing"
        )
    cfg_path = Path(KLEINANZEIGEN_CONFIG)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        "login:\n"
        f"  username: {_yaml_escape(user)}\n"
        f"  password: {_yaml_escape(pw)}\n",
        encoding="utf-8",
    )
    try:
        cfg_path.chmod(0o600)
    except OSError:
        pass


async def run_kleinanzeigen_bot(ad_file: Path) -> tuple[int, str]:
    """Run kleinanzeigen-bot publish on the given ad. Returns (rc, combined_output)."""
    ensure_kleinanzeigen_config()
    cmd = shlex.split(KLEINANZEIGEN_BOT_CMD) + [
        "--config",
        KLEINANZEIGEN_CONFIG,
        "publish",
        "--ads",
        str(ad_file),
        "--force",
    ]
    log.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout_b, _ = await proc.communicate()
    return proc.returncode or 0, stdout_b.decode("utf-8", errors="replace")


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
    DRAFTS.pop(update.effective_chat.id, None)
    await update.message.reply_text("Ok, Entwurf verworfen. Schick neue Fotos.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-form edit request for the current draft."""
    chat_id = update.effective_chat.id
    d = DRAFTS.get(chat_id)
    if not d:
        await update.message.reply_text(
            "Kein Entwurf aktiv — schick mir erst ein Foto."
        )
        return
    user_request = (update.message.text or "").strip()
    if not user_request:
        return

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
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

    await _process_photos(chat_id, [img_bytes], context)


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
    await _process_photos(key[0], entry["photos"], context)


async def _process_photos(
    chat_id: int, photos: list[bytes], context: ContextTypes.DEFAULT_TYPE
) -> None:
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        data = await analyze_photos(photos)
    except Exception as e:
        log.exception("analyze failed")
        await context.bot.send_message(chat_id, f"Fehler bei der Analyse: {e}")
        return

    d = Draft(photos=photos)
    _apply_dict_to_draft(d, data)
    if not d.title:
        d.title = "?"
    if not d.price_type:
        d.price_type = "VB"
    DRAFTS[chat_id] = d
    await context.bot.send_message(
        chat_id,
        render_draft(d),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard(),
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    chat_id = update.effective_chat.id
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
        try:
            ad_file = write_ad_files(d)
            await context.bot.send_message(
                chat_id, f"Schalte Anzeige…\n`{ad_file}`", parse_mode=ParseMode.MARKDOWN
            )
            rc, output = await run_kleinanzeigen_bot(ad_file)
        except Exception as e:
            log.exception("publish failed")
            await context.bot.send_message(chat_id, f"Fehler beim Schalten: {e}")
            return

        tail = output[-3500:] if output else "(keine Ausgabe)"
        if rc == 0:
            await context.bot.send_message(
                chat_id,
                f"✅ Anzeige geschaltet.\n\n```\n{tail}\n```",
                parse_mode=ParseMode.MARKDOWN,
            )
            DRAFTS.pop(chat_id, None)
        else:
            await context.bot.send_message(
                chat_id,
                f"❌ Schalten fehlgeschlagen (rc={rc}).\n\n```\n{tail}\n```\n\n"
                f"Tipp: einmal manuell auf kleinanzeigen.de einloggen, oder /preis "
                f"prüfen. Du kannst weiterhin ✅ Copy-Paste nutzen.",
                parse_mode=ParseMode.MARKDOWN,
            )
    elif q.data == "cancel":
        DRAFTS.pop(chat_id, None)
        await q.edit_message_text("Verworfen.")


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")
    if LLM_PROVIDER == "bedrock":
        if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            raise SystemExit("AWS_BEARER_TOKEN_BEDROCK not set (LLM_PROVIDER=bedrock)")
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY not set")
    log.info("LLM provider=%s model=%s", LLM_PROVIDER, CLAUDE_MODEL)

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("neu", cmd_neu))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Starting bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
