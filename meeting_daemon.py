#!/usr/bin/env python3
"""
MeetScribe — Meeting transcription daemon.

Polls Google Calendar via `gws` CLI. Starts recording 1 min before
each meeting. Stops when the meeting ends OR when you leave the call
(detected by sustained silence on system audio).

System audio captured via ScreenCaptureKit (like OBS Studio) — no
BlackHole or Multi-Output Device needed. Mic captured via ffmpeg.

Uses faster-whisper for 4x speed + pyannote-audio for speaker diarization
on the system audio track to separate multiple remote speakers.

Transcripts are named after the meeting from Google Calendar.
Models are loaded only during active recordings to save RAM.
"""

import argparse
import json
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- Config ---
CHUNK_SECONDS = 30
MEETING_START_BUFFER = 60   # start recording 1 min before meeting
SILENCE_STREAK_LIMIT = 3    # 3 consecutive silent chunks (90s) = you left
TRANSCRIPTS_DIR = Path("transcripts")
CHUNKS_DIR = Path("chunks")

MIC_INDEX = "1"             # MacBook Pro Microphone

# ScreenCaptureKit audio capture binary
SCK_BINARY = Path(__file__).parent / "sck-audio-capture" / "sck-audio-capture"

# App bundle IDs for meeting platforms (SCK captures by app)
MEETING_APPS = {
    "com.google.Chrome":         "Google Chrome",       # Google Meet, web-based Teams
    "com.microsoft.teams2":      "Microsoft Teams",     # Teams desktop app
    "com.microsoft.teams":       "Microsoft Teams",     # Teams older version
    "us.zoom.xos":               "Zoom",                # Zoom desktop
    "com.cisco.webexmeetingsapp": "Webex",              # Webex
    "company.thebrowser.Browser": "Arc",                # Arc browser
    "org.mozilla.firefox":       "Firefox",
    "com.apple.Safari":          "Safari",
    "com.brave.Browser":         "Brave",
    "com.microsoft.edgemac":     "Microsoft Edge",
}

SILENCE_THRESHOLD = 1000    # bytes
VOLUME_THRESHOLD = -60.0    # dB

WHISPER_MODEL = "small"     # faster-whisper: tiny, base, small, medium, large-v3
COMPUTE_TYPE = "int8"       # int8 for CPU, float16 for GPU


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def detect_meeting_app():
    """Detect which meeting app is running by checking SCK's app list."""
    try:
        result = subprocess.run(
            [str(SCK_BINARY), "--list-apps"],
            capture_output=True, text=True, timeout=5
        )
        running_apps = result.stdout.strip().splitlines()
        for line in running_apps:
            bundle_id = line.split("  —  ")[0].strip() if "  —  " in line else ""
            if bundle_id in MEETING_APPS:
                return bundle_id, MEETING_APPS[bundle_id]
    except Exception as e:
        log(f"App detection error: {e}")
    return None, None


def sanitize_filename(name):
    name = re.sub(r'[^\w\s\-]', '', name)
    name = re.sub(r'\s+', '_', name.strip())
    return name[:80] or "untitled_meeting"


