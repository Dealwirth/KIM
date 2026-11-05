"""Assist conversation agent: lets the user talk to KIM from Assist.

The agent supports an inline command syntax (German and English keywords) so
the user can steer the manager without leaving the chat:

  * ``plan: <goal>`` / ``plan <goal>``   -> create a plan from a goal
  * ``status`` / ``zustand``             -> report the current pool/task state
  * ``pause`` / ``pausiere``             -> pause scheduling
  * ``resume`` / ``weiter`` / ``fortsetzen``
  * ``note: <text>`` / ``notiz: <text>`` -> drop a note on the scratchpad
  * anything else                        -> submitted as a new goal
"""
from __future__ import annotations

import re
from typing import Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .runtime import KimRuntime

_STATUS = {"status", "zustand", "state"}
_PAUSE = {"pause", "pausiere", "pausieren", "stop", "stopp"}
_RESUME = {"resume", "weiter", "fortsetzen", "weiter machen", "weiter machen."}
_PLAN_PREFIX = re.compile(r"^(?:plan|plane|erstell(?:e)?\s+plan)\s*:?\s*", re.IGNORECASE)
_NOTE_PREFIX = re.compile(r"^(?:note|notiz|notiere)\s*:?\s*", re.IGNORECASE)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: KimRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([KimConversationAgent(entry, runtime)])


class KimConversationAgent(conversation.ConversationEntity):
    """KIM conversation agent exposed to Assist."""

    _attr_has_entity_name = True
    _attr_name = "KIM"

    def __init__(self, entry: ConfigEntry, runtime: KimRuntime) -> None:
        self._entry = entry
        self.runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_conversation"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "KIM KI-Manager",
            "manufacturer": "KIM",
            "model": "Agent Orchestrator",
        }

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self._entry, self)

    async def async_will_remove_from_hass(self) -> None:
        conversation.async_unset_agent(self.hass, self._entry)
        await super().async_will_remove_from_hass()

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        text = (user_input.text or "").strip()
        manager = self.runtime.manager
        assert manager is not None
        lowered = text.lower()

        if _PLAN_PREFIX.match(text):
            goal = _PLAN_PREFIX.sub("", text).strip()
            plan = await manager.submit(goal)
            response = f"Plan '{plan.title}' mit {len(plan.tasks)} Aufgaben erstellt und eingeplant."
        elif lowered in _STATUS:
            snap = manager.snapshot()
            response = (
                f"Status: {snap['worker_count']} Agenten, "
                f"{snap['running_tasks']}/{snap['max_parallel']} aktiv, "
                f"{snap['tasks_completed']} erledigt, {snap['tasks_failed']} fehlgeschlagen, "
                f"{snap['tasks_pending']} offen."
            )
        elif lowered in _PAUSE:
            manager.pause()
            response = "KIM ist pausiert."
        elif lowered in _RESUME:
            await manager.resume()
            response = "KIM laeuft wieder."
        elif _NOTE_PREFIX.match(text):
            manager.note("user", _NOTE_PREFIX.sub("", text).strip())
            response = "Notiz auf dem gemeinsamen Notizzettel gespeichert."
        elif not text:
            response = "Bitte gib ein Ziel an."
        else:
            plan = await manager.submit(text)
            response = (
                f"Verstanden. Ich habe {len(plan.tasks)} Aufgaben angelegt "
                f"und verteile sie an die Agenten."
            )

        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=user_input.agent_id, content=response)
        )
        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(response)
        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=chat_log.conversation_id,
            continue_conversation=chat_log.continue_conversation,
        )
