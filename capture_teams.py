#!/usr/bin/env python3
"""
One-shot audio capture and transcription.

Records from BlackHole (system audio) then transcribes with faster-whisper.

Usage:
    python capture_teams.py                  # Record until Ctrl+C, then transcribe
    python capture_teams.py --duration 60    # Record for 60 seconds
    python capture_teams.py --output call    # Custom output filename prefix
"""

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

BLACKHOLE_INDEX = "0"


def record_audio(output_path, duration=None):
    cmd = [
        "ffmpeg", "-y",
        "-f", "avfoundation",
        "-i", f":{BLACKHOLE_INDEX}",
        "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
    ]
    if duration:
        cmd.extend(["-t", str(duration)])
    cmd.append(output_path)

    print(f"Recording from BlackHole 2ch (device :{BLACKHOLE_INDEX})...")
    print(f"Output: {output_path}")
    if duration:
        print(f"Duration: {duration}s")
    else:
        print("Press Ctrl+C to stop recording.")
    print()

    return subprocess.Popen(cmd, stderr=subprocess.PIPE)


def main():
    parser = argparse.ArgumentParser(description="Capture and transcribe meeting audio")
    parser.add_argument("--duration", "-d", type=int, help="Recording duration in seconds")
    parser.add_argument("--output", "-o", default="meeting", help="Output filename prefix")
    parser.add_argument("--model", "-m", default="small", choices=["tiny", "base", "small", "medium", "large-v3"],
                        help="Whisper model size (default: small)")
    parser.add_argument("--skip-transcribe", action="store_true", help="Only record")
    args = parser.parse_args()

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    audio_file = Path(f"{args.output}_{timestamp}.wav")
    transcript_file = Path(f"{args.output}_{timestamp}_transcript.txt")

    proc = record_audio(str(audio_file), args.duration)

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\nStopping recording...")
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)

    if not audio_file.exists() or audio_file.stat().st_size == 0:
        print("ERROR: No audio was captured.")
        sys.exit(1)

    file_size_mb = audio_file.stat().st_size / (1024 * 1024)
    print(f"\nRecorded: {audio_file} ({file_size_mb:.1f} MB)")

    if args.skip_transcribe:
        return

    from faster_whisper import WhisperModel

    print(f"\nLoading faster-whisper model ({args.model})...")
    model = WhisperModel(args.model, device="cpu", compute_type="int8")

    print("Transcribing...")
    segments, info = model.transcribe(str(audio_file), language="en", beam_size=5, vad_filter=True)

    lines = []
    full_text_parts = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            ts_start = time.strftime("%H:%M:%S", time.gmtime(seg.start))
            ts_end = time.strftime("%H:%M:%S", time.gmtime(seg.end))
            lines.append(f"[{ts_start} - {ts_end}] {text}")
            full_text_parts.append(text)

    full_text = " ".join(full_text_parts)

    with open(transcript_file, "w") as f:
        f.write(f"Meeting Transcript - {timestamp}\n")
        f.write(f"Audio: {audio_file}\n")
        f.write(f"Engine: faster-whisper ({args.model})\n")
        f.write("=" * 60 + "\n\n")
        f.write("FULL TEXT:\n")
        f.write(full_text + "\n\n")
        f.write("=" * 60 + "\n\n")
        f.write("TIMESTAMPED:\n")
        f.write("\n".join(lines) + "\n")

    print(f"\nTranscript saved: {transcript_file}")
    print("\n--- Full Text ---")
    print(full_text[:500] + ("..." if len(full_text) > 500 else ""))


if __name__ == "__main__":
    main()
