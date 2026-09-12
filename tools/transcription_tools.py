#!/usr/bin/env python3
"""Speech-to-text transcription used by the gateway for voice messages.

Built-in providers: local (faster-whisper, default/free), local_command, groq, openai
(also serves the managed ``nous`` selection), mistral, xai, elevenlabs, deepinfra; plus
user-declared command providers and plugin providers. ``transcribe_audio(path)`` returns
``{"success", "transcript", "error"?, "provider"?}``. This module owns provider resolution,
the dispatcher and the cached local model + idle-unload state; backends live in
``transcription_{common,audio,local,cloud,command}``.
"""

import logging
import os
import shutil
import threading
import time
import importlib.util as _ilu
from pathlib import Path
from typing import Optional, Dict, Any

from utils import is_truthy_value
from hermes_cli._subprocess_compat import windows_hide_flags
from tools.transcription_common import (
    BUILTIN_STT_PROVIDERS, CLOUD_STT_PROVIDERS, DEFAULT_ELEVENLABS_STT_MODEL,
    DEFAULT_GROQ_STT_MODEL, DEFAULT_LOCAL_MODEL, DEFAULT_MISTRAL_STT_MODEL, DEFAULT_PROVIDER,
    DEFAULT_STT_MODEL, LOCAL_STT_COMMAND_ENV, LOCAL_STT_LANGUAGE_ENV, _error_result,
    _get_stt_section, _ok_result)
from tools.transcription_audio import (
    _find_binary,
    _find_ffmpeg_binary,
    _run_ffmpeg_stt_encode,
    _convert_caf_to_wav, _prepare_audio_for_transcription, _trim_silence_for_cloud_stt,
    _transcode_audio_for_stt, _validate_audio_file, _validate_audio_file_size,
    _validate_audio_source_file)
from tools.transcription_local import (
    _get_local_command_template,
    _get_idle_unload_seconds, _has_local_command, _join_confident_segments,
    _load_local_whisper_model, _looks_like_cuda_lib_error, _normalize_local_model,
    _normalize_local_model as _normalize_local_command_model, _transcribe_local_command, _try_lazy_install_stt,
    build_local_transcribe_kwargs)
# The ``_transcribe_<provider>`` handlers are looked up in this module's globals by _dispatch_stt_provider.
from tools.transcription_cloud import (  # noqa: F401  (handlers dispatched via globals())
    _extract_transcript_text,
    _has_xai_stt_credentials, _resolve_openai_audio_client_config, _transcribe_deepinfra,
    _transcribe_elevenlabs, _transcribe_groq, _transcribe_mistral, _transcribe_openai,
    _transcribe_xai)
from tools.transcription_command import (
    _apply_pre_transcription_hook, _dispatch_to_plugin_provider, _enforce_prompt_length_limit,
    _resolve_command_stt_provider_config, _transcribe_command_stt, _unregistered_stt_provider_error)

logger = logging.getLogger(__name__)


def get_env_value(name, default=None):
    """Read env values through the live config module (resolved per call: tests monkeypatch it around import)."""
    try:
        from hermes_cli.config import get_env_value as _get_env_value
    except ImportError:
        return os.getenv(name, default)
    value = _get_env_value(name)
    return default if value is None else value


def _resolve_provider_key(env_var: str, provider_id: str) -> str:
    """STT API key via the shared voice-key resolver (config > env/.env > credential pool); resolved per call."""
    try:
        from tools.tool_backend_helpers import resolve_provider_secret
    except ImportError:  # pragma: no cover — helpers are in-repo
        return str(get_env_value(env_var) or "").strip()
    return resolve_provider_secret(env_var, provider_id, env_getter=get_env_value)


def _safe_find_spec(module_name: str) -> bool:
    try:
        return _ilu.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return module_name in globals() or module_name in os.sys.modules


_HAS_FASTER_WHISPER = _safe_find_spec("faster_whisper")
_HAS_OPENAI = _safe_find_spec("openai")
_HAS_MISTRAL = _safe_find_spec("mistralai")
_HAS_PILK = _safe_find_spec("pilk")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PROVIDER = "local"
DEFAULT_LOCAL_MODEL = "base"
DEFAULT_LOCAL_STT_LANGUAGE = "en"
DEFAULT_STT_MODEL = os.getenv("STT_OPENAI_MODEL", "whisper-1")
DEFAULT_GROQ_STT_MODEL = os.getenv("STT_GROQ_MODEL", "whisper-large-v3-turbo")
DEFAULT_MISTRAL_STT_MODEL = os.getenv("STT_MISTRAL_MODEL", "voxtral-mini-latest")
DEFAULT_ELEVENLABS_STT_MODEL = os.getenv("STT_ELEVENLABS_MODEL", "scribe_v2")
LOCAL_STT_COMMAND_ENV = "HERMES_LOCAL_STT_COMMAND"
LOCAL_STT_LANGUAGE_ENV = "HERMES_LOCAL_STT_LANGUAGE"
COMMON_LOCAL_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
OPENAI_BASE_URL = os.getenv("STT_OPENAI_BASE_URL", "https://api.openai.com/v1")
XAI_STT_BASE_URL = os.getenv("XAI_STT_BASE_URL", "https://api.x.ai/v1")
ELEVENLABS_STT_BASE_URL = os.getenv("ELEVENLABS_STT_BASE_URL", "https://api.elevenlabs.io/v1")
# DeepInfra STT base URL now resolved via hermes_cli.models.deepinfra_base_url (shared).

SUPPORTED_FORMATS = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".oga", ".opus", ".aac", ".flac", ".caf"}
LOCAL_NATIVE_AUDIO_FORMATS = {".wav", ".aiff", ".aif"}
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB

# Known model sets for auto-correction
OPENAI_MODELS = {"whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe", "gpt-transcribe"}
GROQ_MODELS = {"whisper-large-v3", "whisper-large-v3-turbo", "distil-whisper-large-v3-en"}

ELEVENLABS_LANGUAGE_CODE_ALIASES = {
    "en": "eng",
    "de": "deu",
    "fr": "fra",
    "es": "spa",
    "it": "ita",
    "hu": "hun",
}

# Singleton for the local model — loaded once, reused across calls
_local_model: Optional[object] = None
_local_model_name: Optional[str] = None
# See #24767.
_local_model_lock = threading.Lock()

# Idle unload: one daemon thread releases the model (hundreds of MB of RAM/VRAM) after a
# configurable idle period, then exits; the next voice message reloads and restarts it.
# _idle_unload_mgmt_lock serializes the start check so no duplicate watchers spawn.
_last_transcription_time: float = 0.0
_idle_unload_thread: Optional[threading.Thread] = None
_idle_unload_stop = threading.Event()
_idle_unload_mgmt_lock = threading.Lock()
_IDLE_UNLOAD_CHECK_INTERVAL = 30  # seconds between idle checks

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _normalize_elevenlabs_language_code(language_code: str) -> str:
    """Normalize short language aliases without erasing specific codes."""
    code = (language_code or "").strip()
    if not code:
        return ""
    return ELEVENLABS_LANGUAGE_CODE_ALIASES.get(code.lower(), code)



# ---- Config helpers -----------------------------------------------------
def _load_stt_config() -> dict:
    """Load the ``stt`` section from user config, falling back to defaults."""
    try:
        from hermes_cli.config import load_config
        return load_config().get("stt") or {}
    except Exception:
        return {}


def is_stt_enabled(stt_config: Optional[dict] = None) -> bool:
    cfg = _load_stt_config() if stt_config is None else stt_config
    return is_truthy_value(cfg.get("enabled", True), default=True)


def _resolve_stt_language(
    provider_key: str, stt_config: Optional[Dict[str, Any]] = None, *, extra_keys: tuple = ()
) -> Optional[str]:
    """Language hint for an STT provider, first non-empty wins (never ""): ``stt.<provider>.language``
    (plus *extra_keys* aliases, e.g. ``language_code``) > ``stt.language`` > ``HERMES_LOCAL_STT_LANGUAGE``
    env > None (provider auto-detects)."""
    if stt_config is None:
        stt_config = _load_stt_config()
    provider_cfg = _get_stt_section(stt_config, provider_key)
    candidates = [provider_cfg.get(key) for key in ("language", *extra_keys)]
    if isinstance(stt_config, dict):
        candidates.append(stt_config.get("language"))
    candidates.append(os.getenv(LOCAL_STT_LANGUAGE_ENV))
    return next((c.strip() for c in candidates if isinstance(c, str) and c.strip()), None)


