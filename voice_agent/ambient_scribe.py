"""
Optional: ambient capture through LiveKit, transformed from the Lauki Phones voice agent.

What changed from the Lauki agent
- The Lauki agent listened, thought (LangChain + Groq) and spoke back (TTS).
- An ambient scribe must stay silent, so it keeps only the "ears": LiveKit STT.
  There is no LLM or TTS in this session, and on_user_turn_completed raises
  StopResponse, so a reply is never generated.
- Every final transcript is streamed to the Voice Scribe server
  (POST /api/ambient/segments). Understanding, FHIR building and approval
  happen there, in the doctor's review screen.

Run it (with the scribe server already running: python app.py):
    python -m voice_agent.ambient_scribe console   # use this computer's mic, in the terminal
    python -m voice_agent.ambient_scribe dev       # join LiveKit rooms (e.g. a teleconsultation room)
Then open the Voice Scribe page, choose the "LiveKit" source and press Start consultation.

Needs LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET in .env (same as the Lauki agent).
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from dotenv import load_dotenv
from livekit import agents
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    StopResponse,
    TurnHandlingOptions,
    UserInputTranscribedEvent,
    inference,
)

from config import settings

_ = load_dotenv()
log = logging.getLogger("ambient-scribe")

for key, value in (
    ("LIVEKIT_URL", settings.livekit_url),
    ("LIVEKIT_API_KEY", settings.livekit_api_key),
    ("LIVEKIT_API_SECRET", settings.livekit_api_secret),
):
    if value:
        os.environ.setdefault(key, value)


class AmbientScribe(Agent):
    """Listens only. Never generates a reply."""

    def __init__(self) -> None:
        super().__init__(instructions="You are a silent ambient clinical scribe. Never speak.")

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:  # noqa: ANN001
        raise StopResponse()


server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: agents.JobContext):
    room_name = getattr(ctx.room, "name", None) or "console"
    client = httpx.AsyncClient(base_url=settings.scribe_server_url, timeout=10)
    pending: set[asyncio.Task] = set()

    session = AgentSession(
        # Same STT as the Lauki agent: Deepgram Nova-3 in "multi" mode handles Hindi-English code-switching.
        stt=inference.STT(model=settings.livekit_stt_model, language=settings.livekit_stt_language),
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
            endpointing={"mode": "fixed", "min_delay": 0.5, "max_delay": 6.0},
        ),
    )

    async def push(text: str, speaker: str | None) -> None:
        try:
            await client.post("/api/ambient/segments", json={"text": text, "speaker": speaker, "room": room_name})
        except httpx.HTTPError as exc:
            log.warning("Could not reach the scribe server at %s: %s", settings.scribe_server_url, exc)

    @session.on("user_input_transcribed")
    def _on_transcript(ev: UserInputTranscribedEvent) -> None:
        text = (ev.transcript or "").strip()
        if ev.is_final and text:
            log.info("transcript: %s", text)
            task = asyncio.create_task(push(text, None))  # roles are assigned later by the understanding step
            pending.add(task)
            task.add_done_callback(pending.discard)

    async def heartbeat() -> None:
        while True:
            try:
                await client.post("/api/ambient/heartbeat", params={"room": room_name})
            except httpx.HTTPError:
                pass
            await asyncio.sleep(10)

    hb = asyncio.create_task(heartbeat())

    async def _shutdown() -> None:
        hb.cancel()
        await client.aclose()

    ctx.add_shutdown_callback(_shutdown)

    await session.start(agent=AmbientScribe(), room=ctx.room)
    log.info("Listening. Transcripts go to %s (choose the LiveKit source in the browser).", settings.scribe_server_url)


if __name__ == "__main__":
    agents.cli.run_app(server)
