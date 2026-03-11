#!/usr/bin/env python3
"""
Capture audio from a Microsoft Teams call and transcribe it using Whisper.

Usage:
    python capture_teams.py                  # Record until Ctrl+C, then transcribe
    python capture_teams.py --duration 60    # Record for 60 seconds
    python capture_teams.py --output call    # Custom output filename prefix
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def find_teams_audio_device():
    """Find the Microsoft Teams Audio device index via ffmpeg."""
    result = subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True
    )
    output = result.stderr
    audio_section = False
    for line in output.splitlines():
        if "AVFoundation audio devices" in line:
            audio_section = True
            continue
        if audio_section and "Microsoft Teams Audio" in line:
            # Extract index from e.g. "[AVFoundation indev @ ...] [2] Microsoft Teams Audio"
            idx = line.split("]")[1].strip().lstrip("[").split("]")[0]
            return idx
    return None


def record_audio(device_index, output_path, duration=None):
    """Record audio from the given AVFoundation device using ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "avfoundation",
        "-i", f":{device_index}",
        "-ac", "1",             # mono
        "-ar", "16000",         # 16kHz for whisper
        "-acodec", "pcm_s16le", # WAV format
    ]
    if duration:
        cmd.extend(["-t", str(duration)])
    cmd.append(output_path)

    print(f"Recording from Microsoft Teams Audio (device :{device_index})...")
    print(f"Output: {output_path}")
    if duration:
        print(f"Duration: {duration}s")
    else:
        print("Press Ctrl+C to stop recording.")
    print()

    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
    return proc


def transcribe_audio(audio_path, model_name="base"):
    """Transcribe audio file using OpenAI Whisper."""
    import whisper

    print(f"\nLoading Whisper model '{model_name}'...")
    model = whisper.load_model(model_name)

    print("Transcribing audio...")
    result = model.transcribe(str(audio_path), language="en", verbose=False)
    return result


def format_transcript(result):
    """Format whisper result into a readable transcript with timestamps."""
    lines = []
    for seg in result.get("segments", []):
        start = seg["start"]
        end = seg["end"]
        text = seg["text"].strip()
        ts_start = time.strftime("%H:%M:%S", time.gmtime(start))
        ts_end = time.strftime("%H:%M:%S", time.gmtime(end))
        lines.append(f"[{ts_start} - {ts_end}] {text}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Capture Teams call audio and transcribe")
    parser.add_argument("--duration", "-d", type=int, help="Recording duration in seconds (default: until Ctrl+C)")
    parser.add_argument("--output", "-o", default="teams_call", help="Output filename prefix (default: teams_call)")
    parser.add_argument("--model", "-m", default="base", choices=["tiny", "base", "small", "medium", "large"],
                        help="Whisper model size (default: base)")
    parser.add_argument("--skip-transcribe", action="store_true", help="Only record, skip transcription")
    args = parser.parse_args()

    # Find Teams audio device
    device_idx = find_teams_audio_device()
    if device_idx is None:
        print("ERROR: Could not find 'Microsoft Teams Audio' device.")
        print("Make sure you are on an active Teams call.")
        sys.exit(1)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    audio_file = Path(f"{args.output}_{timestamp}.wav")
    transcript_file = Path(f"{args.output}_{timestamp}_transcript.txt")

    # Record
    proc = record_audio(device_idx, str(audio_file), args.duration)

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\nStopping recording...")
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)

    if not audio_file.exists() or audio_file.stat().st_size == 0:
        print("ERROR: No audio was captured. Is the Teams call active?")
        sys.exit(1)

    file_size_mb = audio_file.stat().st_size / (1024 * 1024)
    print(f"\nRecorded: {audio_file} ({file_size_mb:.1f} MB)")

    if args.skip_transcribe:
        print("Skipping transcription (--skip-transcribe).")
        return

    # Transcribe
    result = transcribe_audio(audio_file, args.model)

    # Save transcript
    full_text = result.get("text", "").strip()
    timestamped = format_transcript(result)

    with open(transcript_file, "w") as f:
        f.write(f"Teams Call Transcript - {timestamp}\n")
        f.write(f"Audio file: {audio_file}\n")
        f.write(f"Model: {args.model}\n")
        f.write("=" * 60 + "\n\n")
        f.write("FULL TEXT:\n")
        f.write(full_text + "\n\n")
        f.write("=" * 60 + "\n\n")
        f.write("TIMESTAMPED TRANSCRIPT:\n")
        f.write(timestamped + "\n")

    print(f"\nTranscript saved: {transcript_file}")
    print("\n--- Full Text ---")
    print(full_text[:500] + ("..." if len(full_text) > 500 else ""))


if __name__ == "__main__":
    main()
