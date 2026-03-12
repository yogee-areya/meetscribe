# MeetScribe

Automated meeting transcription for macOS. Records system audio (what others say) and mic (what you say) separately, transcribes with faster-whisper, and labels speakers.

Includes a **calendar-aware daemon** that polls Google Calendar, starts recording 1 minute before each meeting, and stops when you leave the call — fully hands-free.

## Features

- **Dual-track recording** — System audio via ScreenCaptureKit + mic via ffmpeg, kept separate
- **ScreenCaptureKit** — Native macOS API for system audio (same approach as OBS Studio). No BlackHole, no Multi-Output Device, no audio routing changes
- **App-aware capture** — Auto-detects meeting app (Chrome, Teams, Zoom, etc.) and captures only that app's audio
- **Calendar-aware daemon** — Auto-starts/stops based on Google Calendar events
- **Call hangup detection** — Stops recording if you leave early (90s silence on both tracks)
- **Speaker diarization** — Identifies different remote speakers via pyannote-audio
- **Lazy resource usage** — Whisper + diarization models load only during meetings, unload after
- **Daily transcripts** — One file per day with all meetings as sections
- **Volume controls always work** — No audio device switching needed

## Requirements

- **macOS 13+** (Ventura or later — required for ScreenCaptureKit audio capture)
- **Apple Silicon** or Intel Mac
- **Python 3.10+**
- **Xcode Command Line Tools** (for compiling the Swift audio capture tool)
- **Screen & Audio Recording permission** granted to Terminal (System Settings → Privacy & Security → Screen & Audio Recording)

## Setup

### 1. Install system dependencies

```bash
# Xcode Command Line Tools (for Swift compiler)
xcode-select --install

# Audio/video processing (for mic recording + volume detection)
brew install ffmpeg
```

> **No BlackHole or SwitchAudioSource needed.** System audio is captured natively via ScreenCaptureKit.

### 2. Build the ScreenCaptureKit audio capture tool

```bash
cd sck-audio-capture
swiftc main.swift \
    -framework ScreenCaptureKit \
    -framework CoreMedia \
    -framework AudioToolbox \
    -framework Foundation \
    -o sck-audio-capture \
    -target arm64-apple-macosx13.0
cd ..
```

Test it:
```bash
# List apps that can be captured
sck-audio-capture/sck-audio-capture --list-apps

# Capture 5 seconds of all desktop audio
sck-audio-capture/sck-audio-capture --desktop -o test.wav -t 5

# Capture 5 seconds from Chrome only (Google Meet)
sck-audio-capture/sck-audio-capture --app com.google.Chrome -o test.wav -t 5
```

### 3. Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. (Optional) Enable speaker diarization

Speaker diarization labels different remote speakers (Speaker_00, Speaker_01, etc.). It requires a HuggingFace token with access to the pyannote models.

1. Accept the model terms at https://huggingface.co/pyannote/speaker-diarization-3.1
2. Create a token at https://huggingface.co/settings/tokens
3. Login:
   ```bash
   pip install huggingface_hub
   huggingface-cli login
   ```

> Without this, transcription still works — all remote speakers are labeled "Speaker".

### 5. Set up Google Calendar access (for daemon mode)

