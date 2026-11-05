"""Logs API routes (moved verbatim from server.py; logic imported from server)."""
# Postpone annotation evaluation so modern type hints are never evaluated at runtime.
from __future__ import annotations

# APIRouter groups these endpoints so server.py can mount them with include_router().
from fastapi import APIRouter
# Log constants and helpers live in server.py; this file only defines the routes.
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

# The router object every endpoint below is registered on.
router = APIRouter()




@router.get("/api/logs/service")
def api_service_logs(lines: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    """Return the last N lines of the spoke service's systemd journal as JSON."""
    # Timestamp of when the logs were read.
    timestamp = iso_utcnow()
    # Run journalctl for our unit with a 5 s timeout.
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
        # Success: return the lines.
        if result.returncode == 0:
            log_lines = result.stdout.splitlines()
            return {"lines": log_lines, "count": len(log_lines), "timestamp": timestamp}
    except Exception:
        # journalctl missing (e.g. not systemd) or timed out: fall through to the placeholder.
        pass

    # Placeholder response so the UI still renders something.
    fallback = ["journalctl not available"]
    return {"lines": fallback, "count": len(fallback), "timestamp": timestamp}




@router.get("/api/logs/history")
async def api_logs_history(
    lines: int = Query(default=300, ge=10, le=2000),
    source: str = Query(default="journal"),
):
    """Return the last N lines from the selected log source."""
    # Map aliases to a canonical source name.
    source = _normalize_log_source(source)
    # Any unexpected failure becomes a hint message instead of an HTTP error.
    try:
        # agent: lines come from the in-memory proxmox agent log buffer.
        if source == "agent":
            log_lines = proxmox_log_buffer[-lines:]
            # Empty buffer: show a hint explaining where agent logs come from.
            if not log_lines:
                log_lines = [_log_source_hint("agent")]
            return PlainTextResponse("\n".join(log_lines))

        # install/watchdog: tail the corresponding log file.
        if source in {"install", "watchdog"}:
            log_path = INSTALL_LOG_PATH if source == "install" else WATCHDOG_LOG_PATH
            # Missing file: show a hint instead.
            if not log_path.exists():
                return PlainTextResponse(_log_source_hint(source))
            proc = await asyncio.create_subprocess_exec(
                "tail", "-n", str(lines), str(log_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        else:
            # Default: read the spoke service's journal.
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-u", JOURNAL_UNIT, "--no-pager", "-n", str(lines),
                "--output=short-iso",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        # Wait at most 10 s for the command, then decode the output.
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        text = stdout.decode("utf-8", errors="replace").strip()
        # The command failed with no output: show a hint including its error text.
        if proc.returncode != 0 and not text:
            detail = stderr.decode("utf-8", errors="replace").strip() or None
            return PlainTextResponse(_log_source_hint(source, detail))
        # Return the text, or a hint if it was empty.
        return PlainTextResponse(text or _log_source_hint(source))
    except HTTPException:
        # Re-raise deliberate HTTP errors unchanged.
        raise
    except Exception as exc:
        # Anything else is shown as a hint with the error message.
        return PlainTextResponse(_log_source_hint(source, str(exc)))




@router.get("/api/logs/stream")
async def api_logs_stream(source: str = Query(default="journal")):
    """Server-Sent Events stream of live log output."""
    # Map aliases to a canonical source name.
    source = _normalize_log_source(source)

    async def generate():
        """Async generator that yields SSE-formatted log lines until the source ends or goes idle.
        """
        # Tell the browser to wait 5 s before reconnecting if the stream drops.
        yield "retry: 5000\n\n"

        # agent: follow the in-memory agent log buffer forever.
        if source == "agent":
            # Index of the next unsent line, and whether the "empty" hint was already sent.
            last_index = len(proxmox_log_buffer)
            hinted_empty = False
            while True:
                # If the buffer shrank (it was trimmed or reset), start again from the beginning.
                current_len = len(proxmox_log_buffer)
                if current_len < last_index:
                    last_index = 0
                # Send any new lines, then loop straight away to check for more.
                if current_len > last_index:
                    for line in proxmox_log_buffer[last_index:current_len]:
                        yield _encode_sse_line(str(line))
                    last_index = current_len
                    hinted_empty = False
                    continue
                # Send the empty-buffer hint only once.
                if current_len == 0 and not hinted_empty:
                    hinted_empty = True
                    yield _encode_sse_line(_log_source_hint("agent"))
                # Nothing new: send a keepalive and wait before polling again.
                yield await _stream_keepalive()
                await asyncio.sleep(LOG_STREAM_POLL_SECS)

        # Tell the user if the install log does not exist yet; tail -F will wait for it.
        if source == "install" and not INSTALL_LOG_PATH.exists():
            yield _encode_sse_line(_log_source_hint("install"))

        # Child process (set below) and the time after which an idle stream is closed.
        proc = None
        idle_deadline = time.monotonic() + 30
        # Start following the source; failure to start ends the stream with an error event.
        try:
            # install: follow the install log file, even if it is recreated (-F).
            if source == "install":
                proc = await asyncio.create_subprocess_exec(
                    "tail", "-n", "0", "-F", str(INSTALL_LOG_PATH),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            else:
                # Otherwise follow the service journal from now on.
                proc = await asyncio.create_subprocess_exec(
                    "journalctl", "-u", JOURNAL_UNIT, "-f", "--no-pager", "-n", "0",
                    "--output=short-iso",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
        except Exception:
            yield "event: error\ndata: Log stream failed\n\n"
            return

        # Read lines until the process ends or the stream goes idle.
        try:
            while True:
                # Wait for a line, but wake up periodically to send keepalives.
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=LOG_STREAM_KEEPALIVE_SECS)
                except asyncio.TimeoutError:
                    # No line in time: close the stream if idle too long, else send a keepalive
                    # comment.
                    if time.monotonic() >= idle_deadline:
                        yield "event: end\ndata: Log stream idle timeout\n\n"
                        return
                    yield ": keepalive\n\n"
                    continue
                # Got a line: extend the idle deadline and forward it if non-empty.
                if line:
                    idle_deadline = time.monotonic() + 30
                    text = line.decode("utf-8", errors="replace").rstrip("\n")
                    if text:
                        yield _encode_sse_line(text)
                    continue
                # Empty read means the process exited: report why (journalctl stderr) and end the
                # stream.
                detail = None
                if source == "journal" and proc.stderr is not None:
                    detail = (await proc.stderr.read()).decode("utf-8", errors="replace").strip() or None
                terminal = _log_source_hint(source, detail)
                yield f"event: end\ndata: {terminal}\n\n"
                return
        finally:
            # Always stop the child process when the client disconnects or the stream ends.
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.kill()

    # Stream as text/event-stream with caching and proxy buffering disabled so lines arrive live.
    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
