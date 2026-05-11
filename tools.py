"""Strands @tool definitions for the agent.

All tools are synchronous so they work correctly when the Strands agent is
called via asyncio.to_thread() from the Telegram asyncio event loop.
The save_lunch_plan tool is gated: it requires agent.state["plan_confirmed"] == True,
which is set explicitly by the Telegram confirmation handler before re-invoking the agent.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from strands import tool

log = logging.getLogger("kleinanzeigen-agent.tools")

_TZ = ZoneInfo("Europe/Berlin")

LUNCH_PLAN_BASE_URL = os.environ.get(
    "LUNCH_PLAN_URL",
    "http://ubuntu.fritz.box:880/resources/plan",
)
RECIPES_URL = os.environ.get(
    "LUNCH_RECIPES_URL",
    "http://ubuntu.fritz.box:880/resources/recipes",
)
_DEFAULT_SKILL_ADDONS_DIR = "~/.kleinanzeigen-agent/skills"
_NO_ADDITIONAL_LUNCH_RULES = "Noch keine zusätzlichen Regeln."

_DEFAULT_LUNCH_PLANNING_SKILL_CONTENT = """# Anpassbare Mittagessen-Regeln

## Tagesansage
- Rufe zuerst get_current_date auf, um das heutige Datum zu kennen, bevor du Pläne abrufst oder erstellst.
- Jeden Morgen um 08:30 Uhr erhältst du eine Aufgabe, das heutige Mittagessen anzukündigen. Formatiere es übersichtlich mit Emoji und Namen der Gerichte.

## Wochentag-Regeln
- Dienstags kocht Mama. Plane für Dienstag niemals ein Rezept ein und speichere auch keinen Eintrag. Weise in der Vorschlagsliste explizit darauf hin: "Dienstag: Mama kocht 👩‍🍳"

## Wochenplanung
- Wenn kein Mittagessen für morgen geplant ist, erstelle einen Vorschlag für die fehlenden Tage der nächsten Woche.
- Überschreibe keine bereits geplanten Tage.
- Rufe get_lunch_plan einmalig auf: startDate = heute minus 84 Tage, endDate = Ende der nächsten Woche (nächster Sonntag).
- Rufe get_recipes auf, um alle verfügbaren Rezepte zu laden.
- Wähle Rezepte mit diesen Prioritäten: Abwechslung, Vielfalt über Kategorien, versteckte Rezepte nur wenn nötig, saisonale Zutaten als optionaler Tiebreaker.
- Vermeide Gerichte aus den letzten 2 Wochen möglichst; Gerichte aus den letzten 4 Wochen nur wenn nötig.
- Präsentiere Vorschläge als übersichtliche Liste mit Datum und Rezeptname.
- Speichere niemals ohne explizite Bestätigung des Nutzers.

## Speichern
- Übergib save_lunch_plan im Feld recipes eine Liste von Objekten mit mindestens {"id": <rezept-id>}.
- Übergib keine vollständigen Rezeptobjekte.
- Gib nach dem Speichern eine Zusammenfassung aus.