The daemon uses [`gws`](https://github.com/nicholasgasior/gws) CLI to fetch calendar events.

```bash
npm install -g @nicholasgasior/gws
gws auth login
```

Follow the OAuth flow to grant calendar read access. Verify with:

```bash
gws calendar events list --params '{"calendarId": "primary", "singleEvents": true, "orderBy": "startTime", "timeMin": "2025-01-01T00:00:00Z", "timeMax": "2025-01-02T00:00:00Z"}'
```

### 6. Grant Screen & Audio Recording permission

The first time you run `sck-audio-capture`, macOS will prompt for Screen & Audio Recording permission. Grant it to Terminal (or whichever terminal app you use).

You can also enable it manually: **System Settings → Privacy & Security → Screen & Audio Recording → Terminal**.

## Usage

### Daemon mode (fully automated)

```bash
source .venv/bin/activate
python meeting_daemon.py
```

The daemon will:
1. Fetch today's meetings from Google Calendar
2. Sleep until 1 minute before the next meeting
3. Detect which meeting app is running (Chrome, Teams, Zoom, etc.)
4. Record dual-track (system audio via SCK + mic via ffmpeg) in 30s chunks
5. Transcribe each chunk with faster-whisper
6. Apply speaker diarization to the system audio track
7. Stop when the meeting ends or you leave the call (90s silence)
8. Unload models from memory
9. Repeat for the next meeting

### Manual recording

```bash
source .venv/bin/activate

# Live dual-track capture until Ctrl+C
python capture_teams_live.py

# One-shot: record for 60 seconds then transcribe
python capture_teams.py --duration 60
```

### Run daemon on login (launchd)

Create `~/Library/LaunchAgents/com.meetscribe.daemon.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.meetscribe.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/meetscribe/.venv/bin/python</string>
        <string>/path/to/meetscribe/meeting_daemon.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/meetscribe</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/path/to/meetscribe/meeting_daemon.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/meetscribe/meeting_daemon.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
```

> Update `/path/to/meetscribe` and add your node path to `PATH` if using `gws`.

```bash
launchctl load ~/Library/LaunchAgents/com.meetscribe.daemon.plist
tail -f meeting_daemon.log
```

## Output

Daily transcripts in `transcripts/`:

```
transcripts/
  2026-03-12.txt
  2026-03-13.txt
```

Each file contains all meetings for that day:

```
Meeting Transcripts — 2026-03-12
Engine: faster-whisper (small) + pyannote diarization
============================================================

────────────────────────────────────────────────────────────
Morning Standup
   10:00 - 10:30
────────────────────────────────────────────────────────────

[00:00:00 - 00:00:30] Speaker_00: The main blocker is the API integration...
[00:00:00 - 00:00:30] You: Yeah I'll pick that up today.
[00:00:30 - 00:01:00] Speaker_01: Can we also look at the deployment pipeline?

────────────────────────────────────────────────────────────
Sprint Review
   15:00 - 15:30
────────────────────────────────────────────────────────────

[00:00:00 - 00:00:30] Speaker: Let's go over the demo...
```

## Supported meeting apps

| App | Bundle ID | Notes |
|---|---|---|
| Google Meet | `com.google.Chrome` | Runs in Chrome |
| Microsoft Teams | `com.microsoft.teams2` | Desktop app |
| Zoom | `us.zoom.xos` | Desktop app |
| Webex | `com.cisco.webexmeetingsapp` | Desktop app |
| Arc | `company.thebrowser.Browser` | Web-based meetings |
| Firefox | `org.mozilla.firefox` | Web-based meetings |
| Safari | `com.apple.Safari` | Web-based meetings |
| Brave | `com.brave.Browser` | Web-based meetings |
| Edge | `com.microsoft.edgemac` | Web-based meetings |

If no known meeting app is detected, it falls back to capturing all desktop audio.

## Configuration

Key constants at the top of `meeting_daemon.py`:

| Variable | Default | Description |
|---|---|---|
| `CHUNK_SECONDS` | `30` | Recording chunk duration |
| `MIC_INDEX` | `"1"` | Microphone device index (check `ffmpeg -f avfoundation -list_devices true -i ""`) |
| `SILENCE_STREAK_LIMIT` | `3` | Silent chunks before auto-stop (90s) |
| `MEETING_START_BUFFER` | `60` | Seconds before meeting to start recording |
| `VOLUME_THRESHOLD` | `-60.0` | dB threshold for silence detection |
| `WHISPER_MODEL` | `"small"` | faster-whisper model size |

## Architecture

```
Google Calendar ──► meeting_daemon.py ──► sleeps until meeting
                                              │
                              detect_meeting_app()
                              (Chrome? Teams? Zoom?)
                                              │
                                    ┌─────────┴─────────┐
                                    ▼                   ▼
                            ScreenCaptureKit      MacBook Mic
                           (app system audio)     (your voice)
                           sck-audio-capture        ffmpeg
                                    │                   │
                                    ▼                   ▼
                            faster-whisper +      faster-whisper
                          pyannote diarization
                                    │                   │
                                    ▼                   ▼
                          "Speaker_00: ..."      "You: ..."
                                    │                   │
                                    └─────────┬─────────┘
                                              ▼
                                  transcripts/2026-03-12.txt
```

## License

MIT