def _openai_audio_unavailable_reason() -> Optional[str]:
    """None when OpenAI audio has usable credentials (config, env, or managed gateway); else the reason."""
    try:
        # Resolve directly instead of via the boolean probe: the probe flattens
        # _resolve_openai_audio_client_config's selection-specific ValueError into False, so a managed
        # openai-audio gateway outage would be logged as a generic "no API key" hint (#93045).
        _resolve_openai_audio_client_config()
        return None
    except ValueError as exc:
        return str(exc)


def _has_openai_audio_backend() -> bool:
    return _openai_audio_unavailable_reason() is None


def _is_local_stt_provider(provider: str, stt_config: Dict[str, Any]) -> bool:
    """Whether *provider* is exempt from Hermes's remote upload cap."""
    return (provider or "").lower().strip() in {"local", "local_command"}


# ---- Provider resolution ------------------------------------------------
def _has_key(env_var: str, provider: str, *, needs_openai: bool = False, needs_mistral: bool = False):
    """Availability probe factory: optional SDK flag AND a resolvable API key."""
    def probe() -> bool:
        sdk_ok = (not needs_openai or _HAS_OPENAI) and (not needs_mistral or _HAS_MISTRAL)
        return sdk_ok and bool(_resolve_provider_key(env_var, provider))
    return probe


def _has_xai_stt_credentials_quietly() -> bool:
    try:
        return _has_xai_stt_credentials()
    except Exception:
        return False


def _resolve_explicit_openai() -> str:
    if not _HAS_OPENAI:
        logger.warning("STT provider 'openai' configured but no API key available")
        return "none"
    # Resolved directly so a managed openai-audio gateway outage is logged with its real reason.
    reason = _openai_audio_unavailable_reason()
    if reason is None:
        return "openai"
    logger.warning("STT provider 'openai' configured but unavailable: %s", reason)
    return "none"


def _detect_local_backend() -> Optional[str]:
    """faster-whisper > local whisper CLI > lazy-installed faster-whisper; None when nothing local works."""
    if _HAS_FASTER_WHISPER:
        return "local"
    return "local_command" if _has_local_command() else ("local" if _try_lazy_install_stt() else None)


def _resolve_explicit_local() -> str:
    backend = _detect_local_backend()
    if not backend:
        logger.warning("STT provider 'local' configured but unavailable "
                       "(install faster-whisper or set HERMES_LOCAL_STT_COMMAND)")
    return backend or "none"


def _resolve_explicit_local_command() -> str:
    if _has_local_command():
        return "local_command"
    if _HAS_FASTER_WHISPER:
        logger.info("Local STT command unavailable, using local faster-whisper")
        return "local"
    logger.warning("STT provider 'local_command' configured but unavailable")
    return "none"


_has_groq_key = _has_key("GROQ_API_KEY", "groq", needs_openai=True)
_has_mistral_key = _has_key("MISTRAL_API_KEY", "mistral", needs_mistral=True)
_has_elevenlabs_key = _has_key("ELEVENLABS_API_KEY", "elevenlabs")
_has_deepinfra_key = _has_key("DEEPINFRA_API_KEY", "deepinfra", needs_openai=True)

# Cloud providers in AUTO-DETECT priority order:
#   name -> (explicit-selection probe, auto-detect probe, explicit warning, auto-detect log)
# The probes differ only for openai (explicit has its own resolver in _EXPLICIT_RESOLVERS;
# auto-detect also requires the SDK) and xai (auto-detect must never raise). DeepInfra is
# LAST so a DEEPINFRA_API_KEY set for chat never displaces an xAI/ElevenLabs auto-selection.
# Mistral only auto-selects when the SDK is present — no lazy-install during passive
# auto-detection (explicit ``provider: mistral`` installs on first use).
_CLOUD_PROVIDER_SPECS = {
    "groq": (_has_groq_key, _has_groq_key,
             "STT provider 'groq' configured but GROQ_API_KEY not set",
             "No local STT available, using Groq Whisper API"),
    "openai": (None, lambda: _HAS_OPENAI and _has_openai_audio_backend(),
               None,
               "No local STT available, using OpenAI Whisper API"),
    "mistral": (_has_mistral_key, _has_mistral_key,
                "STT provider 'mistral' configured but mistralai package not installed or MISTRAL_API_KEY not set",
                "No local STT available, using Mistral Voxtral Transcribe API"),
    "xai": (_has_xai_stt_credentials, _has_xai_stt_credentials_quietly,
            "STT provider 'xai' configured but no xAI credentials are available",
            "No local STT available, using xAI Grok STT API"),
    "elevenlabs": (_has_elevenlabs_key, _has_elevenlabs_key,
                   "STT provider 'elevenlabs' configured but ELEVENLABS_API_KEY not set",
                   "No local STT available, using ElevenLabs Scribe STT API"),
    "deepinfra": (_has_deepinfra_key, _has_deepinfra_key,
                  "STT provider 'deepinfra' configured but DEEPINFRA_API_KEY not set (or openai package missing)",
                  "No local STT available, using DeepInfra Whisper API")}

# Explicit selections whose resolution is more than a probe + warning.
_EXPLICIT_RESOLVERS = {
    "local": _resolve_explicit_local,
    "local_command": _resolve_explicit_local_command,
    "openai": _resolve_explicit_openai}


def _resolve_explicit_provider(provider: str) -> str:
    """Explicit ``stt.provider`` -> usable name or ``"none"``; unknown names pass through untouched
    so the dispatcher fails with the provider-not-registered message."""
    resolver = _EXPLICIT_RESOLVERS.get(provider)
    if resolver is not None:
        return resolver()
    spec = _CLOUD_PROVIDER_SPECS.get(provider)
    if spec is None or spec[0]():
        return provider
    logger.warning(spec[2])
    return "none"


def _get_provider(stt_config: dict) -> str:
    """Which STT provider to use: an explicit ``stt.provider`` is honoured (no silent cloud
    fallback); otherwise auto-detect local > groq > openai > mistral > xai > elevenlabs > deepinfra."""
    if not is_stt_enabled(stt_config):
        return "none"
    explicit = "provider" in stt_config
    provider = stt_config.get("provider", DEFAULT_PROVIDER)
    # The managed "Nous Subscription" selection is the OpenAI backend routed via the managed gateway.
    if isinstance(provider, str) and provider.strip().lower() == "nous":
        provider = "openai"
    if explicit and provider == "local":
        # Legacy DEFAULT_CONFIG seeded ``stt.provider: local`` on every install, so only a
        # raw config.yaml selection counts as explicit; otherwise autodetect (local-first anyway).
        try:
            from tools.tool_backend_helpers import read_selection
            if read_selection("stt") is None:
                explicit = False
        except Exception:  # pragma: no cover — helpers are in-repo
            pass
    if explicit:
        return _resolve_explicit_provider(provider)
    backend = _detect_local_backend()
    if backend:
        return backend
    for name, (_probe, available, _warning, message) in _CLOUD_PROVIDER_SPECS.items():
        if available():
            logger.info(message)
            return name
    return "none"


# ---- Provider: local (faster-whisper) -----------------------------------
def _unload_local_model() -> None:
    """Release the cached local whisper model. Thread-safe via the model lock."""
    global _local_model, _local_model_name
    with _local_model_lock:
        if _local_model is not None:
            logger.info("Unloading local whisper model '%s' after idle timeout", _local_model_name or "unknown")
            _local_model = None
            _local_model_name = None