## Nutzerregeln
- {NO_ADDITIONAL_LUNCH_RULES}
""".replace("{NO_ADDITIONAL_LUNCH_RULES}", _NO_ADDITIONAL_LUNCH_RULES)


def _validate_skill_name(skill_name: str) -> str:
    normalized = (skill_name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", normalized):
        raise ValueError("skill_name darf nur Kleinbuchstaben, Zahlen, '-' und '_' enthalten.")
    return normalized


def _skill_addons_dir() -> Path:
    return Path(
        os.environ.get(
            "KLEINANZEIGEN_SKILL_ADDONS_DIR",
            _DEFAULT_SKILL_ADDONS_DIR,
        )
    ).expanduser()


def _skill_addon_path(skill_name: str) -> Path:
    normalized = _validate_skill_name(skill_name)
    if normalized == "lunch-planning":
        legacy_path = os.environ.get("LUNCH_PLANNING_SKILL_MEMORY_PATH")
        if legacy_path:
            return Path(legacy_path).expanduser()
    return _skill_addons_dir() / f"{normalized}.md"


def _default_skill_addon_content(skill_name: str) -> str:
    if skill_name == "lunch-planning":
        return _DEFAULT_LUNCH_PLANNING_SKILL_CONTENT
    return (
        f"# Anpassbare Regeln für {skill_name}\n\n"
        "## Nutzerregeln\n"
        "- Noch keine zusätzlichen Regeln.\n"
    )


def _ensure_skill_addon(skill_name: str) -> Path:
    normalized = _validate_skill_name(skill_name)
    path = _skill_addon_path(normalized)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_default_skill_addon_content(normalized), encoding="utf-8")
    return path


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep temporary files hidden so the external skill directory stays tidy.
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _normalize_rule_text(text: str) -> str:
    # Strip Markdown list markers so semantically identical rules deduplicate.
    normalized = re.sub(r"^(?:[-*]\s*)+", "", text.strip())
    return " ".join(normalized.split()).casefold()


# ---------------------------------------------------------------------------
# Date / time
# ---------------------------------------------------------------------------

@tool
def get_current_date() -> dict:
    """Return the current date and day-of-week in the Europe/Berlin timezone.

    Always call this tool first whenever the task involves dates (e.g. fetching
    or planning meals), so you know today's actual date rather than guessing.

    Returns a dict with:
    - date: ISO date string, e.g. "2026-05-11"
    - weekday: German weekday name, e.g. "Montag"
    - weekday_en: English weekday name, e.g. "Monday"
    """
    now = datetime.datetime.now(_TZ)
    weekdays_de = [
        "Montag", "Dienstag", "Mittwoch", "Donnerstag",
        "Freitag", "Samstag", "Sonntag",
    ]
    return {
        "date": now.date().isoformat(),
        "weekday": weekdays_de[now.weekday()],
        "weekday_en": now.strftime("%A"),
    }


def _read_skill_addon(skill_name: str) -> str:
    try:
        path = _ensure_skill_addon(skill_name)
    except ValueError as e:
        return f"Fehler: {e}"
    return path.read_text(encoding="utf-8")


def _update_skill_addon(skill_name: str, content: str, mode: str = "append") -> str:
    try:
        normalized_skill = _validate_skill_name(skill_name)
        path = _ensure_skill_addon(normalized_skill)
    except ValueError as e:
        return f"Fehler: {e}"

    normalized_mode = (mode or "append").strip().lower()
    normalized_content = (content or "").strip()
    if not normalized_content:
        return "Fehler: Keine Regel angegeben."

    if normalized_mode == "replace":
        _write_text_atomic(path, normalized_content.rstrip() + "\n")
        return f"Skill-Add-on für {normalized_skill} wurde ersetzt ({path})."

    if normalized_mode != "append":
        return "Fehler: mode muss 'append' oder 'replace' sein."

    current = path.read_text(encoding="utf-8").rstrip()
    bullet = normalized_content
    if not bullet.startswith(("-", "*")):
        bullet = f"- {bullet}"

    marker = "## Nutzerregeln"
    if marker in current:
        before, after = current.split(marker, 1)
        after_content = after.strip()
        if _normalize_rule_text(after_content) == _normalize_rule_text(
            _NO_ADDITIONAL_LUNCH_RULES
        ):
            updated_after = bullet
        else:
            updated_after = after_content
            existing_rules = {
                _normalize_rule_text(line)
                for line in updated_after.splitlines()
                if line.strip()
            }
            if _normalize_rule_text(bullet) not in existing_rules:
                updated_after = f"{updated_after}\n{bullet}"
        updated = f"{before.rstrip()}\n\n{marker}\n{updated_after}\n"
    else:
        updated = f"{current}\n\n{marker}\n{bullet}\n"

    _write_text_atomic(path, updated)
    return f"Skill-Add-on-Regel für {normalized_skill} gespeichert ({path})."


@tool
def get_skill_addon(skill_name: str) -> str:
    """Read persistent add-on instructions for a skill.

    Use this before working on a skill-specific task when durable user
    preferences may apply. For example, call with skill_name="lunch-planning"
    before lunch planning or skill_name="kleinanzeigen" before drafting ads.
    """
    return _read_skill_addon(skill_name)


@tool
def update_skill_addon(skill_name: str, content: str, mode: str = "append") -> str:
    """Edit persistent add-on instructions for a skill.

    Use this when the user states a durable preference or rule for any skill.
    Prefer mode="append" for new rules. Use mode="replace" only after reading the
    current add-on with get_skill_addon and preserving all still-valid rules in
    the replacement content.

    Args:
        skill_name: Skill identifier, e.g. "lunch-planning" or "kleinanzeigen".
        content: New rule text for append mode, or the full file content for replace mode.
        mode: "append" to add a bullet under Nutzerregeln, or "replace" to rewrite the file.
    """
    return _update_skill_addon(skill_name, content, mode)


@tool
def get_lunch_planning_skill() -> str:
    """Read the mutable lunch-planning skill add-on.

    Compatibility wrapper for get_skill_addon("lunch-planning").
    """
    return _read_skill_addon("lunch-planning")


@tool
def update_lunch_planning_skill(content: str, mode: str = "append") -> str:
    """Edit the mutable lunch-planning skill add-on.

    Compatibility wrapper for update_skill_addon("lunch-planning", ...).
    """
    return _update_skill_addon("lunch-planning", content, mode)


def _fetch_all_recipes() -> list[dict]:
    """Internal helper – returns full recipe objects including hidden ones."""
    with httpx.Client(timeout=15) as client:
        resp = client.get(RECIPES_URL, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()


@tool
def get_recipes() -> list[dict]:
    """Fetch all available (non-hidden) recipes from the recipe API.

    Hidden recipes are excluded. Returns a slim list; each object contains:
    - id: unique recipe identifier
    - name: recipe name
    - category: e.g. "Hauptgericht", "Suppe"
    - lastPlanDate: ISO date string of when it was last planned (or null)
    """
    keep_keys = {"id", "name", "category", "lastPlanDate"}
    return [
        {k: v for k, v in recipe.items() if k in keep_keys}
        for recipe in _fetch_all_recipes()
        if not recipe.get("hide", False)
    ]


@tool
def get_lunch_plan(start_date: str, end_date: str) -> list[dict]:
    """Fetch the meal plan for a date range from the plan API.

    Args:
        start_date: ISO date string, e.g. "2026-05-12"
        end_date:   ISO date string, e.g. "2026-05-18"

    Returns a slim list of day-entries. Each entry contains:
    - date: ISO date string
    - recipes: list of {id, name} for each recipe planned that day (may be empty)
    """
    url = f"{LUNCH_PLAN_BASE_URL}?startDate={start_date}&endDate={end_date}"
    with httpx.Client(timeout=15) as client:
        resp = client.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    entries = data if isinstance(data, list) else [data]
    return [
        {
            "date": entry.get("date"),
            "recipes": [
                {"id": r.get("id"), "name": r.get("name")}
                for r in entry.get("recipes", [])
            ],
        }
        for entry in entries
    ]


@tool
def save_lunch_plan(date: str, recipes: list[dict], **kwargs) -> str:
    """Save a meal plan entry for a specific date to the plan API.

    IMPORTANT: This tool must only be called after the user has explicitly confirmed
    the plan. The confirmation is tracked in agent.state["plan_confirmed"]. If
    confirmation is missing, this tool returns an error message instead of saving.

    Args:
        date:    ISO date string, e.g. "2026-05-12"
        recipes: list of recipe references – each must contain at least {"id": <id>}.
                 The tool resolves the full recipe objects internally before saving.

    Returns a success message or an error description.
    """
    # Check agent state for confirmation flag
    agent_context = kwargs.get("agent_context") or kwargs.get("context")
    if agent_context is not None:
        agent_state = getattr(agent_context, "state", None)
        if agent_state is not None and not agent_state.get("plan_confirmed", False):
            return (
                "Fehler: Der Plan wurde noch nicht vom Nutzer bestätigt. "
                "Bitte warte auf eine ausdrückliche Bestätigung (z.B. '✅ Annehmen'), "
                "bevor du den Plan speicherst."
            )

    # Resolve full recipe objects by ID (includes hidden recipes)
    all_recipes = {r["id"]: r for r in _fetch_all_recipes()}
    resolved = []
    for ref in recipes:
        rid = ref.get("id")
        if rid is None:
            return f"Fehler: Rezept-Referenz ohne ID: {ref}"
        full = all_recipes.get(rid)
        if full is None:
            return f"Fehler: Rezept mit ID {rid} nicht gefunden."
        resolved.append(full)

    payload = {"date": date, "recipes": resolved}
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            LUNCH_PLAN_BASE_URL,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        resp.raise_for_status()

    log.info("Saved lunch plan for %s", date)
    return f"Plan für {date} erfolgreich gespeichert."


# ---------------------------------------------------------------------------
# Kleinanzeigen publish tool
# ---------------------------------------------------------------------------

_queue_enqueue_fn = None


def set_queue_enqueue_fn(fn) -> None:
    """Register the queue enqueue function from main.py."""
    global _queue_enqueue_fn
    _queue_enqueue_fn = fn


@tool
def publish_kleinanzeigen_ad(ad_yaml_path: str, chat_id: int) -> str:
    """Queue a Kleinanzeigen.de ad for publishing via the background worker.

    Args:
        ad_yaml_path: Absolute path to the ad.yaml file prepared for this listing.
        chat_id:      Telegram chat ID of the user who owns this ad.

    Returns a confirmation string with the job ID, or an error message.
    """
    if _queue_enqueue_fn is None:
        return "Fehler: Warteschlange nicht initialisiert."

    job_id = str(uuid.uuid4())
    _queue_enqueue_fn(
        job_id=job_id,
        chat_id=chat_id,
        job_type="publish_ad",
        data={"ad_file": ad_yaml_path, "chat_id": chat_id},
    )
    log.info("[chat=%d job=%s] Publish job queued via tool", chat_id, job_id)
    return f"Anzeige wird geschaltet (Job-ID: {job_id[:8]})."