def get_todays_meetings():
    """Fetch all meetings for today."""
    now = datetime.now(timezone.utc)
    time_min = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        result = subprocess.run(
            ["gws", "calendar", "events", "list", "--params", json.dumps({
                "calendarId": "primary",
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": True,
                "orderBy": "startTime",
            })],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            log(f"gws error: {result.stderr.strip()}")
            return []
        data = json.loads(result.stdout)
    except Exception as e:
        log(f"Calendar fetch error: {e}")
        return []

    meetings = []
    for item in data.get("items", []):
        start_str = item.get("start", {}).get("dateTime")
        end_str = item.get("end", {}).get("dateTime")
        if not start_str or not end_str:
            continue

        meetings.append({
            "id": item.get("id", ""),
            "summary": item.get("summary", "Untitled Meeting"),
            "start": datetime.fromisoformat(start_str),
            "end": datetime.fromisoformat(end_str),
        })

    return meetings


def get_volume_db(audio_path):
    result = subprocess.run(
        ["ffmpeg", "-i", str(audio_path), "-af", "volumedetect", "-f", "null", "/dev/null"],
        capture_output=True, text=True
    )
    for line in result.stderr.splitlines():
        if "mean_volume" in line:
            try:
                return float(line.split("mean_volume:")[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
    return -91.0


def record_chunk(sys_path, mic_path, duration, app_bundle_id=None):
    """Record system audio via SCK and mic via ffmpeg in parallel."""
    # System audio via ScreenCaptureKit
    sck_cmd = [str(SCK_BINARY)]
    if app_bundle_id:
        sck_cmd += ["--app", app_bundle_id]
    else:
        sck_cmd += ["--desktop"]
    sck_cmd += ["-o", str(sys_path), "-t", str(duration)]

    # Mic via ffmpeg
    mic_cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "avfoundation", "-i", f":{MIC_INDEX}",
        "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        "-t", str(duration), str(mic_path),
    ]
    proc_sys = subprocess.Popen(sck_cmd, stderr=subprocess.PIPE)
    proc_mic = subprocess.Popen(mic_cmd, stderr=subprocess.PIPE)
    return proc_sys, proc_mic


def transcribe_chunk(chunk_path, model):
    """Transcribe using faster-whisper. Returns list of (start, end, text) segments."""
    if not chunk_path.exists() or chunk_path.stat().st_size < SILENCE_THRESHOLD:
        return []
    segments, _ = model.transcribe(
        str(chunk_path), language="en",
        beam_size=5, vad_filter=True,
    )
    results = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            results.append((seg.start, seg.end, text))
    return results


def transcribe_with_diarization(chunk_path, whisper_model, diarize_pipeline, chunk_offset=0):
    """Transcribe system audio with speaker diarization."""
    if not chunk_path.exists() or chunk_path.stat().st_size < SILENCE_THRESHOLD:
        return []

    segments, _ = whisper_model.transcribe(
        str(chunk_path), language="en",
        beam_size=5, vad_filter=True,
    )
    whisper_segments = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            whisper_segments.append({"start": seg.start, "end": seg.end, "text": text})

    if not whisper_segments:
        return []

    if diarize_pipeline is not None:
        try:
            diarization = diarize_pipeline(str(chunk_path))
            for ws in whisper_segments:
                best_speaker = "Speaker"
                best_overlap = 0
                for turn, _, speaker in diarization.itertracks(yield_label=True):
                    overlap_start = max(ws["start"], turn.start)
                    overlap_end = min(ws["end"], turn.end)
                    overlap = max(0, overlap_end - overlap_start)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_speaker = speaker
                ws["speaker"] = best_speaker
        except Exception as e:
            log(f"  Diarization error: {e}")
            for ws in whisper_segments:
                ws["speaker"] = "Speaker"
    else:
        for ws in whisper_segments:
            ws["speaker"] = "Speaker"

    return [(ws["speaker"], ws["text"]) for ws in whisper_segments]


def load_models():
    """Load faster-whisper and pyannote diarization pipeline."""
    from faster_whisper import WhisperModel

    log(f"Loading faster-whisper model ({WHISPER_MODEL}, {COMPUTE_TYPE})...")
    whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type=COMPUTE_TYPE)
    log("Whisper model loaded.")

    diarize_pipeline = None
    try:
        from pyannote.audio import Pipeline
        import torch

        log("Loading pyannote speaker diarization pipeline...")
        diarize_pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=True,
        )
        if torch.backends.mps.is_available():
            diarize_pipeline.to(torch.device("mps"))
            log("Diarization pipeline loaded (MPS/GPU).")
        else:
            log("Diarization pipeline loaded (CPU).")
    except Exception as e:
        log(f"Diarization not available: {e}")
        log("Continuing without speaker diarization.")

    return whisper_model, diarize_pipeline


