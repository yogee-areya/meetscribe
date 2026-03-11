# MeetScribe

Automated meeting transcription for macOS. Records system audio (what others say) and mic (what you say) separately, transcribes with OpenAI Whisper, and labels the output.

Includes a **calendar-aware daemon** that polls Google Calendar, starts recording 1 minute before each meeting, and stops when you leave the call — fully hands-free.

## Features

- **Dual-track recording** — System audio via BlackHole + mic, kept separate
- **Echo cancellation** — Subtracts speaker bleed from mic so "You:" lines are clean
- **Calendar-aware daemon** — Auto-starts/stops based on Google Calendar events
- **Call hangup detection** — Stops recording if you leave early (90s silence)
- **Lazy resource usage** — Whisper loads only during meetings, unloads after
- **Auto audio routing** — Switches to Multi-Output Device for capture, restores speakers when done
- **Named transcripts** — Files named after the meeting from your calendar

## Prerequisites

- macOS (tested on Sonnet/Sequoia)
- Python 3.10+
- [Homebrew](https://brew.sh)

## Setup

### 1. Install system dependencies

```bash
# Audio loopback driver (requires reboot after install)
brew install blackhole-2ch

# Audio output switcher
brew install switchaudio-osx

# Audio/video processing
brew install ffmpeg
```

**Reboot** after installing BlackHole.

### 2. Create Multi-Output Device

This routes system audio to both your speakers AND BlackHole simultaneously.

1. Open **Audio MIDI Setup** (`/System/Applications/Utilities/Audio MIDI Setup.app`)
2. Click **"+"** at bottom-left → **"Create Multi-Output Device"**
3. Check both **"MacBook Pro Speakers"** (or your output device) and **"BlackHole 2ch"**
4. Ensure your speakers are listed **first**

> The daemon handles switching to/from this device automatically — you don't need to set it as default manually.

### 3. Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install openai-whisper
```

### 4. Set up Google Calendar access (for daemon mode)

The daemon uses [`gws`](https://github.com/nicholasgasior/gws) CLI to fetch calendar events.

```bash
npm install -g @nicholasgasior/gws
gws auth login
```

Follow the OAuth flow to grant calendar read access. Verify with:

```bash
gws calendar events list --params '{"calendarId": "primary", "singleEvents": true, "orderBy": "startTime", "timeMin": "2025-01-01T00:00:00Z", "timeMax": "2025-01-02T00:00:00Z"}'
```

### 5. Verify audio devices

```bash
ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | grep -A10 "audio devices"
```

You should see:
```
[0] BlackHole 2ch
[1] MacBook Pro Microphone
```

> If indices differ, update `BLACKHOLE_INDEX` and `MIC_INDEX` in the scripts.

## Usage

### Manual recording

```bash
source .venv/bin/activate

# Record + transcribe until Ctrl+C
python capture_teams_live.py

# One-shot: record for 60 seconds then transcribe
python capture_teams.py --duration 60
```

### Daemon mode (fully automated)

```bash
source .venv/bin/activate
python meeting_daemon.py
```

The daemon will:
1. Fetch today's meetings from Google Calendar
2. Sleep until 1 minute before the next meeting
3. Switch audio output to Multi-Output Device
4. Record dual-track (system + mic) in 30s chunks
5. Transcribe each chunk with Whisper
6. Stop when the meeting ends or you leave the call (90s silence)
7. Restore speakers and unload Whisper from memory
8. Repeat for the next meeting

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
# Start
launchctl load ~/Library/LaunchAgents/com.meetscribe.daemon.plist

# Stop
launchctl unload ~/Library/LaunchAgents/com.meetscribe.daemon.plist

# Check logs
tail -f meeting_daemon.log
```

## Output

Transcripts are saved to `transcripts/` with the meeting name and date:

```
transcripts/
  Morning_Standup_20260311_1000.txt
  Sprint_Review_20260311_1500.txt
```

Each transcript contains labeled, timestamped lines:

```
Meeting: Morning Standup
Start: 2026-03-11 10:00
End: 2026-03-11 10:30
============================================================

[00:00:00 - 00:00:30] Others: So the main blocker right now is the API integration...
[00:00:00 - 00:00:30] You: Yeah I'll pick that up today, should be done by EOD.
[00:00:30 - 00:01:00] Others: Great. Next item...
```

## Configuration

Key constants at the top of each script:

| Variable | Default | Description |
|---|---|---|
| `CHUNK_SECONDS` | `30` | Recording chunk duration |
| `BLACKHOLE_INDEX` | `"0"` | BlackHole device index |
| `MIC_INDEX` | `"1"` | Microphone device index |
| `SILENCE_STREAK_LIMIT` | `3` | Silent chunks before auto-stop (daemon) |
| `MEETING_START_BUFFER` | `60` | Seconds before meeting to start recording |
| `VOLUME_THRESHOLD` | `-60.0` | dB threshold for silence detection |

## How it works

```
Google Calendar ──► meeting_daemon.py ──► sleeps until meeting
                                              │
                                    ┌─────────┴─────────┐
                                    ▼                   ▼
                              BlackHole 2ch      MacBook Mic
                             (system audio)      (your voice)
                                    │                   │
                                    │         echo cancellation
                                    │          (subtract system
                                    │           audio from mic)
                                    ▼                   ▼
                              Whisper STT         Whisper STT
                                    │                   │
                                    ▼                   ▼
                              "Others: ..."      "You: ..."
                                    │                   │
                                    └─────────┬─────────┘
                                              ▼
                                    transcripts/<meeting>.txt
```

## License

MIT