def _start_idle_unload_watcher(timeout_seconds: int) -> None:
    """Ensure the single idle-unload watcher thread is running. The loop re-reads
    ``stt.local.unload_after_idle_seconds`` every cycle so config edits apply within one interval;
    ``timeout_seconds`` seeds the first cycle so a just-written config is honored even if a
    concurrent read races. Exits after unloading, when the timeout becomes 0, or when the model is gone."""
    global _idle_unload_thread
    with _idle_unload_mgmt_lock:
        if _idle_unload_thread is not None and _idle_unload_thread.is_alive():
            return

        def _watch(initial_timeout=timeout_seconds):
            while not _idle_unload_stop.wait(_IDLE_UNLOAD_CHECK_INTERVAL) and _local_model is not None:
                try:
                    timeout = _get_idle_unload_seconds(_load_stt_config().get("local") or {})
                except Exception:  # noqa: BLE001 - keep the seed value
                    timeout = initial_timeout
                if timeout <= 0:
                    break  # unload disabled mid-flight — stand down
                if time.monotonic() - _last_transcription_time >= timeout:
                    _unload_local_model()
                    break
        _idle_unload_stop.clear()
        _idle_unload_thread = threading.Thread(target=_watch, name="hermes-stt-idle-unload", daemon=True)
        _idle_unload_thread.start()


def _touch_transcription_time() -> None:
    """Record transcription activity (resets the idle timer)."""
    global _last_transcription_time
    _last_transcription_time = time.monotonic()


def _get_or_load_local_model(model_name: str, local_cfg: Dict[str, Any]):
    """Cached faster-whisper model, (re)loaded under a double-checked lock when needed. The returned
    strong reference stays valid even if the idle watcher nulls the global mid-transcription."""
    global _local_model, _local_model_name
    model = _local_model
    # Lazy-load the model (downloads on first use, ~150 MB for 'base'). Double-checked lock: concurrent
    # voice messages must not both download/load the model (#24767). ``model`` is a strong local reference
    # bound under the lock: the idle watcher may null the module global at any time, but this transcription
    # keeps using the instance it grabbed.
    if model is None or _local_model_name != model_name:
        with _local_model_lock:
            if _local_model is None or _local_model_name != model_name:
                logger.info("Loading faster-whisper model '%s' (first load downloads the model)...", model_name)
                # stt.local.device / compute_type pin a configuration where ``auto`` mis-detects.
                _local_model = _load_local_whisper_model(model_name, device=local_cfg.get("device", "auto"),
                                                         compute_type=local_cfg.get("compute_type", "auto"))
                _local_model_name = model_name
            model = _local_model
    return model


def _replace_cached_model_on_cpu(model_name: str):
    """Load *model_name* on CPU/int8 and make it the cached singleton."""
    global _local_model, _local_model_name
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    with _local_model_lock:
        _local_model, _local_model_name = model, model_name
    return model


def _transcribe_local(
    file_path: str, model_name: str, *, language: Optional[str] = None, prompt: Optional[str] = None
) -> Dict[str, Any]:
    """Transcribe using faster-whisper (local, free)."""
    if not _HAS_FASTER_WHISPER and not _try_lazy_install_stt():
        return _error_result("faster-whisper not installed")
    try:
        stt_config = _load_stt_config()
        local_cfg = stt_config.get("local") or {}
        # Reset the idle timer BEFORE loading so a long in-flight transcription isn't counted as idle.
        _touch_transcription_time()
        model = _get_or_load_local_model(model_name, local_cfg)
        if model is None:  # defensive: load failed without raising
            return _error_result("Local whisper model failed to load")
        # pre_transcription hook overrides win over config-resolved values.
        transcribe_kwargs = build_local_transcribe_kwargs(stt_config)
        transcribe_kwargs.update({k: v for k, v in (("language", language), ("initial_prompt", prompt))
                                  if v})
        try:
            segments, info = model.transcribe(file_path, **transcribe_kwargs)
        except Exception as exc:
            # CUDA libs can fail at dlopen-on-first-use, AFTER loading: evict the poisoned
            # cached model, reload on CPU and retry once, else every later message fails.
            if not _looks_like_cuda_lib_error(exc):
                raise
            logger.warning("faster-whisper CUDA runtime failed mid-transcribe (%s) — "
                           "evicting cached model and retrying on CPU (int8).", exc)
            model = _replace_cached_model_on_cpu(model_name)
            segments, info = model.transcribe(file_path, **transcribe_kwargs)
        transcript = _join_confident_segments(segments, local_cfg)
        logger.info("Transcribed %s via local whisper (%s, lang=%s, %.1fs audio)",
                    Path(file_path).name, model_name, info.language, info.duration)
        _touch_transcription_time()
        idle_timeout = _get_idle_unload_seconds(local_cfg)
        if idle_timeout > 0:
            _start_idle_unload_watcher(idle_timeout)
        return _ok_result(transcript, "local")
    except Exception as e:
        logger.error("Local transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Local transcription failed: {e}"}


def _prepare_local_audio(file_path: str, work_dir: str) -> tuple[Optional[str], Optional[str]]:
    """Normalize audio for local CLI STT when needed."""
    audio_path = Path(file_path)
    if audio_path.suffix.lower() in LOCAL_NATIVE_AUDIO_FORMATS:
        return file_path, None

    ffmpeg = _find_ffmpeg_binary()
    if not ffmpeg:
        return None, "Local STT fallback requires ffmpeg for non-WAV inputs, but ffmpeg was not found"

    converted_path = os.path.join(work_dir, f"{audio_path.stem}.wav")
    command = [ffmpeg, "-y", "-i", file_path, converted_path]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300, stdin=subprocess.DEVNULL, creationflags=windows_hide_flags())
        return converted_path, None
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg conversion timed out for %s", file_path)
        return None, "Audio conversion for local STT timed out"
    except subprocess.CalledProcessError as e:
        details = e.stderr.strip() or e.stdout.strip() or str(e)
        logger.error("ffmpeg conversion failed for %s: %s", file_path, details)
        return None, f"Failed to convert audio for local STT: {details}"


def _convert_caf_to_wav(file_path: str) -> Optional[str]:
    """Convert CAF to WAV using ffmpeg or afconvert (macOS)."""
    audio_path = Path(file_path)
    wav_path = os.path.join(audio_path.parent, f"{audio_path.stem}.wav")
    ffmpeg = _find_ffmpeg_binary()
    if ffmpeg:
        try:
            subprocess.run([ffmpeg, "-y", "-i", file_path, wav_path],
                check=True, capture_output=True, text=True,
                timeout=300, stdin=subprocess.DEVNULL,
                creationflags=windows_hide_flags())
            return wav_path
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning("ffmpeg CAF to WAV failed for %s: %s", file_path, e)
    afconvert = shutil.which("afconvert")
    if afconvert:
        try:
            subprocess.run([afconvert, file_path, wav_path, "-d", "LEI16", "-f", "WAVE"],
                check=True, capture_output=True, text=True,
                timeout=300, stdin=subprocess.DEVNULL)
            return wav_path
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning("afconvert CAF to WAV failed for %s: %s", file_path, e)
    return None