def unload_models():
    import gc
    gc.collect()
    log("Models unloaded from memory.")


def get_daily_transcript(meeting):
    """Get the daily transcript file path. Creates header if new day."""
    date_str = meeting["start"].strftime("%Y-%m-%d")
    transcript_file = TRANSCRIPTS_DIR / f"{date_str}.txt"

    if not transcript_file.exists():
        with open(transcript_file, "w") as f:
            f.write(f"Meeting Transcripts — {date_str}\n")
            f.write(f"Engine: faster-whisper ({WHISPER_MODEL}) + pyannote diarization\n")
            f.write("=" * 60 + "\n")

    return transcript_file


def record_meeting(meeting):
    """Record and transcribe a single meeting."""
    transcript_file = get_daily_transcript(meeting)

    log(f"▶ Starting recording: {meeting['summary']}")
    log(f"  Scheduled: {meeting['start'].strftime('%H:%M')} - {meeting['end'].strftime('%H:%M')}")
    log(f"  Transcript: {transcript_file}")

    whisper_model, diarize_pipeline = load_models()

    # Detect which meeting app to capture audio from
    app_bundle_id, app_name = detect_meeting_app()
    if app_bundle_id:
        log(f"  Detected meeting app: {app_name} ({app_bundle_id})")
    else:
        log("  No known meeting app detected — capturing all desktop audio")

    # Append meeting header to daily file
    with open(transcript_file, "a") as f:
        f.write(f"\n\n{'─' * 60}\n")
        f.write(f"📅 {meeting['summary']}\n")
        f.write(f"   {meeting['start'].strftime('%H:%M')} - {meeting['end'].strftime('%H:%M')}\n")
        f.write(f"{'─' * 60}\n\n")

    elapsed = 0
    chunk_num = 0
    silent_streak = 0

    while True:
        now = datetime.now(meeting["start"].tzinfo)
        if now > meeting["end"] + timedelta(minutes=2):
            log(f"■ Meeting time ended: {meeting['summary']}")
            break

        # Re-detect meeting app every 5 chunks in case user switched apps
        if chunk_num % 5 == 0 and chunk_num > 0:
            new_id, new_name = detect_meeting_app()
            if new_id and new_id != app_bundle_id:
                app_bundle_id, app_name = new_id, new_name
                log(f"  Switched to: {app_name} ({app_bundle_id})")

        chunk_num += 1
        sys_path = CHUNKS_DIR / f"sys_{chunk_num:04d}.wav"
        mic_path = CHUNKS_DIR / f"mic_{chunk_num:04d}.wav"
        ts_start = time.strftime("%H:%M:%S", time.gmtime(elapsed))

        proc_sys, proc_mic = record_chunk(sys_path, mic_path, CHUNK_SECONDS, app_bundle_id)
        try:
            proc_sys.wait()
            proc_mic.wait()
        except:
            proc_sys.terminate()
            proc_mic.terminate()
            proc_sys.wait()
            proc_mic.wait()
            break

        elapsed += CHUNK_SECONDS
        ts_end = time.strftime("%H:%M:%S", time.gmtime(elapsed))
        ts_label = f"[{ts_start} - {ts_end}]"

        sys_vol = get_volume_db(sys_path) if sys_path.exists() else -91.0
        mic_vol = get_volume_db(mic_path) if mic_path.exists() else -91.0

        lines = []

        # Transcribe system audio with speaker diarization
        if sys_vol > VOLUME_THRESHOLD:
            diarized = transcribe_with_diarization(
                sys_path, whisper_model, diarize_pipeline, elapsed
            )
            for speaker, text in diarized:
                line = f"{ts_label} {speaker}: {text}"
                lines.append(line)
                log(line)

        # Transcribe mic audio separately
        if mic_vol > VOLUME_THRESHOLD:
            mic_segments = transcribe_chunk(mic_path, whisper_model)
            mic_text = " ".join(t for _, _, t in mic_segments)
            if mic_text:
                line = f"{ts_label} You: {mic_text}"
                lines.append(line)
                log(line)

        if not lines:
            log(f"  {ts_label} (silence)")

        if lines:
            with open(transcript_file, "a") as f:
                f.write("\n".join(lines) + "\n")

        sys_path.unlink(missing_ok=True)
        mic_path.unlink(missing_ok=True)

        # Detect call hangup — silence on BOTH tracks means you left
        both_silent = sys_vol <= VOLUME_THRESHOLD and mic_vol <= VOLUME_THRESHOLD
        if both_silent:
            silent_streak += 1
            if silent_streak >= SILENCE_STREAK_LIMIT:
                log(f"■ No audio for {silent_streak * CHUNK_SECONDS}s — you likely left. Stopping.")
                break
        else:
            silent_streak = 0

    del whisper_model, diarize_pipeline
    unload_models()
    log(f"Transcript saved: {transcript_file}")
    return transcript_file


