#!/usr/bin/env python3
"""
Meeting transcription daemon.

Polls Google Calendar via `gws` CLI. Starts recording 1 min before
each meeting. Stops when the meeting ends OR when you leave the call
(detected by sustained silence on system audio).

Transcripts are named after the meeting from Google Calendar.
Whisper model is loaded only during active recordings to save RAM.
"""

import argparse
import atexit
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
SILENCE_STREAK_LIMIT = 3    # 3 consecutive silent chunks (90s) = you left the call
TRANSCRIPTS_DIR = Path("transcripts")
CHUNKS_DIR = Path("chunks")

BLACKHOLE_INDEX = "0"       # BlackHole 2ch - system/meeting audio
MIC_INDEX = "1"             # MacBook Pro Microphone

MULTI_OUTPUT_DEVICE = "Multi-Output Device"
SPEAKERS_DEVICE = "MacBook Pro Speakers"

SILENCE_THRESHOLD = 1000    # bytes
VOLUME_THRESHOLD = -60.0    # dB


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def switch_audio_output(device_name):
    try:
        result = subprocess.run(
            ["SwitchAudioSource", "-s", device_name, "-t", "output"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            log(f"Audio output → {device_name}")
        else:
            log(f"WARNING: Could not switch to {device_name}: {result.stderr.strip()}")
    except FileNotFoundError:
        log("WARNING: SwitchAudioSource not found")


def restore_speakers():
    switch_audio_output(SPEAKERS_DEVICE)


def sanitize_filename(name):
    name = re.sub(r'[^\w\s\-]', '', name)
    name = re.sub(r'\s+', '_', name.strip())
    return name[:80] or "untitled_meeting"


def get_todays_meetings():
    """Fetch all meetings for today with conference links."""
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


def remove_speaker_bleed(mic_path, sys_path, cleaned_path):
    """Subtract system audio from mic to isolate user's voice (echo cancellation)."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(mic_path),
        "-i", str(sys_path),
        "-filter_complex",
        # Phase-invert system audio and mix with mic — cancels speaker bleed
        "[1]volume=-1[inv];[0][inv]amix=inputs=2:duration=first:weights=1 0.8",
        "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        str(cleaned_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Fallback to raw mic if cancellation fails
        log(f"  WARNING: Echo cancellation failed, using raw mic")
        return mic_path
    return cleaned_path


def record_chunk(sys_path, mic_path, duration):
    cmd_sys = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "avfoundation", "-i", f":{BLACKHOLE_INDEX}",
        "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        "-t", str(duration), str(sys_path),
    ]
    cmd_mic = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "avfoundation", "-i", f":{MIC_INDEX}",
        "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        "-t", str(duration), str(mic_path),
    ]
    proc_sys = subprocess.Popen(cmd_sys, stderr=subprocess.PIPE)
    proc_mic = subprocess.Popen(cmd_mic, stderr=subprocess.PIPE)
    return proc_sys, proc_mic


def transcribe_chunk(chunk_path, model):
    if not chunk_path.exists() or chunk_path.stat().st_size < SILENCE_THRESHOLD:
        return ""
    result = model.transcribe(str(chunk_path), language="en", verbose=False)
    segments = []
    for seg in result.get("segments", []):
        text = seg["text"].strip()
        if text:
            segments.append(text)
    return " ".join(segments)


def load_whisper():
    import whisper
    log("Loading Whisper model (base)...")
    model = whisper.load_model("base")
    log("Model loaded.")
    return model


def unload_whisper():
    import gc
    gc.collect()
    log("Whisper model unloaded from memory.")


def record_meeting(meeting):
    """Record and transcribe a single meeting. Stops on meeting end or call hangup."""
    meeting_name = sanitize_filename(meeting["summary"])
    date_str = meeting["start"].strftime("%Y%m%d_%H%M")
    transcript_file = TRANSCRIPTS_DIR / f"{meeting_name}_{date_str}.txt"

    log(f"▶ Starting recording: {meeting['summary']}")
    log(f"  Scheduled: {meeting['start'].strftime('%H:%M')} - {meeting['end'].strftime('%H:%M')}")
    log(f"  Transcript: {transcript_file}")

    model = load_whisper()
    switch_audio_output(MULTI_OUTPUT_DEVICE)

    with open(transcript_file, "w") as f:
        f.write(f"Meeting: {meeting['summary']}\n")
        f.write(f"Start: {meeting['start'].strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"End: {meeting['end'].strftime('%Y-%m-%d %H:%M')}\n")
        f.write("=" * 60 + "\n\n")

    elapsed = 0
    chunk_num = 0
    silent_streak = 0  # consecutive chunks with no audio on BOTH tracks

    while True:
        # Stop if past meeting end time (+ 2 min grace)
        now = datetime.now(meeting["start"].tzinfo)
        if now > meeting["end"] + timedelta(minutes=2):
            log(f"■ Meeting time ended: {meeting['summary']}")
            break

        chunk_num += 1
        sys_path = CHUNKS_DIR / f"sys_{chunk_num:04d}.wav"
        mic_path = CHUNKS_DIR / f"mic_{chunk_num:04d}.wav"
        ts_start = time.strftime("%H:%M:%S", time.gmtime(elapsed))

        proc_sys, proc_mic = record_chunk(sys_path, mic_path, CHUNK_SECONDS)
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

        has_any_audio = sys_vol > VOLUME_THRESHOLD or mic_vol > VOLUME_THRESHOLD
        lines = []

        if sys_vol > VOLUME_THRESHOLD:
            sys_text = transcribe_chunk(sys_path, model)
            if sys_text:
                line = f"{ts_label} Others: {sys_text}"
                lines.append(line)
                log(line)

        if mic_vol > VOLUME_THRESHOLD:
            # Remove speaker bleed from mic before transcribing
            cleaned_path = CHUNKS_DIR / f"cleaned_{chunk_num:04d}.wav"
            remove_speaker_bleed(mic_path, sys_path, cleaned_path)
            cleaned_vol = get_volume_db(cleaned_path) if cleaned_path.exists() else -91.0
            if cleaned_vol > VOLUME_THRESHOLD:
                mic_text = transcribe_chunk(cleaned_path, model)
                if mic_text:
                    line = f"{ts_label} You: {mic_text}"
                    lines.append(line)
                    log(line)
            cleaned_path.unlink(missing_ok=True)

        if not lines:
            log(f"  {ts_label} (silence)")

        if lines:
            with open(transcript_file, "a") as f:
                f.write("\n".join(lines) + "\n")

        sys_path.unlink(missing_ok=True)
        mic_path.unlink(missing_ok=True)

        # Detect if user left the call: sustained silence on system audio
        if sys_vol <= VOLUME_THRESHOLD:
            silent_streak += 1
            if silent_streak >= SILENCE_STREAK_LIMIT:
                log(f"■ No call audio for {silent_streak * CHUNK_SECONDS}s — you likely left. Stopping.")
                break
        else:
            silent_streak = 0

    del model
    unload_whisper()
    switch_audio_output(SPEAKERS_DEVICE)
    log(f"Transcript saved: {transcript_file}")
    return transcript_file


def seconds_until(dt):
    """Seconds from now until a datetime."""
    now = datetime.now(dt.tzinfo)
    diff = (dt - now).total_seconds()
    return max(0, diff)


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
    atexit.register(restore_speakers)

    running = True

    def handle_sigint(sig, frame):
        nonlocal running
        running = False
        log("Shutting down daemon...")

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    log("Meeting daemon started.")
    log(f"Transcripts → {TRANSCRIPTS_DIR.resolve()}")

    recorded_meetings = set()

    while running:
        # Fetch today's meetings
        meetings = get_todays_meetings()
        now = datetime.now(timezone.utc)

        # Find the next upcoming meeting we haven't recorded
        next_meeting = None
        for m in meetings:
            if m["id"] in recorded_meetings:
                continue
            # Meeting hasn't ended yet
            if m["end"].astimezone(timezone.utc) > now:
                next_meeting = m
                break

        if next_meeting is None:
            # No more meetings today — check again in 15 min
            log("No upcoming meetings. Sleeping 15 min...")
            for _ in range(900):
                if not running:
                    break
                time.sleep(1)
            continue

        # Calculate when to start (1 min before meeting)
        record_start = next_meeting["start"] - timedelta(seconds=MEETING_START_BUFFER)
        wait_seconds = seconds_until(record_start)

        if wait_seconds > 0:
            log(f"Next: {next_meeting['summary']} at {next_meeting['start'].strftime('%H:%M')}. "
                f"Sleeping {int(wait_seconds // 60)}m {int(wait_seconds % 60)}s...")
            for _ in range(int(wait_seconds)):
                if not running:
                    break
                time.sleep(1)
            if not running:
                break

        # Time to record
        log(f"Meeting detected: {next_meeting['summary']} "
            f"({next_meeting['start'].strftime('%H:%M')} - {next_meeting['end'].strftime('%H:%M')})")
        recorded_meetings.add(next_meeting["id"])
        record_meeting(next_meeting)

    log("Daemon stopped.")


if __name__ == "__main__":
    main()
