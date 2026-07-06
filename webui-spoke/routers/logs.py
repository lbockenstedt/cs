"""Logs API routes (moved verbatim from server.py; logic imported from server)."""
from __future__ import annotations

from fastapi import APIRouter
from server import (
    Any,
    HTTPException,
    INSTALL_LOG_PATH,
    JOURNAL_UNIT,
    LOG_STREAM_KEEPALIVE_SECS,
    LOG_STREAM_POLL_SECS,
    PlainTextResponse,
    Query,
    StreamingResponse,
    WATCHDOG_LOG_PATH,
    _encode_sse_line,
    _log_source_hint,
    _normalize_log_source,
    _stream_keepalive,
    asyncio,
    contextlib,
    iso_utcnow,
    proxmox_log_buffer,
    subprocess,
    time,
)

router = APIRouter()




@router.get("/api/logs/service")
def api_service_logs(lines: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    timestamp = iso_utcnow()
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                JOURNAL_UNIT,
                "-n",
                str(lines),
                "--no-pager",
                "--output=short",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            log_lines = result.stdout.splitlines()
            return {"lines": log_lines, "count": len(log_lines), "timestamp": timestamp}
    except Exception:
        pass

    fallback = ["journalctl not available"]
    return {"lines": fallback, "count": len(fallback), "timestamp": timestamp}




@router.get("/api/logs/history")
async def api_logs_history(
    lines: int = Query(default=300, ge=10, le=2000),
    source: str = Query(default="journal"),
):
    """Return the last N lines from the selected log source."""
    source = _normalize_log_source(source)
    try:
        if source == "agent":
            log_lines = proxmox_log_buffer[-lines:]
            if not log_lines:
                log_lines = [_log_source_hint("agent")]
            return PlainTextResponse("\n".join(log_lines))

        if source in {"install", "watchdog"}:
            log_path = INSTALL_LOG_PATH if source == "install" else WATCHDOG_LOG_PATH
            if not log_path.exists():
                return PlainTextResponse(_log_source_hint(source))
            proc = await asyncio.create_subprocess_exec(
                "tail", "-n", str(lines), str(log_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-u", JOURNAL_UNIT, "--no-pager", "-n", str(lines),
                "--output=short-iso",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        text = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and not text:
            detail = stderr.decode("utf-8", errors="replace").strip() or None
            return PlainTextResponse(_log_source_hint(source, detail))
        return PlainTextResponse(text or _log_source_hint(source))
    except HTTPException:
        raise
    except Exception as exc:
        return PlainTextResponse(_log_source_hint(source, str(exc)))




@router.get("/api/logs/stream")
async def api_logs_stream(source: str = Query(default="journal")):
    """Server-Sent Events stream of live log output."""
    source = _normalize_log_source(source)

    async def generate():
        yield "retry: 5000\n\n"

        if source == "agent":
            last_index = len(proxmox_log_buffer)
            hinted_empty = False
            while True:
                current_len = len(proxmox_log_buffer)
                if current_len < last_index:
                    last_index = 0
                if current_len > last_index:
                    for line in proxmox_log_buffer[last_index:current_len]:
                        yield _encode_sse_line(str(line))
                    last_index = current_len
                    hinted_empty = False
                    continue
                if current_len == 0 and not hinted_empty:
                    hinted_empty = True
                    yield _encode_sse_line(_log_source_hint("agent"))
                yield await _stream_keepalive()
                await asyncio.sleep(LOG_STREAM_POLL_SECS)

        if source == "install" and not INSTALL_LOG_PATH.exists():
            yield _encode_sse_line(_log_source_hint("install"))

        proc = None
        idle_deadline = time.monotonic() + 30
        try:
            if source == "install":
                proc = await asyncio.create_subprocess_exec(
                    "tail", "-n", "0", "-F", str(INSTALL_LOG_PATH),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    "journalctl", "-u", JOURNAL_UNIT, "-f", "--no-pager", "-n", "0",
                    "--output=short-iso",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
        except Exception:
            yield "event: error\ndata: Log stream failed\n\n"
            return

        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=LOG_STREAM_KEEPALIVE_SECS)
                except asyncio.TimeoutError:
                    if time.monotonic() >= idle_deadline:
                        yield "event: end\ndata: Log stream idle timeout\n\n"
                        return
                    yield ": keepalive\n\n"
                    continue
                if line:
                    idle_deadline = time.monotonic() + 30
                    text = line.decode("utf-8", errors="replace").rstrip("\n")
                    if text:
                        yield _encode_sse_line(text)
                    continue
                detail = None
                if source == "journal" and proc.stderr is not None:
                    detail = (await proc.stderr.read()).decode("utf-8", errors="replace").strip() or None
                terminal = _log_source_hint(source, detail)
                yield f"event: end\ndata: {terminal}\n\n"
                return
        finally:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.kill()

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
