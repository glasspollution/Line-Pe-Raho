"""All configuration, thresholds and timeouts (CLAUDE.md §17, §21). Nothing tunable lives inline elsewhere.

Env var names are the upper-cased field names (pydantic-settings), read from `.env` at the repo root.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # --- Gnani (§7) ---
    gnani_api_key: SecretStr = SecretStr("")
    gnani_base_url: str = "https://api.vachana.ai"
    stt_lang_default: str = "en-IN"
    tts_model: str = "timbre-v3.0"
    tts_agent_voice: str = "Shivani"
    tts_ivr_voice: str = "Riyaa"
    tts_company_agent_voice: str = "Avinash"  # auto-agent (company employee) in sim/live runs
    tts_sim_user_voice: str = "Shubham"  # simulated user in live runs (a real user speaks for themselves)

    # --- LLM (§0.2, §9.4): Kimi K3 on Amazon Bedrock by default (D-4); any OpenAI-compatible endpoint works ---
    llm_base_url: str = "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"
    llm_model: str = "us.moonshotai.kimi-k3"
    llm_auth: Literal["api_key", "bedrock_profile"] = "bedrock_profile"
    llm_api_key: SecretStr = SecretStr("")  # used when llm_auth=api_key
    aws_profile: str = ""  # used when llm_auth=bedrock_profile; set in .env, never committed
    aws_region: str = "us-east-1"
    llm_token_ttl_s: int = 3600
    llm_fallback_base_url: str = ""
    llm_fallback_model: str = ""
    llm_temperature: float = 0.6
    llm_top_p: float = 0.95
    llm_timeout_realtime_s: float = 4.0
    llm_timeout_postcall_s: float = 60.0
    llm_max_tokens_realtime: int = 400  # always explicit: Bedrock reserves quota for the max when unset
    llm_max_tokens_postcall: int = 3000
    llm_reasoning_effort_realtime: str = "none"  # Kimi K3 defaults to "max"; "" = don't send the field
    llm_reasoning_effort_postcall: str = "low"
    llm_reasoning_effort_plan: str = "none"  # the user waits on planning; "low" took 8-20 s
    llm_max_tokens_plan: int = 800
    llm_timeout_plan_s: float = 15.0

    # --- Line / runtime (§6, §17) ---
    line_kind: Literal["virtual", "exotel"] = "virtual"
    data_dir: Path = REPO_ROOT / "data"
    hold_stt_mode: Literal["stream_all", "gated"] = "stream_all"
    agent_speaks_enabled: bool = False

    # --- Retry policy (§7.5) ---
    retry_base_s: float = 0.5
    retry_max_s: float = 8.0
    retry_max_tries: int = 5

    # --- Realtime STT (§7.1) ---
    stt_vad_threshold: float = 0.7
    stt_min_speech_ms: int = 200
    stt_min_silence_ms: int = 500
    stt_min_segment_ms: int = 120
    stt_max_speech_s: int = 20
    stt_session_rotate_s: float = 14 * 60
    stt_session_overlap_s: float = 2.0

    # --- Orchestrator limits (§8) ---
    dial_timeout_s: float = 45.0
    max_ivr_s: float = 6 * 60
    max_hold_min: float = 45.0
    max_call_s: float = 60 * 60
    max_ivr_depth: int = 8
    same_menu_max_repeats: int = 3
    fallback_keys: str = "0*9"
    probe_response_window_s: float = 4.0
    keepalive_every_s: float = 12.0
    keepalive_max: int = 6
    user_join_timeout_s: float = 90.0

    # --- Menu parsing / IVR decisions (§9.2, §9.3) ---
    menu_complete_silence_s: float = 2.5
    decider_fuzzy_min: int = 85
    decider_early_press_min: int = 95
    decider_llm_min_confidence: float = 0.6

    # --- Human detection (§10.2) ---
    human_confirm_threshold: float = 0.85
    human_probe_threshold: float = 0.5
    human_novelty_fuzzy_min: int = 80
    human_turn_silence_s: float = 1.2
    human_end_slack_s: float = 0.8  # STT end-of-speech estimates run early on real audio (live run 2026-10-20)
    hold_silence_probe_s: float = 3.0  # music stopped + this much silence in HOLD -> probe (silent agent)
    stt_oracle_latency_s: float = 0.3  # simulated Prisma latency after end-of-speech (sim only)
    human_w_greeting: float = 0.35
    human_w_novel: float = 0.25
    human_w_turn_taking: float = 0.20
    human_w_no_music_before: float = 0.10
    human_w_after_connecting: float = 0.10
    human_w_announcement: float = -0.5
    human_w_music_under: float = -0.3

    # --- Audio classifier (§10.1) ---
    classifier_window_ms: int = 500
    music_min_dbfs: float = -45.0
    music_min_duration_s: float = 2.0
    music_max_speech_ratio: float = 0.3
    vad_mode: int = 2
    classifier_silence_dbfs: float = -50.0
    classifier_tone_min_top2: float = 0.75  # energy share of the two strongest spectral peaks
    classifier_speech_min_flux_var: float = 0.006  # tuned 2026-10-20 on synthetic+Timbre audio, clean..snr5
    classifier_noise_min_flatness: float = 0.35
    classifier_sustain_windows: int = 4  # music must hold for this many 500 ms windows ...
    classifier_sustain_min_music: int = 3  # ... with at least this many labelled music
    gate_reopen_speech_ratio: float = 0.6
    gate_reopen_ms: int = 300
    gate_preroll_s: float = 1.5

    # --- DTMF (§6.4) ---
    dtmf_tone_ms: int = 120
    dtmf_gap_ms: int = 80
    dtmf_level_dbfs: float = -8.0
    dtmf_min_ms: int = 60
    dtmf_max_twist_db: float = 8.0

    # --- Mock company simulator (§11) ---
    ivr_answer_after_s: float = 3.0  # ringback before the IVR picks up
    ivr_default_timeout_s: float = 8.0
    ivr_default_max_repeats: int = 2
    collect_interdigit_timeout_s: float = 6.0
    ask_max_s: float = 8.0
    ask_end_silence_s: float = 1.0
    ask_speech_dbfs: float = -40.0

    # --- API (§14) ---
    api_use_llm: bool = True  # plan via the LLM (and menus/summary in live calls); false = rules only
    transcribe_max_s: float = 30.0

    # --- Post-call (§12) ---
    verify_quote_fuzzy_min: int = 90
    itn_pad_s: float = 0.3
    clip_pad_s: float = 0.5
    batch_poll_s: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
