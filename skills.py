"""Strands Skills plugin factory for the dual-purpose agent.

Skills use the Strands AgentSkills API: lightweight metadata is injected into the
system prompt; full instructions are loaded on-demand when the agent activates a skill.
Both skills live as SKILL.md files under the skills/ directory.
"""

from __future__ import annotations

from pathlib import Path

from strands.vended_plugins.skills import AgentSkills

_SKILLS_DIR = Path(__file__).parent / "skills"

# ---------------------------------------------------------------------------
# Base system prompt – kept minimal; domain knowledge lives in skills
# ---------------------------------------------------------------------------

BASE_SYSTEM_PROMPT = (
    "Du bist ein persönlicher Assistent. Du kommunizierst ausschliesslich auf Deutsch.\n"
    "Wenn du eine Aufgabe nicht ausführen kannst (z.B. fehlende Bestätigung), erkläre\n"
    "kurz warum und was der Nutzer tun muss."
)


def build_skills_plugin() -> AgentSkills:
    """Create the AgentSkills plugin – loads all skills from the skills/ directory."""
    return AgentSkills(skills=[str(_SKILLS_DIR)])
