"""Interactive CLI for testing the agent without Telegram.

Usage:
    python test_agent.py [--chat-id 12345]

Type messages and press Enter. The agent responds in the terminal.
Special commands:
    /neu    — clear agent session (same as the Telegram /neu command)
    /tools  — call tools directly for quick API sanity checks
    /quit   — exit
"""

from __future__ import annotations

import argparse
import logging
import sys

# ---------------------------------------------------------------------------
# Logging – set AGENT_LOG_LEVEL=DEBUG to see full LLM requests/responses
# ---------------------------------------------------------------------------
import os

_log_level = os.environ.get("AGENT_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(
    level=logging.WARNING,
    format="%(name)s %(levelname)s %(message)s",
)
logging.getLogger("strands").setLevel(_log_level)
logging.getLogger("kleinanzeigen-agent").setLevel(logging.DEBUG)

from dotenv import load_dotenv

load_dotenv()

# Fake a queue enqueue fn so the publish tool doesn't crash
from tools import set_queue_enqueue_fn

def _fake_enqueue(**kwargs):
    print(f"  [queue] enqueue called: job_type={kwargs.get('job_type')} data={kwargs.get('data')}")

set_queue_enqueue_fn(_fake_enqueue)

from agent_registry import get_agent, clear_agent


def _run_tool_sanity(chat_id: int) -> None:
    """Call each tool directly and print the result."""
    import datetime
    from tools import get_recipes, get_lunch_plan

    print("\n--- get_recipes() ---")
    try:
        recipes = get_recipes()
        print(f"  {len(recipes)} recipes returned")
        if recipes:
            r = recipes[0]
            print(f"  First: {r.get('name')} (lastPlanDate={r.get('lastPlanDate')})")
    except Exception as e:
        print(f"  ERROR: {e}")

    today = datetime.date.today().isoformat()
    week = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    print(f"\n--- get_lunch_plan({today}, {week}) ---")
    try:
        plan = get_lunch_plan(today, week)
        print(f"  {len(plan)} day-entries returned")
        for entry in plan:
            recipes_count = len(entry.get("recipes") or [])
            print(f"  {entry.get('date')}: {recipes_count} recipe(s)")
    except Exception as e:
        print(f"  ERROR: {e}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Test the Strands agent interactively.")
    parser.add_argument("--chat-id", type=int, default=1, help="Fake Telegram chat ID")
    args = parser.parse_args()

    chat_id = args.chat_id
    print(f"Agent REPL (chat_id={chat_id}). Type /neu, /tools, or /quit.")
    print("─" * 60)

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            sys.exit(0)

        if not user_input:
            continue

        if user_input == "/quit":
            print("Bye.")
            sys.exit(0)

        if user_input == "/neu":
            clear_agent(chat_id)
            print("Agent session cleared.")
            continue

        if user_input == "/tools":
            _run_tool_sanity(chat_id)
            continue

        agent = get_agent(chat_id)
        try:
            result = agent(user_input)
            print(f"\nAgent: {result}")
        except Exception as e:
            print(f"\nERROR: {e}")


if __name__ == "__main__":
    main()