def sleep_until(target_dt, running_check):
    """Sleep until target datetime, checking actual time each second.
    Resilient to system sleep/wake — always checks real clock.
    """
    while running_check():
        now = datetime.now(target_dt.tzinfo)
        remaining = (target_dt - now).total_seconds()
        if remaining <= 0:
            return True
        time.sleep(min(remaining, 1))
    return False


def main():
    parser = argparse.ArgumentParser(description="Meeting transcription daemon")
    parser.add_argument("--daemon", action="store_true", help="Run in background mode")
    args = parser.parse_args()

    if args.daemon:
        log_file = open("meeting_daemon.log", "a")
        sys.stdout = log_file
        sys.stderr = log_file

    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    CHUNKS_DIR.mkdir(exist_ok=True)

    running = True

    def handle_sigint(sig, frame):
        nonlocal running
        running = False
        log("Shutting down daemon...")

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    log("MeetScribe daemon started.")
    log(f"Engine: faster-whisper ({WHISPER_MODEL}) + pyannote diarization")
    log(f"System audio: ScreenCaptureKit (no BlackHole needed)")
    log(f"Transcripts → {TRANSCRIPTS_DIR.resolve()}")

    recorded_meetings = set()

    while running:
        # Fetch today's meetings
        meetings = get_todays_meetings()
        now_utc = datetime.now(timezone.utc)

        # Find next unrecorded meeting that hasn't ended
        next_meeting = None
        for m in meetings:
            if m["id"] in recorded_meetings:
                continue
            if m["end"].astimezone(timezone.utc) > now_utc:
                next_meeting = m
                break

        if next_meeting is None:
            log("No upcoming meetings. Checking again in 15 min...")
            # Use real-clock sleep — wake at exact time, not counting seconds
            wake_at = datetime.now(timezone.utc) + timedelta(minutes=15)
            sleep_until(wake_at, lambda: running)
            continue

        # Calculate when to start recording (1 min before meeting)
        record_start = next_meeting["start"] - timedelta(seconds=MEETING_START_BUFFER)
        now_local = datetime.now(next_meeting["start"].tzinfo)
        wait_seconds = (record_start - now_local).total_seconds()

        if wait_seconds > 0:
            log(f"Next: {next_meeting['summary']} at {next_meeting['start'].strftime('%H:%M')}. "
                f"Waking at {record_start.strftime('%H:%M:%S')} ({int(wait_seconds // 60)}m {int(wait_seconds % 60)}s)")
            reached = sleep_until(record_start, lambda: running)
            if not reached:
                break

        # Time to record
        log(f"Meeting time: {next_meeting['summary']} "
            f"({next_meeting['start'].strftime('%H:%M')} - {next_meeting['end'].strftime('%H:%M')})")
        recorded_meetings.add(next_meeting["id"])
        record_meeting(next_meeting)

    log("Daemon stopped.")


if __name__ == "__main__":
    main()
