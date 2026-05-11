"""Agent registry: one strands.Agent per Telegram chat_id.

Agents are lazily created and cached. Each agent gets its own FileSessionManager
so conversation history survives container restarts.

Usage:
    from agent_registry import get_agent, clear_agent

    agent = get_agent(chat_id)
    result = await asyncio.to_thread(agent, user_message)
    response_text = result.message  # or str(result)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager

from skills import BASE_SYSTEM_PROMPT, build_skills_plugin
from tools import get_current_date, get_recipes, get_lunch_plan, save_lunch_plan, publish_kleinanzeigen_ad

log = logging.getLogger("kleinanzeigen-agent.registry")

SESSION_DIR = Path(
    os.environ.get("KLEINANZEIGEN_SESSION_DIR", "/data/sessions")
)

_TOOLS = [get_current_date, get_recipes, get_lunch_plan, save_lunch_plan, publish_kleinanzeigen_ad]

# chat_id -> Agent
_agents: dict[int, Agent] = {}


def _make_model():
    """Create a Strands model based on the active LLM provider."""
    provider = os.environ.get("LLM_PROVIDER", "").lower()
    if not provider:
        provider = "bedrock" if os.environ.get("AWS_BEARER_TOKEN_BEDROCK") else "anthropic"

    if provider == "bedrock":
        from strands.models import BedrockModel
        model_id = os.environ.get(
            "CLAUDE_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        )
        region = os.environ.get("AWS_REGION", "us-east-1")
        return BedrockModel(model_id=model_id, region_name=region)

    from strands.models.anthropic import AnthropicModel
    model_id = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    return AnthropicModel(model_id=model_id, api_key=api_key)


def get_agent(chat_id: int) -> Agent:
    """Return the Agent for *chat_id*, creating it if necessary."""
    if chat_id not in _agents:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        session_dir = SESSION_DIR / str(chat_id)
        session_dir.mkdir(parents=True, exist_ok=True)

        try:
            from strands.agent.session import FileSessionManager
            session_manager = FileSessionManager(
                session_id=str(chat_id),
                base_dir=str(SESSION_DIR),
            )
        except Exception:
            # Session manager unavailable – proceed without persistence
            log.warning(
                "[chat=%d] FileSessionManager unavailable, using in-memory session",
                chat_id,
            )
            session_manager = None

        kwargs: dict = dict(
            model=_make_model(),
            system_prompt=BASE_SYSTEM_PROMPT,
            plugins=[build_skills_plugin()],
            tools=_TOOLS,
            conversation_manager=SlidingWindowConversationManager(window_size=50),
        )
        if session_manager is not None:
            kwargs["session_manager"] = session_manager

        agent = Agent(**kwargs)
        _agents[chat_id] = agent
        log.info("[chat=%d] Agent created (session_dir=%s)", chat_id, session_dir)

    return _agents[chat_id]


def clear_agent(chat_id: int) -> None:
    """Remove the agent for *chat_id* from the cache.

    The next call to get_agent() will create a fresh agent (session history on
    disk is NOT deleted – only the in-memory instance is cleared).
    """
    if chat_id in _agents:
        del _agents[chat_id]
        log.info("[chat=%d] Agent cleared from registry", chat_id)