def _transcribe_local_command(
    file_path: str,
    model_name: str,
    *,
    language: Optional[str] = None,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the configured local STT command template and read back a .txt transcript."""
    if prompt:
        logger.debug(
            "STT provider 'local_command' does not support transcription "
            "prompts — proceeding without the prompt."
        )

    command_template = _get_local_command_template()
    if not command_template:
        return {
            "success": False,
            "transcript": "",
            "error": (
                f"{LOCAL_STT_COMMAND_ENV} not configured and no local whisper binary was found"
            ),
        }

    # Language: hook override > stt.local.language > stt.language > env var
    # > "en" default.
    language = (
        language or _resolve_stt_language("local") or DEFAULT_LOCAL_STT_LANGUAGE
    )
    normalized_model = _normalize_local_command_model(model_name)

    try:
        with tempfile.TemporaryDirectory(prefix="hermes-local-stt-") as output_dir:
            prepared_input, prep_error = _prepare_local_audio(file_path, output_dir)
            if prep_error:
                return {"success": False, "transcript": "", "error": prep_error}

            command = command_template.format(
                input_path=shlex.quote(prepared_input),
                output_dir=shlex.quote(output_dir),
                language=shlex.quote(language),
                model=shlex.quote(normalized_model),
            )
            # Scrub Hermes secrets from the child env (sibling path to #56332 /
            # _run_command_stt — this local-whisper path previously inherited
            # the full process environment).
            from tools.environments.local import hermes_subprocess_env

            child_env = hermes_subprocess_env(inherit_credentials=False)
            subprocess.run(
                shlex.split(command),
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                stdin=subprocess.DEVNULL,
                env=child_env,
                creationflags=windows_hide_flags(),
            )

            txt_files = sorted(Path(output_dir).glob("*.txt"))
            if not txt_files:
                return {
                    "success": False,
                    "transcript": "",
                    "error": "Local STT command completed but did not produce a .txt transcript",
                }

            transcript_text = txt_files[0].read_text(encoding="utf-8").strip()
            logger.info(
                "Transcribed %s via local STT command (%s, %d chars)",
                Path(file_path).name,
                normalized_model,
                len(transcript_text),
            )
            return {"success": True, "transcript": transcript_text, "provider": "local_command"}

    except KeyError as e:
        return {
            "success": False,
            "transcript": "",
            "error": f"Invalid {LOCAL_STT_COMMAND_ENV} template, missing placeholder: {e}",
        }
    except subprocess.CalledProcessError as e:
        details = e.stderr.strip() or e.stdout.strip() or str(e)
        logger.error("Local STT command failed for %s: %s", file_path, details)
        return {"success": False, "transcript": "", "error": f"Local STT failed: {details}"}
    except Exception as e:
        logger.error("Unexpected error during local command transcription: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Local transcription failed: {e}"}

# ---------------------------------------------------------------------------
# Provider: groq (Whisper API — free tier)
# ---------------------------------------------------------------------------


def _transcribe_groq(
    file_path: str,
    model_name: str,
    *,
    language: Optional[str] = None,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcribe using Groq Whisper API (free tier available).

    Honours an optional ISO-639-1 language hint resolved from a
    ``pre_transcription`` hook override > ``stt.groq.language`` >
    ``stt.language`` (config.yaml) > ``HERMES_LOCAL_STT_LANGUAGE`` (env).
    When none is set, Groq Whisper auto-detects.
    """
    api_key = _resolve_provider_key("GROQ_API_KEY", "groq")
    if not api_key:
        return {"success": False, "transcript": "", "error": "GROQ_API_KEY not set"}

    if not _HAS_OPENAI:
        return {"success": False, "transcript": "", "error": "openai package not installed"}

    # Auto-correct model if caller passed an OpenAI-only model
    if model_name in OPENAI_MODELS:
        logger.info("Model %s not available on Groq, using %s", model_name, DEFAULT_GROQ_STT_MODEL)
        model_name = DEFAULT_GROQ_STT_MODEL

    # Language: hook override > stt.groq.language > stt.language > env.
    language = language or _resolve_stt_language("groq")

    try:
        from openai import OpenAI, APIError, APIConnectionError, APITimeoutError
        client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL, timeout=30, max_retries=0)
        try:
            create_kwargs = {
                "model": model_name,
                "response_format": "text",
            }
            if language:
                create_kwargs["language"] = language
            if prompt:
                # Only send the prompt when set so the no-hook, no-config
                # request stays byte-identical to today's.
                create_kwargs["prompt"] = prompt
            with open(file_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    file=audio_file,
                    **create_kwargs,
                )

            transcript_text = str(transcription).strip()
            logger.info("Transcribed %s via Groq API (%s, lang=%s, %d chars)",
                         Path(file_path).name, model_name, language or "auto", len(transcript_text))

            return {"success": True, "transcript": transcript_text, "provider": "groq"}
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except APIConnectionError as e:
        return {"success": False, "transcript": "", "error": f"Connection error: {e}"}
    except APITimeoutError as e:
        return {"success": False, "transcript": "", "error": f"Request timeout: {e}"}
    except APIError as e:
        return {"success": False, "transcript": "", "error": f"API error: {e}"}
    except Exception as e:
        logger.error("Groq transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Transcription failed: {e}"}

# ---------------------------------------------------------------------------
# Provider: openai (Whisper API)
# ---------------------------------------------------------------------------


