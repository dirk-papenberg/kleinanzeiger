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
import uuid
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
