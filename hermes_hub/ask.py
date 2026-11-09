from __future__ import annotations

import json
import os
import shutil
import re

_suspicious_marker = re.compile(r"<\s*/?\s*(longcat_)?tool_?call\b", re.IGNORECASE)
import shlex
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKER_PATH = Path(__file__).resolve().with_name("ask_worker.py")


class AskError(RuntimeError):
    """Raised when ask_hermes cannot produce an answer."""


def _hermes_root() -> Path:
    raw = os.environ.get("HERMES_ROOT")
    if raw:
        path = Path(raw).expanduser()
        if (path / "hermes_cli").is_dir():
            return path
    return Path.home() / ".hermes" / "hermes-agent"


def _hermes_python() -> Path:
    raw = os.environ.get("HERMES_PYTHON")
    if raw:
        path = Path(raw).expanduser()
        if path.is_file():
            return path

    hermes_bin = None
    for candidate in (shutil.which("hermes"),):
        if candidate:
            hermes_bin = candidate
            break
    if hermes_bin is None:
        cand = Path.home() / ".local" / "bin" / "hermes"
        if cand.is_file():
            hermes_bin = str(cand)

    if hermes_bin is not None:
        try:
            out = subprocess.check_output(
                [hermes_bin, "--print-runtime-command"],
                text=True,
                timeout=30,
                stderr=subprocess.DEVNULL,
            )
            cmd = json.loads(out)
            if isinstance(cmd, list) and cmd and Path(cmd[0]).is_file():
                return Path(cmd[0])
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError, ValueError):
            pass

    import glob as _glob

    matches = sorted(_glob.glob(str(Path.home() / ".hermes" / "tools" / "python-*" / "bin" / "python3")))
    for match in reversed(matches):
        path = Path(match)
        if path.is_file():
            return path

    raise AskError("interpretador Python do Hermes não encontrado. Defina HERMES_PYTHON.")


def _default_timeout() -> int:
    try:
        return max(30, min(int(os.environ.get("HERMES_HUB_ASK_TIMEOUT", "180")), 600))
    except ValueError:
        return 180


def _max_question_chars() -> int:
    try:
        return max(100, min(int(os.environ.get("HERMES_HUB_ASK_MAX_QUESTION", "8000")), 50000))
    except ValueError:
        return 8000


def _max_answer_chars_default() -> int:
    return 20000


def _concurrency() -> int:
    try:
        return max(1, min(int(os.environ.get("HERMES_HUB_ASK_CONCURRENCY", "1")), 8))
    except ValueError:
        return 1


def _min_interval() -> float:
    try:
        return max(0.0, float(os.environ.get("HERMES_HUB_ASK_MIN_INTERVAL", "2.0")))
    except ValueError:
        return 2.0


