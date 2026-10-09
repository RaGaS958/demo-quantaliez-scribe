"""
Central configuration for Quantaliez Voice Scribe (demo).

Every model endpoint is OpenAI-compatible. The demo points at Groq, which hosts the
same open-weight models (Whisper large-v3, gpt-oss-120b). In an NHA deployment you
change only LLM_BASE_URL / STT_BASE_URL so they point at vLLM inside the NHA-approved cloud.
"""

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_STT_PROMPT = (
    "OPD consultation in India between a doctor and a patient, in Hindi and English. "
    "Write Hindi in Roman script (Hinglish). Medical terms: bukhar, khansi, sar dard, "
    "paracetamol, metformin, amlodipine, CBC, HbA1c, BP, sugar, TDS, BD, OD, SOS."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Clinical understanding (LLM) ------------------------------------------------
    llm_api_key: str = Field("", validation_alias=AliasChoices("LLM_API_KEY", "GROQ_API_KEY"))
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"
    # gpt-oss models accept reasoning_effort (low|medium|high). Leave empty for other models.
    llm_reasoning_effort: str = "low"
    llm_timeout_s: float = 60.0

    # --- Speech-to-text ---------------------------------------------------------------
    stt_api_key: str = Field("", validation_alias=AliasChoices("STT_API_KEY", "GROQ_API_KEY"))
    stt_base_url: str = "https://api.groq.com/openai/v1"
    stt_model: str = "whisper-large-v3"
    stt_language: str = ""  # "" = auto-detect, or force e.g. "hi" / "en"
    stt_prompt: str = DEFAULT_STT_PROMPT
    stt_max_upload_mb: int = 25

    # --- Your doctor / clinic details (printed on reports). Empty = demo values ------------
    doctor_name: str = ""
    doctor_qualification: str = ""
    doctor_registration: str = ""  # e.g. state medical council registration no.
    doctor_hpr_id: str = ""  # ABDM Healthcare Professionals Registry ID
    facility_name: str = ""
    facility_address: str = ""
    facility_phone: str = ""
    facility_hfr_id: str = ""  # ABDM Health Facility Registry ID

    # --- App ------------------------------------------------------------------------------
    offline_demo: bool = False  # force offline mode (cached drafts) even if keys exist
    host: str = "127.0.0.1"
    port: int = 8000
    runtime_dir: str = "runtime"  # demo HMIS records + audit log are written here

    # --- Optional: LiveKit ambient capture (voice_agent/ambient_scribe.py) ---------------
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_stt_model: str = "deepgram/nova-3"
    livekit_stt_language: str = "multi"
    scribe_server_url: str = "http://127.0.0.1:8000"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key) and not self.offline_demo

    @property
    def stt_enabled(self) -> bool:
        return bool(self.stt_api_key) and not self.offline_demo


settings = Settings()