def _transcribe_openai(
    file_path: str,
    model_name: str,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    provider_label: str = "openai",
    language: Optional[str] = None,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcribe via the OpenAI ``audio.transcriptions.create`` SDK shape.

    Also serves as the shared backend for every OpenAI-compatible STT
    endpoint (DeepInfra etc.) — callers pass an explicit ``api_key`` /
    ``base_url`` to skip the OpenAI-only auth chain, and a
    ``provider_label`` so the response carries the right ``provider``
    name.
    """
    if api_key is None:
        try:
            api_key, fallback_base = _resolve_openai_audio_client_config()
        except ValueError as exc:
            return {"success": False, "transcript": "", "error": str(exc)}
        base_url = base_url or fallback_base

    # Language: hook override > stt.<provider>.language > stt.language >
    # env > auto-detect. Explicit language hint improves accuracy for
    # non-English languages.
    language = language or _resolve_stt_language(provider_label)

    if not _HAS_OPENAI:
        return {"success": False, "transcript": "", "error": "openai package not installed"}

    # Auto-correct model if caller passed a Groq-only model. Only applies
    # to the native OpenAI path — third-party endpoints may legitimately
    # serve a whisper-large-v3 variant.
    if provider_label == "openai" and model_name in GROQ_MODELS:
        logger.info("Model %s not available on OpenAI, using %s", model_name, DEFAULT_STT_MODEL)
        model_name = DEFAULT_STT_MODEL

    try:
        from openai import (
            OpenAI,
            APIError,
            APIConnectionError,
            APITimeoutError,
            BadRequestError,
        )
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=30, max_retries=0)

        def _create_transcription(path: str):
            with open(path, "rb") as audio_file:
                create_kwargs = {
                    "model": model_name,
                    "file": audio_file,
                    "response_format": "text" if model_name == "whisper-1" else "json",
                }
                if language:
                    if model_name == "gpt-transcribe":
                        # gpt-transcribe replaces the singular ``language``
                        # field with a ``languages`` list; the API rejects
                        # requests that send the legacy field.
                        create_kwargs["extra_body"] = {"languages": [language]}
                    else:
                        create_kwargs["language"] = language
                    logger.debug("Using language hint '%s' for OpenAI STT", language)
                if prompt:
                    # Only send the prompt when set so the no-hook, no-config
                    # request stays byte-identical to today's.
                    create_kwargs["prompt"] = prompt
                return client.audio.transcriptions.create(**create_kwargs)

        try:
            with tempfile.TemporaryDirectory(prefix="hermes-stt-") as work_dir:
                try:
                    transcription = _create_transcription(file_path)
                except BadRequestError as exc:
                    message = str(exc).lower()
                    if not any(k in message for k in ("unsupported", "corrupted", "invalid file")):
                        raise
                    # Newer models (e.g. gpt-4o-transcribe) reject some containers
                    # whisper-1 accepted (notably Ogg/Opus voice notes). Transcode
                    # to a compact .m4a and retry once.
                    converted_path, transcode_error = _transcode_audio_for_stt(file_path, work_dir)
                    if transcode_error:
                        return {"success": False, "transcript": "", "error": transcode_error}
                    logger.info(
                        "Retrying %s STT after transcoding %s to m4a (API rejected the original container)",
                        provider_label, Path(file_path).name,
                    )
                    transcription = _create_transcription(converted_path)

            transcript_text = _extract_transcript_text(transcription)
            logger.info(
                "Transcribed %s via %s (%s, %d chars)",
                Path(file_path).name, provider_label, model_name, len(transcript_text),
            )

            return {"success": True, "transcript": transcript_text, "provider": provider_label}
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except APIConnectionError as e:
        return {"success": False, "transcript": "", "error": f"Connection error: {e}"}
    except APITimeoutError as e:
        return {"success": False, "transcript": "", "error": f"Request timeout: {e}"}
    except APIError as e:
        return {"success": False, "transcript": "", "error": f"API error: {e}"}
    except Exception as e:
        logger.error("%s transcription failed: %s", provider_label, e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Transcription failed: {e}"}

# ---------------------------------------------------------------------------
# Provider: mistral (Voxtral Transcribe API)
# ---------------------------------------------------------------------------


def _transcribe_mistral(
    file_path: str,
    model_name: str,
    *,
    language: Optional[str] = None,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcribe using Mistral Voxtral Transcribe API.

    Uses the ``mistralai`` Python SDK to call ``/v1/audio/transcriptions``.
    Requires ``MISTRAL_API_KEY`` environment variable.
    """
    api_key = _resolve_provider_key("MISTRAL_API_KEY", "mistral")
    if not api_key:
        return {"success": False, "transcript": "", "error": "MISTRAL_API_KEY not set"}

    try:
        try:
            from tools.lazy_deps import ensure as _lazy_ensure
            _lazy_ensure("stt.mistral", prompt=False)
        except Exception:
            pass
        from mistralai.client import Mistral

        with Mistral(api_key=api_key) as client:
            with open(file_path, "rb") as audio_file:
                complete_kwargs: Dict[str, Any] = {
                    "model": model_name,
                    "file": {"content": audio_file, "file_name": Path(file_path).name},
                }
                # Language: hook override > stt.mistral.language >
                # stt.language > env > auto.
                language = language or _resolve_stt_language("mistral")
                if language:
                    complete_kwargs["language"] = language
                if prompt:
                    # Only send the prompt when set so the no-hook, no-config
                    # request stays byte-identical to today's.
                    complete_kwargs["prompt"] = prompt
                result = client.audio.transcriptions.complete(**complete_kwargs)

            transcript_text = _extract_transcript_text(result)
            logger.info(
                "Transcribed %s via Mistral API (%s, %d chars)",
                Path(file_path).name, model_name, len(transcript_text),
            )
            return {"success": True, "transcript": transcript_text, "provider": "mistral"}

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except Exception as e:
        logger.error("Mistral transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Mistral transcription failed: {type(e).__name__}"}


# ---------------------------------------------------------------------------
# Provider: xAI (Grok STT API)
# ---------------------------------------------------------------------------


def _transcribe_xai(
    file_path: str,
    model_name: str,
    *,
    language: Optional[str] = None,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcribe using xAI Grok STT API.

    Uses the ``POST /v1/stt`` REST endpoint with multipart/form-data.
    Supports Inverse Text Normalization, diarization, and word-level timestamps.
    Requires ``XAI_API_KEY`` environment variable.
    """
    from tools.xai_http import resolve_xai_http_credentials

    if prompt:
        logger.debug(
            "STT provider 'xai' does not support transcription prompts — "
            "proceeding without the prompt."
        )

    # STT is an API-billed endpoint. Prefer the explicit XAI_API_KEY over the
    # general xAI OAuth/Grok-subscription credential; subscription OAuth may be
    # valid for Grok while returning personal-team spending-limit errors for
    # /v1/stt. Other xAI integrations keep their existing resolver precedence.
    direct_api_key = str(get_env_value("XAI_API_KEY") or "").strip()
    if direct_api_key:
        creds = {
            "provider": "xai",
            "api_key": direct_api_key,
            "base_url": str(
                get_env_value("XAI_BASE_URL") or "https://api.x.ai/v1"
            ).strip().rstrip("/"),
        }
    else:
        creds = resolve_xai_http_credentials()
    api_key = str(creds.get("api_key") or "").strip()
    if not api_key:
        return {
            "success": False,
            "transcript": "",
            "error": "No xAI credentials found. Configure xAI OAuth in `hermes model` or set XAI_API_KEY",
        }

    stt_config = _load_stt_config()
    xai_config = stt_config.get("xai") or {}

    def _resolve_base_url(resolved_creds: Dict[str, str]) -> str:
        # OAuth bearers are pinned to the resolver-validated xAI origin;
        # config/env base URL overrides only apply to API-key credentials.
        if resolved_creds.get("provider") == "xai-oauth":
            return str(
                resolved_creds.get("base_url") or XAI_STT_BASE_URL
            ).strip().rstrip("/")
        return str(
            xai_config.get("base_url")
            or get_env_value("XAI_STT_BASE_URL")
            or resolved_creds.get("base_url")
            or XAI_STT_BASE_URL
        ).strip().rstrip("/")

    base_url = _resolve_base_url(creds)
    # Language: hook override > stt.xai.language > stt.language > env.
    language = language or _resolve_stt_language("xai", stt_config) or ""
    # .get("format", True) already defaults to True when the key is absent;
    # is_truthy_value only normalizes truthy/falsy strings from config.
    use_format = is_truthy_value(xai_config.get("format", True))
    use_diarize = is_truthy_value(xai_config.get("diarize", False))

    try:
        import requests
        from tools.xai_http import hermes_xai_user_agent

        data: Dict[str, str] = {}
        if language:
            data["language"] = language
        if use_format:
            data["format"] = "true"
        if use_diarize:
            data["diarize"] = "true"

        def _post_transcription(bearer: str, endpoint_base_url: str):
            with open(file_path, "rb") as audio_file:
                return requests.post(
                    f"{endpoint_base_url}/stt",
                    headers={
                        "Authorization": f"Bearer {bearer}",
                        "User-Agent": hermes_xai_user_agent(),
                    },
                    files={
                        "file": (Path(file_path).name, audio_file),
                    },
                    data=data,
                    timeout=120,
                )

        response = _post_transcription(api_key, base_url)

        if (
            response.status_code in {401, 403}
            and creds.get("provider") == "xai-oauth"
        ):
            logger.info(
                "xAI STT got HTTP %d; refreshing OAuth credentials and retrying once",
                response.status_code,
            )
            try:
                refreshed_creds = resolve_xai_http_credentials(
                    force_refresh=True,
                    api_key_hint=api_key,
                )
                refreshed_key = str(refreshed_creds.get("api_key") or "").strip()
                if refreshed_key and refreshed_key != api_key:
                    response = _post_transcription(
                        refreshed_key,
                        _resolve_base_url(refreshed_creds),
                    )
            except Exception as retry_exc:
                logger.warning(
                    "xAI STT OAuth refresh-and-retry after HTTP %d failed: %s",
                    response.status_code,
                    retry_exc,
                )

        if response.status_code != 200:
            detail = ""
            try:
                err_body = response.json()
                detail = err_body.get("error", {}).get("message", "") or response.text[:300]
            except Exception:
                detail = response.text[:300]
            return {
                "success": False,
                "transcript": "",
                "error": f"xAI STT API error (HTTP {response.status_code}): {detail}",
            }

        result = response.json()
        transcript_text = result.get("text", "").strip()

        if not transcript_text:
            return {
                "success": False,
                "transcript": "",
                "error": "xAI STT returned empty transcript",
                "no_speech": True,
            }

        logger.info(
            "Transcribed %s via xAI Grok STT (lang=%s, %.1fs audio, %d chars)",
            Path(file_path).name,
            result.get("language", language),
            result.get("duration", 0),
            len(transcript_text),
        )

        return {"success": True, "transcript": transcript_text, "provider": "xai"}

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except Exception as e:
        logger.error("xAI STT transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"xAI STT transcription failed: {e}"}


# ---------------------------------------------------------------------------
# Provider: ElevenLabs (Scribe STT API)
# ---------------------------------------------------------------------------


def _transcribe_elevenlabs(
    file_path: str,
    model_name: str,
    *,
    language: Optional[str] = None,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcribe using ElevenLabs Scribe STT API."""
    if prompt:
        logger.debug(
            "STT provider 'elevenlabs' does not support transcription "
            "prompts — proceeding without the prompt."
        )

    api_key = _resolve_provider_key("ELEVENLABS_API_KEY", "elevenlabs")
    if not api_key:
        return {"success": False, "transcript": "", "error": "ELEVENLABS_API_KEY not set"}

    stt_config = _load_stt_config()
    elevenlabs_config = stt_config.get("elevenlabs") or {}
    base_url = str(
        elevenlabs_config.get("base_url")
        or get_env_value("ELEVENLABS_STT_BASE_URL")
        or ELEVENLABS_STT_BASE_URL
    ).strip().rstrip("/")
    # Language: hook override > stt.elevenlabs.language(_code) > stt.language.
    language_code = _normalize_elevenlabs_language_code(
        language or _resolve_stt_language(
            "elevenlabs", stt_config, extra_keys=("language_code",)
        ) or ""
    )
    tag_audio_events = is_truthy_value(elevenlabs_config.get("tag_audio_events", False))
    diarize = is_truthy_value(elevenlabs_config.get("diarize", False))

    try:
        import requests

        data: Dict[str, str] = {
            "model_id": model_name,
            "tag_audio_events": "true" if tag_audio_events else "false",
            "diarize": "true" if diarize else "false",
        }
        if language_code:
            data["language_code"] = language_code

        with open(file_path, "rb") as audio_file:
            response = requests.post(
                f"{base_url}/speech-to-text",
                headers={"xi-api-key": api_key},
                files={"file": (Path(file_path).name, audio_file)},
                data=data,
                timeout=120,
            )

        if response.status_code != 200:
            detail = ""
            try:
                err_body = response.json()
                error_value = err_body.get("detail") or err_body.get("error")
                if isinstance(error_value, dict):
                    detail = str(error_value.get("message") or error_value)
                elif error_value:
                    detail = str(error_value)
                else:
                    detail = response.text[:300]
            except Exception:
                detail = response.text[:300]
            return {
                "success": False,
                "transcript": "",
                "error": f"ElevenLabs STT API error (HTTP {response.status_code}): {detail}",
            }

        result = response.json()
        transcript_text = _extract_transcript_text(result)
        if not transcript_text:
            return {
                "success": False,
                "transcript": "",
                "error": "ElevenLabs STT returned empty transcript",
                "no_speech": True,
            }

        logger.info(
            "Transcribed %s via ElevenLabs Scribe (%s, %d chars)",
            Path(file_path).name,
            model_name,
            len(transcript_text),
        )

        return {"success": True, "transcript": transcript_text, "provider": "elevenlabs"}

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except Exception as e:
        logger.error("ElevenLabs STT transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"ElevenLabs STT transcription failed: {e}"}


# ---------------------------------------------------------------------------
# Provider: DeepInfra (OpenAI-compatible /v1/audio/transcriptions)
# ---------------------------------------------------------------------------


def _transcribe_deepinfra(
    file_path: str,
    model_name: str,
    *,
    language: Optional[str] = None,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve DeepInfra credentials/model, then delegate to the OpenAI handler.

    DeepInfra's STT endpoint is OpenAI-compatible, so the actual SDK
    call lives in :func:`_transcribe_openai` — this wrapper only owns
    DeepInfra-specific credential and model resolution, using the shared
    ``hermes_cli.models`` helpers so every DeepInfra surface resolves the
    base URL and model ids identically.
    """
    api_key = _resolve_provider_key("DEEPINFRA_API_KEY", "deepinfra")
    if not api_key:
        return {"success": False, "transcript": "", "error": "DEEPINFRA_API_KEY not set"}

    from hermes_cli.models import deepinfra_base_url, deepinfra_model_ids

    stt_config = _load_stt_config()
    # ``stt.deepinfra: null`` in YAML yields None, not {} — coalesce so the
    # ``.get`` calls don't raise (no stt.deepinfra block in DEFAULT_CONFIG to
    # deep-merge over the null).
    di_config = stt_config.get("deepinfra") if isinstance(stt_config, dict) else None
    if not isinstance(di_config, dict):
        di_config = {}
    base_url = deepinfra_base_url(di_config)

    if not model_name:
        candidates = deepinfra_model_ids("stt")
        if not candidates:
            return {
                "success": False,
                "transcript": "",
                "error": (
                    "No DeepInfra STT model available. Pin one in "
                    "config.yaml under stt.deepinfra.model, or check "
                    "connectivity to api.deepinfra.com so the live catalog "
                    "can be fetched."
                ),
            }
        model_name = candidates[0]

    return _transcribe_openai(
        file_path,
        model_name,
        api_key=api_key,
        base_url=base_url,
        provider_label="deepinfra",
        language=language,
        prompt=prompt,
    )


# ---------------------------------------------------------------------------
# Cloud pre-upload silence trim
# ---------------------------------------------------------------------------
#
# Local faster-whisper gets Silero VAD (build_local_transcribe_kwargs) so
# silence never reaches the model. Cloud providers get no such protection:
# the raw file is uploaded, so every second of silence is paid for twice —
# once in upload time and once in per-audio-minute billing — and cloud
# Whisper hallucinates junk tokens on silent stretches exactly like local
# Whisper did before the VAD hardening.
#
# Before uploading to a built-in cloud provider we collapse long pauses with
# ffmpeg's silenceremove filter, keeping ``stt.cloud_trim_keep_ms`` of every
# pause so word boundaries and natural pacing survive. The trim is purely
# best-effort — ANY of these falls back to uploading the original untouched:
#   - ``stt.cloud_trim_silence: false``
#   - ffmpeg or ffprobe not installed
#   - the trim command fails or times out
#   - the trimmed result is suspiciously empty (mostly-silence clip — the
#     provider, not a client-side heuristic, decides whether it has speech)
#   - the trim saves less than ~10% (re-encoding for nothing)
#
# Command-type and plugin providers are deliberately NOT trimmed: they may
# wrap local CLIs that want the original bytes (and may run their own VAD).

_CLOUD_TRIM_THRESHOLD_DB_DEFAULT = -40  # audio below this level counts as silence
_CLOUD_TRIM_KEEP_MS_DEFAULT = 300  # how much of each pause survives the trim
_CLOUD_TRIM_MIN_SAVING = 0.10  # use the trimmed file only when >=10% shorter
_CLOUD_TRIM_MIN_RESULT_SECONDS = 0.3  # all-silence guard floor: never upload ~empty audio
# Below this duration the trim can't pay for itself: a >=10% saving on a short
# clip is ~a second of audio, several providers bill a per-request minimum
# anyway (Groq: 10s), and the encode would sit on the synchronous voice-note
# response path. Skip the whole pipeline.
_CLOUD_TRIM_MIN_INPUT_SECONDS = 12.0

# Built-in providers that upload audio to a remote API.
CLOUD_STT_PROVIDERS = frozenset(BUILTIN_STT_PROVIDERS - {"local", "local_command"})


def _find_ffprobe_binary() -> Optional[str]:
    return _find_binary("ffprobe")


def _probe_audio_duration(file_path: str) -> Optional[float]:
    """Return the audio duration in seconds via ffprobe, or None.

    Canonical sync seconds-probe. ``gateway/run.py._probe_audio_duration``
    (async, returns a display string) and the Telegram adapter's
    ``_probe_voice_duration_seconds`` carry local variants of the same
    ffprobe invocation — keep the command shape in sync.
    """
    ffprobe = _find_ffprobe_binary()
    if not ffprobe:
        return None
    command = [
        ffprobe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
            stdin=subprocess.DEVNULL, creationflags=windows_hide_flags(),
        )
        return float(result.stdout.strip())
    except Exception:  # noqa: BLE001 - probe is best-effort
        return None


def _cloud_trim_settings(stt_config: Dict[str, Any]) -> tuple[bool, int, int]:
    """Resolve (enabled, threshold_db, keep_ms) for the cloud silence trim."""
    cfg = stt_config if isinstance(stt_config, dict) else {}
    # is_truthy_value: the module's established config-boolean normalizer —
    # a YAML string "false" must disable, exactly like is_stt_enabled.
    enabled = is_truthy_value(cfg.get("cloud_trim_silence", True), default=True)
    try:
        threshold_db = int(cfg.get("cloud_trim_threshold_db", _CLOUD_TRIM_THRESHOLD_DB_DEFAULT))
    except (TypeError, ValueError):
        threshold_db = _CLOUD_TRIM_THRESHOLD_DB_DEFAULT
    try:
        keep_ms = int(cfg.get("cloud_trim_keep_ms", _CLOUD_TRIM_KEEP_MS_DEFAULT))
    except (TypeError, ValueError):
        keep_ms = _CLOUD_TRIM_KEEP_MS_DEFAULT
    return enabled, threshold_db, max(keep_ms, 0)


def _trim_silence_for_cloud_stt(
    file_path: str, stt_config: Dict[str, Any]
) -> Optional[str]:
    """Return a silence-trimmed copy of *file_path* for cloud upload, or None.

    ``None`` always means "upload the original file": the trim is disabled,
    the tools are missing, the clip is too short for a trim to pay for
    itself, the trim failed, the clip is mostly silence, or trimming would
    not save enough to justify the re-encode. On success the caller owns
    deleting the returned file's parent directory.
    """
    enabled, threshold_db, keep_ms = _cloud_trim_settings(stt_config)
    if not enabled:
        return None
    ffmpeg = _find_ffmpeg_binary()
    if not ffmpeg:
        logger.debug("Cloud STT silence trim skipped: ffmpeg not found")
        return None
    original_duration = _probe_audio_duration(file_path)
    if not original_duration or original_duration <= 0:
        logger.debug("Cloud STT silence trim skipped: could not probe %s", file_path)
        return None
    if original_duration < _CLOUD_TRIM_MIN_INPUT_SECONDS:
        # Short clip: savings can't matter (some providers bill a 10s
        # minimum per request anyway) — skip the encode entirely.
        logger.debug(
            "Cloud STT silence trim skipped for %s: %.1fs is below the %.0fs gate",
            Path(file_path).name, original_duration, _CLOUD_TRIM_MIN_INPUT_SECONDS,
        )
        return None

    keep_seconds = keep_ms / 1000.0
    # start_periods=1 strips leading silence; stop_periods=-1 collapses every
    # interior/trailing silence, keeping ``keep_seconds`` of each pause.
    filter_expr = (
        f"silenceremove="
        f"start_periods=1:start_threshold={threshold_db}dB:start_silence={keep_seconds}:"
        f"stop_periods=-1:stop_threshold={threshold_db}dB:stop_silence={keep_seconds}"
    )
    work_dir = tempfile.mkdtemp(prefix="hermes-stt-trim-")
    trimmed_path = os.path.join(work_dir, f"{Path(file_path).stem or 'audio'}-trimmed.m4a")
    # Scale the all-silence guard with keep_ms: an output consisting solely
    # of kept pause must never be uploaded as "speech".
    min_result_seconds = max(_CLOUD_TRIM_MIN_RESULT_SECONDS, 2 * keep_seconds)
    keep_result = False
    try:
        _run_ffmpeg_stt_encode(ffmpeg, file_path, trimmed_path, audio_filter=filter_expr)
        trimmed_duration = _probe_audio_duration(trimmed_path)
        if not trimmed_duration or trimmed_duration < min_result_seconds:
            # Mostly/all silence. Deciding "no speech" belongs to the
            # provider, not a client-side dB heuristic — upload the original.
            logger.debug(
                "Cloud STT silence trim discarded for %s: trimmed result ~empty (%.2fs)",
                Path(file_path).name, trimmed_duration or 0.0,
            )
            return None
        if trimmed_duration > original_duration * (1 - _CLOUD_TRIM_MIN_SAVING):
            logger.debug(
                "Cloud STT silence trim discarded for %s: saves <%.0f%% (%.1fs -> %.1fs)",
                Path(file_path).name, _CLOUD_TRIM_MIN_SAVING * 100,
                original_duration, trimmed_duration,
            )
            return None
        logger.info(
            "Trimmed silence from %s before cloud STT upload (%.1fs -> %.1fs, -%d%%)",
            Path(file_path).name, original_duration, trimmed_duration,
            round((1 - trimmed_duration / original_duration) * 100),
        )
        keep_result = True
        return trimmed_path
    except Exception as exc:  # noqa: BLE001 - trim is best-effort
        logger.debug("Cloud STT silence trim failed for %s: %s", file_path, exc)
        return None
    finally:
        if not keep_result:
            shutil.rmtree(work_dir, ignore_errors=True)



# ---- Public API ---------------------------------------------------------
def _read_block_error(file_path: str) -> Optional[Dict[str, Any]]:
    """Refuse to ship a credential store (auth.json, .env, OAuth tokens) to an STT provider.
    Mirrors the image-gen / video-gen read guards."""
    from agent.file_safety import get_read_block_error
    blocked = get_read_block_error(file_path)
    return _error_result(blocked) if blocked else None


def _transcribe_prepared_audio(
    file_path: str, model: Optional[str] = None, source: Optional[str] = None) -> Dict[str, Any]:
    """Transcribe a validated audio file with the configured STT provider. ``model`` overrides the
    config default; ``source`` is a caller-surface label (``"gateway"``, ``"voice_mode"``) forwarded
    to the ``pre_transcription`` hook only."""
    # Validate before provider resolution so invalid files can't trigger provider setup
    # or lazy installation; the remote-upload size cap applies to non-local only.
    error = _read_block_error(file_path) or _validate_audio_file(file_path, enforce_size_limit=False)
    if error:
        return error
    stt_config = _load_stt_config()
    if not is_stt_enabled(stt_config):
        return _error_result("STT is disabled in config.yaml (stt.enabled: false).")
    provider = _get_provider(stt_config)
    if not _is_local_stt_provider(provider, stt_config):
        error = _validate_audio_file_size(Path(file_path))
        if error:
            return error
        # Convert CAF (iMessage voice notes) to WAV for cloud STT providers.
        if Path(file_path).suffix.lower() == ".caf":
            file_path = _convert_caf_to_wav(file_path)
            if not file_path:
                return _error_result("CAF audio could not be converted to WAV.")
    # Best-effort pre-upload silence trim for built-in cloud providers.
    trim_cleanup_dir: Optional[str] = None
    if provider in CLOUD_STT_PROVIDERS:
        trimmed = _trim_silence_for_cloud_stt(file_path, stt_config)
        if trimmed:
            file_path = trimmed
            trim_cleanup_dir = os.path.dirname(trimmed)
    try:
        return _dispatch_stt_provider(file_path, provider, stt_config, model, source)
    finally:
        if trim_cleanup_dir:
            shutil.rmtree(trim_cleanup_dir, ignore_errors=True)


# Built-in provider -> (stt section, config key, default, treat-empty-as-missing). "local_command"
# shares ``stt.local``; xAI takes no model (logging-only); deepinfra uses the live catalog when empty.
_BUILTIN_MODEL_KEYS = {
    "local": ("local", "model", DEFAULT_LOCAL_MODEL, False),
    "local_command": ("local", "model", DEFAULT_LOCAL_MODEL, False),
    "groq": ("groq", "model", DEFAULT_GROQ_STT_MODEL, True),
    "openai": ("openai", "model", DEFAULT_STT_MODEL, False),
    "mistral": ("mistral", "model", DEFAULT_MISTRAL_STT_MODEL, False),
    "elevenlabs": ("elevenlabs", "model_id", DEFAULT_ELEVENLABS_STT_MODEL, False),
    "deepinfra": ("deepinfra", "model", "", True)}


def _builtin_model_name(provider: str, stt_config: Dict[str, Any], model: Optional[str]) -> str:
    """Resolve the model for a built-in provider: caller override > ``stt.<provider>`` config > default."""
    if model:
        return model
    if provider == "xai":
        return "grok-stt"
    section, key, default, empty_is_missing = _BUILTIN_MODEL_KEYS[provider]
    cfg = _get_stt_section(stt_config, section)
    return (cfg.get(key) or default) if empty_is_missing else cfg.get(key, default)


def _dispatch_stt_provider(
    file_path: str, provider: str, stt_config: Dict[str, Any], model: Optional[str] = None,
    source: Optional[str] = None) -> Dict[str, Any]:
    """Route *file_path* to the handler for *provider* (built-in > command > plugin)."""
    # Static ``stt.prompt`` is the base; hook results mutate on top (last hook to set a field wins).
    prompt = stt_config.get("prompt")
    prompt = prompt if isinstance(prompt, str) and prompt.strip() else None
    # Fires after provider resolution and BEFORE any backend; ``language`` stays None unless a hook sets it.
    model, language, prompt = _apply_pre_transcription_hook(
        file_path=file_path, provider=provider, model=model,
        language=_get_stt_section(stt_config, provider).get("language"), prompt=prompt, source=source,
    )
    prompt = _enforce_prompt_length_limit(prompt, provider)
    if provider in BUILTIN_STT_PROVIDERS:
        # Looked up in this module at call time so tests may patch ``_transcribe_*``.
        handler = globals()[f"_transcribe_{provider}"]
        model_name = _builtin_model_name(provider, stt_config, model)
        if provider in ("local", "local_command"):
            model_name = _normalize_local_model(model_name)
        return handler(file_path, model_name, language=language, prompt=prompt)
    # Command providers: after built-ins (``stt.providers.openai.command`` can't override the
    # real handler) and BEFORE plugins, since config is more local than a plugin install.
    # User-declared command-type provider (``stt.providers.<name>: type: command``). See #17843.
    command_provider_config = _resolve_command_stt_provider_config(provider, stt_config)
    if command_provider_config is not None:
        return _transcribe_command_stt(file_path, provider, command_provider_config, stt_config,
                                       model_override=model, language_override=language, prompt=prompt)
    # Plugin backend: reads ``stt.<provider>`` like built-ins; the ``model`` argument overrides it.
    plugin_result = _dispatch_to_plugin_provider(
        file_path, provider, stt_config, model=model or _get_stt_section(stt_config, provider).get("model"),
        language=language or _resolve_stt_language(provider, stt_config), prompt=prompt)
    return plugin_result if plugin_result is not None else _no_provider_error(provider, stt_config)


def _no_provider_error(provider: str, stt_config: Dict[str, Any]) -> Dict[str, Any]:
    """Error envelope when nothing claimed *provider*: unregistered name > openai selection reason > generic hint."""
    provider_key = str(provider or "").strip().lower()
    if "provider" in stt_config and provider_key and provider_key not in BUILTIN_STT_PROVIDERS and provider_key != "none":
        return _unregistered_stt_provider_error(provider_key)
    # An explicit openai selection flattened to "none" has a specific reason (e.g. managed gateway down).
    # Surface it — with its `hermes tools` remediation — instead of the all-provider setup hint (#93045).
    if provider_key == "none" and str(stt_config.get("provider") or "") == "openai" and _HAS_OPENAI:
        reason = _openai_audio_unavailable_reason()
        if reason is not None:
            return _error_result(reason)
    return _error_result(
        "No STT provider available. Install faster-whisper for free local "
        f"transcription, configure {LOCAL_STT_COMMAND_ENV} or install a local whisper CLI, "
        "set GROQ_API_KEY for free Groq Whisper, set MISTRAL_API_KEY for Mistral "
        "Voxtral Transcribe, configure xAI OAuth or set XAI_API_KEY for xAI Grok STT, "
        "set ELEVENLABS_API_KEY for ElevenLabs Scribe, or set VOICE_TOOLS_OPENAI_KEY "
        "or OPENAI_API_KEY for the OpenAI Whisper API.")


def transcribe_audio(
    file_path: str, model: Optional[str] = None, source: Optional[str] = None) -> Dict[str, Any]:
    """Validate, preprocess supported inputs, and dispatch transcription. ``source`` is a caller-surface
    label (``"gateway"``, ``"voice_mode"``) forwarded to the ``pre_transcription`` hook only."""
    # Secret-store refusal runs before ANY validation so the error names the real reason.
    blocked = _read_block_error(file_path)
    if blocked:
        return blocked
    # Cap .silk sources before the decoder runs; for other inputs the upload cap is
    # provider-scoped in _transcribe_prepared_audio so local whisper can take big files.
    is_silk = Path(file_path).suffix.lower() == ".silk"
    source_error = _validate_audio_source_file(file_path, enforce_size_limit=is_silk)
    if source_error:
        return source_error
    prepared_path, cleanup_dir, prep_error = _prepare_audio_for_transcription(file_path)
    if prep_error or prepared_path is None:
        return prep_error or _error_result("Audio preprocessing did not produce a file for transcription.")
    try:
        return (_validate_audio_file(prepared_path, enforce_size_limit=False)
                or _transcribe_prepared_audio(prepared_path, model, source))
    finally:
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


def transcribe_audio_local_fallback(file_path: str, model: Optional[str] = None) -> Dict[str, Any]:
    """Try an already-installed local STT backend without changing config: passive inbound-media
    recovery after the configured provider failed — never lazy-installs or falls through to cloud."""
    error = _validate_audio_file(file_path)
    if error:
        return error
    local_model = model or (_load_stt_config().get("local") or {}).get("model", DEFAULT_LOCAL_MODEL)
    if _HAS_FASTER_WHISPER:
        return _transcribe_local(file_path, _normalize_local_model(local_model))
    if _has_local_command():
        return _transcribe_local_command(file_path, _normalize_local_model(local_model))
    return _error_result("No installed local STT backend is available.", provider="local")


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import platform  # noqa: F401,E402
import queue  # noqa: F401,E402
import re  # noqa: F401,E402
import shlex  # noqa: F401,E402
import subprocess  # noqa: F401,E402
import tempfile  # noqa: F401,E402
from urllib.parse import urljoin  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'COMMAND_STT_OUTPUT_FORMATS': ('tools.transcription_command', 'COMMAND_STT_OUTPUT_FORMATS'),
    'COMMON_LOCAL_BIN_DIRS': ('tools.transcription_common', 'COMMON_LOCAL_BIN_DIRS'),
    'DEFAULT_COMMAND_STT_LANGUAGE': ('tools.transcription_command', 'DEFAULT_COMMAND_STT_LANGUAGE'),
    'DEFAULT_COMMAND_STT_OUTPUT_FORMAT': ('tools.transcription_command', 'DEFAULT_COMMAND_STT_OUTPUT_FORMAT'),
    'DEFAULT_COMMAND_STT_TIMEOUT_SECONDS': ('tools.transcription_command', 'DEFAULT_COMMAND_STT_TIMEOUT_SECONDS'),
    'DEFAULT_LOCAL_STT_LANGUAGE': ('tools.transcription_common', 'DEFAULT_LOCAL_STT_LANGUAGE'),
    'ELEVENLABS_STT_BASE_URL': ('tools.transcription_common', 'ELEVENLABS_STT_BASE_URL'),
    'GROQ_BASE_URL': ('tools.transcription_common', 'GROQ_BASE_URL'),
    'GROQ_MODELS': ('tools.transcription_common', 'GROQ_MODELS'),
    'LOCAL_NATIVE_AUDIO_FORMATS': ('tools.transcription_common', 'LOCAL_NATIVE_AUDIO_FORMATS'),
    'MAX_FILE_SIZE': ('tools.transcription_common', 'MAX_FILE_SIZE'),
    'OPENAI_BASE_URL': ('tools.transcription_common', 'OPENAI_BASE_URL'),
    'OPENAI_MODELS': ('tools.transcription_common', 'OPENAI_MODELS'),
    'SUPPORTED_FORMATS': ('tools.transcription_common', 'SUPPORTED_FORMATS'),
    'XAI_STT_BASE_URL': ('tools.transcription_common', 'XAI_STT_BASE_URL'),
    'managed_nous_tools_enabled': ('tools.tool_backend_helpers', 'managed_nous_tools_enabled'),
    'nous_tool_gateway_unavailable_message': ('tools.tool_backend_helpers', 'nous_tool_gateway_unavailable_message'),
    'resolve_managed_tool_gateway': ('tools.managed_tool_gateway', 'resolve_managed_tool_gateway'),
    'resolve_openai_audio_api_key': ('tools.tool_backend_helpers', 'resolve_openai_audio_api_key'),
    'windows_hide_flags': ('hermes_cli._subprocess_compat', 'windows_hide_flags'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