def _is_enabled() -> bool:
    raw = os.environ.get("HERMES_HUB_ASK_DISABLED", "0").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return False
    return os.environ.get("HERMES_HUB_ASK_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _check_depth() -> None:
    try:
        depth = int(os.environ.get("HERMES_HUB_ASK_DEPTH", "0") or 0)
    except ValueError:
        depth = 0
    if depth >= 1:
        raise AskError("chamada ask_hermes recursiva bloqueada (HERMES_HUB_ASK_DEPTH)")


def _ensure_enabled() -> None:
    if not _is_enabled():
        raise AskError("ask_hermes desabilitado (HERMES_HUB_ASK_DISABLED=1)")



def _clean_answer(text: str) -> str:
    text = (text or "").strip()
    lines = [
        line for line in text.splitlines()
        if not re.match(r"^\s*session_id:\s*\S+\s*$", line, re.IGNORECASE)
    ]
    text = "\n".join(lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def redact_secrets(text: str) -> str:
    """Best-effort redaction of common secret formats."""
    if not text:
        return text
    patterns = [
        (re.compile(r"(?i)\b(api[-_ ]?key|access[-_ ]?token|auth[-_ ]?token|secret|password|passwd)\b\s*[:=]\s*\S+"), r"\1: [REDACTED]"),
        (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "[REDACTED_SK_KEY]"),
        (re.compile(r"gh[ps]_[A-Za-z0-9]{20,}"), "[REDACTED_GH_TOKEN]"),
        (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
        (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
        (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "[REDACTED_GOOGLE_KEY]"),
        (re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"), "Bearer [REDACTED]"),
        (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    ]
    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    return text


_SEMAPHORE = threading.BoundedSemaphore(_concurrency())
_LAST_CALL_LOCK = threading.Lock()
_LAST_CALL_TS = 0.0


def _acquire_rate_limit() -> None:
    global _LAST_CALL_TS
    with _LAST_CALL_LOCK:
        now = time.monotonic()
        wait = _min_interval() - (now - _LAST_CALL_TS)
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL_TS = time.monotonic()



def _invoke_worker(payload: dict[str, Any], env: dict[str, str], timeout: int):
    """Spawn the in-process Hermes worker and return (parsed_json, stderr_text, rc)."""
    python = _hermes_python()
    stdin_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    proc = subprocess.Popen(
        [str(python), "-I", str(WORKER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        stdout_bytes, stderr_bytes = proc.communicate(input=stdin_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise AskError("timeout após %d segundos" % timeout)

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

    data = None
    try:
        data = json.loads(stdout_text.strip()) if stdout_text.strip() else None
    except (json.JSONDecodeError, ValueError):
        data = None
    if data is None:
        if proc.returncode != 0:
            raise AskError(stderr_text or "hermes worker rc=%d" % proc.returncode)
        data = {"ok": True, "answer": stdout_text, "rc": 0}
    return data, stderr_text, proc.returncode


def ask_hermes_answer(
    question: str,
    max_chars: int | None = None,
    model: str | None = None,
    provider: str | None = None,
    reasoning: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Run a no-tools Hermes Q&A turn and return a structured result."""
    _ensure_enabled()
    _check_depth()

    question = (question or "").strip()
    if not question:
        raise AskError("pergunta vazia")
    if len(question) > _max_question_chars():
        raise AskError(
            "pergunta muito longa (%d chars; limite %d)" % (len(question), _max_question_chars())
        )

    if max_chars is None:
        max_chars = 8000
    max_chars = max(200, min(int(max_chars), _max_answer_chars_default()))

    if timeout is None:
        timeout = _default_timeout()
    timeout = max(30, min(int(timeout), 600))

    if not WORKER_PATH.is_file():
        raise AskError("worker não encontrado: %s" % WORKER_PATH)

    python = _hermes_python()
    payload = {
        "question": question,
        "model": model,
        "provider": provider,
        "reasoning": reasoning,
    }
    stdin_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    env = dict(os.environ)
    env["HERMES_HUB_ASK_DEPTH"] = "1"
    env["HERMES_ROOT"] = str(_hermes_root())
    env.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))
    env.setdefault("HERMES_HUB_ASK_IGNORE_RULES", "1")
    env.pop("HERMES_TUI_TOOLSETS", None)

    payload = {
        "question": question,
        "model": model,
        "provider": provider,
        "reasoning": reasoning,
    }

    start = time.monotonic()
    with _SEMAPHORE:
        _acquire_rate_limit()
        data, stderr_text, _rc = _invoke_worker(payload, env, timeout)

    elapsed_ms = int((time.monotonic() - start) * 1000)

    if not data.get("ok", False):
        msg = data.get("error") or stderr_text or "hermes worker rc=%s" % data.get("rc")
        raise AskError(str(msg))

    answer = _clean_answer(str(data.get("answer") or ""))
    answer = redact_secrets(answer)

    truncated = False
    if len(answer) > max_chars:
        answer = answer[:max_chars].rstrip()
        truncated = True

    if _suspicious_marker.search(answer):
        # Defensive: never return something that looks like a tool call.
        raise AskError("resposta continha marcador de ferramenta; abortada")

    result = {
        "answer": answer,
        "model": model,
        "elapsed_ms": elapsed_ms,
        "truncated": truncated,
        "chars": len(answer),
    }
    if stderr_text:
        result["stderr"] = stderr_text[-500:]
    return result


# Optional convenience exports

# --- Async job support for long-running MCP tool calls ----------------------
# ChatGPT's MCP connector times out on tool calls that take >~60s and returns
# "Invalid connector tool continuation". ask_hermes therefore starts the work
# in a background thread and returns immediately; the caller polls
# ask_hermes_status(job_id).

_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_JOB_TTL_SECONDS = 3600.0


def _cleanup_jobs() -> None:
    now = time.time()
    with _jobs_lock:
        expired = [jid for jid, job in _jobs.items() if now - job["created_at"] > _JOB_TTL_SECONDS]
        for jid in expired:
            _jobs.pop(jid, None)


def _run_job_background(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return
    try:
        result = ask_hermes_answer(
            job["question"],
            max_chars=job["max_chars"],
            model=job["model"],
            provider=job["provider"],
            reasoning=job["reasoning"],
            timeout=job["timeout"],
        )
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job["status"] = "done"
                job["result"] = result
                job["completed_at"] = time.time()
    except AskError as exc:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job["status"] = "error"
                job["error"] = str(exc)
                job["completed_at"] = time.time()
    except Exception as exc:  # pragma: no cover - defensive
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job["status"] = "error"
                job["error"] = "%s: %s" % (type(exc).__name__, exc)
                job["completed_at"] = time.time()


def start_ask_job(
    question: str,
    max_chars: int | None = None,
    model: str | None = None,
    provider: str | None = None,
    reasoning: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Start an async Hermes Q&A job and return immediately with a job_id."""
    _ensure_enabled()
    _check_depth()

    question = (question or "").strip()
    if not question:
        raise AskError("pergunta vazia")
    if len(question) > _max_question_chars():
        raise AskError(
            "pergunta muito longa (%d chars; limite %d)"
            % (len(question), _max_question_chars())
        )

    if max_chars is None:
        max_chars = 8000
    max_chars = max(200, min(int(max_chars), _max_answer_chars_default()))
    if timeout is None:
        timeout = _default_timeout()
    timeout = max(30, min(int(timeout), 600))

    _cleanup_jobs()
    job_id = uuid.uuid4().hex[:16]
    job = {
        "job_id": job_id,
        "question": question,
        "model": model,
        "provider": provider,
        "reasoning": reasoning,
        "max_chars": max_chars,
        "timeout": timeout,
        "status": "running",
        "created_at": time.time(),
        "result": None,
        "error": None,
        "completed_at": None,
        "depth": os.environ.get("HERMES_HUB_ASK_DEPTH", "0"),
    }

    with _jobs_lock:
        _jobs[job_id] = job

    thread = threading.Thread(target=_run_job_background, args=(job_id,), daemon=True, name="ask-job-%s" % job_id)
    thread.start()

    return {
        "job_id": job_id,
        "status": "running",
        "poll_tool": "ask_hermes_status",
        "poll_after_ms": 4000,
        "hint": "Chame ask_hermes_status(job_id) a cada ~4s ate status ser 'done' ou 'error'.",
    }


def ask_job_status(job_id: str) -> dict[str, Any]:
    """Return the current status of an async ask job."""
    _cleanup_jobs()
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise AskError("job_id não encontrado: %s" % job_id)

    status = job["status"]
    now = time.time()
    out: dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "elapsed_ms": int((now - job["created_at"]) * 1000),
    }
    if status == "running":
        out["hint"] = "Ainda em execução. Chame ask_hermes_status(job_id) novamente em ~4s."
        return out
    if status == "done" and isinstance(job.get("result"), dict):
        result = dict(job["result"])
        result["job_id"] = job_id
        result["status"] = "done"
        return result
    if status == "error":
        out["error"] = job.get("error") or "falha desconhecida"
        return out
    out["error"] = "estado desconhecido"
    return out


__all__ = ["AskError", "ask_hermes_answer", "redact_secrets", "start_ask_job", "ask_job_status"]
