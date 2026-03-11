#!/usr/bin/env python3
"""
Dual-track live capture + transcription of meetings (Teams/Google Meet).

Track 1: BlackHole 2ch (system audio = what others say)
Track 2: MacBook Pro Microphone (what you say)

Auto-switches audio output to Multi-Output Device on start,
and restores to MacBook Pro Speakers on stop.

Outputs a labeled transcript with "Others:" and "You:" prefixes.
"""

import atexit
import signal
import subprocess
import sys
import time
from pathlib import Path

CHUNK_SECONDS = 30
AUDIO_DIR = Path("chunks")
TRANSCRIPT_FILE = Path("teams_transcript_live.txt")

# Device indices (from ffmpeg -f avfoundation -list_devices true -i "")
BLACKHOLE_INDEX = "0"  # BlackHole 2ch - system/meeting audio
MIC_INDEX = "1"        # MacBook Pro Microphone - your voice

MULTI_OUTPUT_DEVICE = "Multi-Output Device"
SPEAKERS_DEVICE = "MacBook Pro Speakers"

SILENCE_THRESHOLD = 1000  # bytes - skip chunks smaller than this


def switch_audio_output(device_name):
    """Switch macOS audio output device using SwitchAudioSource."""
    try:
        result = subprocess.run(
            ["SwitchAudioSource", "-s", device_name, "-t", "output"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"  Audio output → {device_name}")
        else:
            print(f"  WARNING: Could not switch to {device_name}: {result.stderr.strip()}")
    except FileNotFoundError:
        print("  WARNING: SwitchAudioSource not found. Install with: brew install switchaudio-osx")


def restore_speakers():
    """Restore audio output to speakers on exit."""
    switch_audio_output(SPEAKERS_DEVICE)


def record_dual(system_path, mic_path, duration):
    """Record system audio and mic simultaneously as separate files."""
    cmd_sys = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "avfoundation", "-i", f":{BLACKHOLE_INDEX}",
        "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        "-t", str(duration), str(system_path),
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


def get_volume_db(audio_path):
    """Get mean volume in dB for an audio file."""
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
    """Subtract system audio from mic to isolate user's voice."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(mic_path),
        "-i", str(sys_path),
        "-filter_complex",
        "[1]volume=-1[inv];[0][inv]amix=inputs=2:duration=first:weights=1 0.8",
        "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        str(cleaned_path),
    ]
    subprocess.run(cmd, capture_output=True)
    return cleaned_path


def transcribe_chunk(chunk_path, model):
    """Transcribe a single audio chunk."""
    if not chunk_path.exists() or chunk_path.stat().st_size < SILENCE_THRESHOLD:
        return ""
    result = model.transcribe(str(chunk_path), language="en", verbose=False)
    segments = []
    for seg in result.get("segments", []):
        text = seg["text"].strip()
        if text:
            segments.append(text)
    return " ".join(segments)


def main():
    import whisper

    AUDIO_DIR.mkdir(exist_ok=True)

    # Switch to Multi-Output Device so BlackHole receives system audio
    print("Setting up audio routing...")
    switch_audio_output(MULTI_OUTPUT_DEVICE)
    atexit.register(restore_speakers)

    print("Loading Whisper model (base)...")
    model = whisper.load_model("base")
    print("Model loaded.\n")

    session_ts = time.strftime("%Y%m%d_%H%M%S")
    elapsed = 0
    chunk_num = 0
    running = True

    def handle_sigint(sig, frame):
        nonlocal running
        running = False
        print("\nStopping after current chunk...")

    signal.signal(signal.SIGINT, handle_sigint)

    with open(TRANSCRIPT_FILE, "w") as f:
        f.write(f"Meeting Transcript - {session_ts}\n")
        f.write(f"System audio: BlackHole 2ch (device :{BLACKHOLE_INDEX})\n")
        f.write(f"Mic audio: MacBook Pro Microphone (device :{MIC_INDEX})\n")
        f.write("=" * 60 + "\n\n")

    print(f"Dual-track recording:")
    print(f"  System audio (Others) → BlackHole 2ch (:{BLACKHOLE_INDEX})")
    print(f"  Microphone (You)      → MacBook Pro Mic (:{MIC_INDEX})")
    print(f"  Chunk size: {CHUNK_SECONDS}s")
    print(f"  Transcript: {TRANSCRIPT_FILE}")
    print("Press Ctrl+C to stop.\n")
    sys.stdout.flush()

    while running:
        chunk_num += 1
        sys_path = AUDIO_DIR / f"sys_{chunk_num:04d}.wav"
        mic_path = AUDIO_DIR / f"mic_{chunk_num:04d}.wav"

        ts_start = time.strftime("%H:%M:%S", time.gmtime(elapsed))

        proc_sys, proc_mic = record_dual(sys_path, mic_path, CHUNK_SECONDS)
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

        # Check volume levels to determine who was speaking
        sys_vol = get_volume_db(sys_path) if sys_path.exists() else -91.0
        mic_vol = get_volume_db(mic_path) if mic_path.exists() else -91.0

        sys_has_audio = sys_vol > -60.0
        mic_has_audio = mic_vol > -60.0

        lines = []

        if sys_has_audio:
            sys_text = transcribe_chunk(sys_path, model)
            if sys_text:
                line = f"{ts_label} Others: {sys_text}"
                lines.append(line)
                print(line)

        if mic_has_audio:
            cleaned_path = AUDIO_DIR / f"cleaned_{chunk_num:04d}.wav"
            remove_speaker_bleed(mic_path, sys_path, cleaned_path)
            cleaned_vol = get_volume_db(cleaned_path) if cleaned_path.exists() else -91.0
            if cleaned_vol > -60.0:
                mic_text = transcribe_chunk(cleaned_path, model)
                if mic_text:
                    line = f"{ts_label} You: {mic_text}"
                    lines.append(line)
                    print(line)
            cleaned_path.unlink(missing_ok=True)

        if not lines:
            print(f"  {ts_label} (silence)")

        sys.stdout.flush()

        if lines:
            with open(TRANSCRIPT_FILE, "a") as f:
                f.write("\n".join(lines) + "\n")

        # Clean up chunks
        sys_path.unlink(missing_ok=True)
        mic_path.unlink(missing_ok=True)

    # Restore speakers (also handled by atexit)
    restore_speakers()
    print(f"\nDone. Full transcript saved to: {TRANSCRIPT_FILE}")


if __name__ == "__main__":
    main()
