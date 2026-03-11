#!/usr/bin/env python3
"""
Manual dual-track live capture + transcription of meetings.

Track 1: BlackHole 2ch (system audio = others) — with speaker diarization
Track 2: MacBook Pro Microphone (your voice)

Uses faster-whisper (4x speed) + pyannote-audio (speaker labels).
Auto-switches audio output on start, restores on stop.
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

BLACKHOLE_INDEX = "0"  # BlackHole 2ch
MIC_INDEX = "1"        # MacBook Pro Microphone

MULTI_OUTPUT_DEVICE = "Multi-Output Device"
SPEAKERS_DEVICE = "MacBook Pro Speakers"

SILENCE_THRESHOLD = 1000
VOLUME_THRESHOLD = -60.0

WHISPER_MODEL = "small"
COMPUTE_TYPE = "int8"


def switch_audio_output(device_name):
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
        print("  WARNING: SwitchAudioSource not found")


def restore_speakers():
    switch_audio_output(SPEAKERS_DEVICE)


def record_dual(system_path, mic_path, duration):
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


def transcribe_chunk(chunk_path, model):
    if not chunk_path.exists() or chunk_path.stat().st_size < SILENCE_THRESHOLD:
        return ""
    segments, _ = model.transcribe(str(chunk_path), language="en", beam_size=5, vad_filter=True)
    texts = [seg.text.strip() for seg in segments if seg.text.strip()]
    return " ".join(texts)


def transcribe_with_diarization(chunk_path, whisper_model, diarize_pipeline):
    """Transcribe with speaker labels."""
    if not chunk_path.exists() or chunk_path.stat().st_size < SILENCE_THRESHOLD:
        return []

    segments, _ = whisper_model.transcribe(str(chunk_path), language="en", beam_size=5, vad_filter=True)
    whisper_segments = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments if s.text.strip()]

    if not whisper_segments:
        return []

    if diarize_pipeline is not None:
        try:
            diarization = diarize_pipeline(str(chunk_path))
            for ws in whisper_segments:
                best_speaker, best_overlap = "Speaker", 0
                for turn, _, speaker in diarization.itertracks(yield_label=True):
                    overlap = max(0, min(ws["end"], turn.end) - max(ws["start"], turn.start))
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_speaker = speaker
                ws["speaker"] = best_speaker
        except Exception as e:
            print(f"  Diarization error: {e}")
            for ws in whisper_segments:
                ws["speaker"] = "Speaker"
    else:
        for ws in whisper_segments:
            ws["speaker"] = "Speaker"

    return [(ws["speaker"], ws["text"]) for ws in whisper_segments]


def main():
    from faster_whisper import WhisperModel

    AUDIO_DIR.mkdir(exist_ok=True)

    print("Setting up audio routing...")
    switch_audio_output(MULTI_OUTPUT_DEVICE)
    atexit.register(restore_speakers)

    print(f"Loading faster-whisper model ({WHISPER_MODEL})...")
    whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type=COMPUTE_TYPE)
    print("Whisper model loaded.")

    diarize_pipeline = None
    try:
        from pyannote.audio import Pipeline
        import torch
        print("Loading pyannote diarization pipeline...")
        diarize_pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", token=True)
        if torch.backends.mps.is_available():
            diarize_pipeline.to(torch.device("mps"))
            print("Diarization loaded (MPS/GPU).")
        else:
            print("Diarization loaded (CPU).")
    except Exception as e:
        print(f"Diarization not available: {e}. Continuing without it.")

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
        f.write(f"Engine: faster-whisper ({WHISPER_MODEL}) + pyannote diarization\n")
        f.write("=" * 60 + "\n\n")

    print(f"\nDual-track recording:")
    print(f"  System audio (Others) → BlackHole 2ch (:{BLACKHOLE_INDEX})")
    print(f"  Microphone (You)      → MacBook Pro Mic (:{MIC_INDEX})")
    print(f"  Engine: faster-whisper ({WHISPER_MODEL}) + pyannote")
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

        sys_vol = get_volume_db(sys_path) if sys_path.exists() else -91.0
        mic_vol = get_volume_db(mic_path) if mic_path.exists() else -91.0

        lines = []

        # System audio with speaker diarization
        if sys_vol > VOLUME_THRESHOLD:
            diarized = transcribe_with_diarization(sys_path, whisper_model, diarize_pipeline)
            for speaker, text in diarized:
                line = f"{ts_label} {speaker}: {text}"
                lines.append(line)
                print(line)

        # Mic — just your voice
        if mic_vol > VOLUME_THRESHOLD:
            mic_text = transcribe_chunk(mic_path, whisper_model)
            if mic_text:
                line = f"{ts_label} You: {mic_text}"
                lines.append(line)
                print(line)

        if not lines:
            print(f"  {ts_label} (silence)")

        sys.stdout.flush()

        if lines:
            with open(TRANSCRIPT_FILE, "a") as f:
                f.write("\n".join(lines) + "\n")

        sys_path.unlink(missing_ok=True)
        mic_path.unlink(missing_ok=True)

    restore_speakers()
    print(f"\nDone. Full transcript saved to: {TRANSCRIPT_FILE}")


if __name__ == "__main__":
    main()
