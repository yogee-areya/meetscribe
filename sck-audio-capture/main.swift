import AudioToolbox
import CoreMedia
import Foundation
@preconcurrency import ScreenCaptureKit

// MARK: - Audio capture (OBS-style: raw CMBlockBuffer → ExtAudioFile)

var globalCapture: AudioCapture?

class AudioCapture: NSObject, SCStreamDelegate, SCStreamOutput {
    let outputURL: URL
    var extAudioFile: ExtAudioFileRef?
    var sampleCount: Int64 = 0
    var bufferCount: Int = 0
    var stream: SCStream?
    let writeQueue = DispatchQueue(label: "audio.write")

    init(outputURL: URL) {
        self.outputURL = outputURL
        super.init()
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        guard CMSampleBufferDataIsReady(sampleBuffer) else { return }

        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else { return }

        let channelCount = Int(asbd.pointee.mChannelsPerFrame)
        guard channelCount >= 1 else { return }

        // Get raw data pointer from CMBlockBuffer (like OBS does — no copy)
        guard let dataBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }
        var length = 0
        var dataPointer: UnsafeMutablePointer<Int8>?
        CMBlockBufferGetDataPointer(dataBuffer, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length, dataPointerOut: &dataPointer)
        guard let bytes = dataPointer, length > 0 else { return }

        // Frame count: OBS does data_buffer_length / mBytesPerFrame / mChannelsPerFrame
        // But for planar float32: mBytesPerFrame is per-channel, so frames = length / 4 / channelCount
        let bytesPerSample = Int(asbd.pointee.mBitsPerChannel / 8)
        let frames = length / bytesPerSample / channelCount

        bufferCount += 1

        // Create ExtAudioFile on first buffer
        if extAudioFile == nil {
            // Input format: what SCK delivers (Float32, planar/non-interleaved)
            var inputASBD = AudioStreamBasicDescription(
                mSampleRate: asbd.pointee.mSampleRate,
                mFormatID: kAudioFormatLinearPCM,
                mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsNonInterleaved,
                mBytesPerPacket: 4,
                mFramesPerPacket: 1,
                mBytesPerFrame: 4,
                mChannelsPerFrame: UInt32(channelCount),
                mBitsPerChannel: 32,
                mReserved: 0
            )

            // Output format: 16kHz mono 16-bit WAV (for Whisper)
            var outputASBD = AudioStreamBasicDescription(
                mSampleRate: 16000,
                mFormatID: kAudioFormatLinearPCM,
                mFormatFlags: kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked,
                mBytesPerPacket: 2,
                mFramesPerPacket: 1,
                mBytesPerFrame: 2,
                mChannelsPerFrame: 1,
                mBitsPerChannel: 16,
                mReserved: 0
            )

            var fileRef: ExtAudioFileRef?
            let cfURL = outputURL as CFURL
            let status = ExtAudioFileCreateWithURL(
                cfURL,
                kAudioFileWAVEType,
                &outputASBD,
                nil,
                AudioFileFlags.eraseFile.rawValue,
                &fileRef
            )

            guard status == noErr, let file = fileRef else {
                fputs("Error creating audio file: \(status)\n", stderr)
                return
            }

            // Tell ExtAudioFile what format we'll feed it (so it converts for us)
            let clientStatus = ExtAudioFileSetProperty(
                file,
                kExtAudioFileProperty_ClientDataFormat,
                UInt32(MemoryLayout<AudioStreamBasicDescription>.size),
                &inputASBD
            )

            guard clientStatus == noErr else {
                fputs("Error setting client format: \(clientStatus)\n", stderr)
                ExtAudioFileDispose(file)
                return
            }

            extAudioFile = file
            fputs("Audio stream started (src: \(Int(asbd.pointee.mSampleRate))Hz, \(channelCount)ch)\n", stderr)
        }

        guard let file = extAudioFile else { return }

        // Build AudioBufferList with channel pointers (OBS-style: point into CMBlockBuffer data)
        let bytesPerChannel = length / channelCount
        let bufferListSize = MemoryLayout<AudioBufferList>.size + MemoryLayout<AudioBuffer>.size * (channelCount - 1)
        let rawPtr = UnsafeMutableRawPointer.allocate(byteCount: bufferListSize, alignment: MemoryLayout<AudioBufferList>.alignment)
        let abl = rawPtr.bindMemory(to: AudioBufferList.self, capacity: 1)

        abl.pointee.mNumberBuffers = UInt32(channelCount)

        // Set up channel pointers — each channel is sequential in the block buffer
        withUnsafeMutablePointer(to: &abl.pointee.mBuffers) { firstBuf in
            let bufArray = UnsafeMutableBufferPointer<AudioBuffer>(start: firstBuf, count: channelCount)
            for ch in 0..<channelCount {
                let offset = ch * bytesPerChannel
                bufArray[ch] = AudioBuffer(
                    mNumberChannels: 1,
                    mDataByteSize: UInt32(bytesPerChannel),
                    mData: UnsafeMutableRawPointer(bytes.advanced(by: offset))
                )
            }
        }

        let writeStatus = ExtAudioFileWrite(file, UInt32(frames), abl)

        rawPtr.deallocate()

        if writeStatus != noErr {
            if bufferCount <= 3 {
                fputs("Write error: \(writeStatus)\n", stderr)
            }
        } else {
            sampleCount += Int64(frames)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("Stream error: \(error)\n", stderr)
    }

    func finish() {
        if let file = extAudioFile {
            ExtAudioFileDispose(file)
            extAudioFile = nil
        }
        // sampleCount is in source sample rate (48kHz), convert to real seconds
        let seconds = Double(sampleCount) / 48000.0
        fputs("Captured \(String(format: "%.1f", seconds))s of audio (\(bufferCount) buffers)\n", stderr)
    }
}

// MARK: - Main

func printUsage() {
    fputs("""
    sck-audio-capture — Capture system/app audio via ScreenCaptureKit

    USAGE:
        sck-audio-capture --desktop -o output.wav [-t duration]
        sck-audio-capture --app com.google.Chrome -o output.wav [-t duration]
        sck-audio-capture --list-apps

    OPTIONS:
        --desktop           Capture all system audio
        --app BUNDLE_ID     Capture audio from a specific app
        --list-apps         List running apps with audio
        -o, --output PATH   Output WAV file path (16kHz mono)
        -t, --duration SECS Recording duration in seconds
        -h, --help          Show this help

    """, stderr)
}

func listApps() {
    Task {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: false)
            for app in content.applications {
                print("\(app.bundleIdentifier)  —  \(app.applicationName)")
            }
        } catch {
            fputs("Error: \(error)\n", stderr)
        }
        exit(0)
    }
}

func startCapture(bundleID: String?, outputPath: String, duration: TimeInterval?) {
    Task {
        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: false)
        } catch {
            fputs("Error getting content: \(error)\n", stderr)
            exit(1)
        }

        guard let display = content.displays.first else {
            fputs("Error: No displays found\n", stderr)
            exit(1)
        }

        let filter: SCContentFilter
        if let bundleID = bundleID {
            guard let app = content.applications.first(where: { $0.bundleIdentifier == bundleID }) else {
                fputs("Error: App '\(bundleID)' not found. Use --list-apps to see available apps.\n", stderr)
                exit(1)
            }
            filter = SCContentFilter(display: display, including: [app], exceptingWindows: [])
            fputs("Capturing audio from: \(app.applicationName) (\(bundleID))\n", stderr)
        } else {
            filter = SCContentFilter(display: display, excludingWindows: [])
            fputs("Capturing all desktop audio\n", stderr)
        }

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.channelCount = 2
        config.sampleRate = 48000
        // Minimal video (SCK requires it, like OBS does)
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let outputURL = URL(fileURLWithPath: outputPath)
        let capture = AudioCapture(outputURL: outputURL)
        globalCapture = capture

        let stream = SCStream(filter: filter, configuration: config, delegate: capture)
        capture.stream = stream

        do {
            // OBS adds both video and audio outputs — SCK requires video even for audio-only
            try stream.addStreamOutput(capture, type: .screen, sampleHandlerQueue: DispatchQueue(label: "video"))
            try stream.addStreamOutput(capture, type: .audio, sampleHandlerQueue: DispatchQueue(label: "audio"))
        } catch {
            fputs("Error adding stream output: \(error)\n", stderr)
            exit(1)
        }

        do {
            try await stream.startCapture()
        } catch {
            fputs("Error starting capture: \(error)\n", stderr)
            exit(1)
        }

        fputs("Recording to: \(outputPath)\n", stderr)

        if let duration = duration {
            fputs("Duration: \(Int(duration))s\n", stderr)
        } else {
            fputs("Press Ctrl+C to stop\n", stderr)
        }

        // Clean shutdown on SIGINT
        signal(SIGINT) { _ in
            fputs("\nStopping...\n", stderr)
            globalCapture?.finish()
            exit(0)
        }

        // Timer-based stop for duration mode
        if let duration = duration {
            DispatchQueue.main.asyncAfter(deadline: .now() + duration) {
                stream.stopCapture { _ in
                    capture.finish()
                    exit(0)
                }
            }
        }
    }
}

// MARK: - Argument parsing

var args = CommandLine.arguments.dropFirst()
var mode: String?
var bundleID: String?
var outputPath: String?
var duration: TimeInterval?

while let arg = args.first {
    args = args.dropFirst()
    switch arg {
    case "--desktop":
        mode = "desktop"
    case "--app":
        mode = "app"
        bundleID = args.first.map { String($0) }
        args = args.dropFirst()
    case "--list-apps":
        mode = "list"
    case "-o", "--output":
        outputPath = args.first.map { String($0) }
        args = args.dropFirst()
    case "-t", "--duration":
        if let val = args.first, let d = TimeInterval(val) {
            duration = d
        }
        args = args.dropFirst()
    case "-h", "--help":
        printUsage()
        exit(0)
    default:
        fputs("Unknown option: \(arg)\n", stderr)
        printUsage()
        exit(1)
    }
}

switch mode {
case "list":
    listApps()
case "desktop", "app":
    guard let output = outputPath else {
        fputs("Error: --output is required\n", stderr)
        printUsage()
        exit(1)
    }
    startCapture(bundleID: bundleID, outputPath: output, duration: duration)
default:
    printUsage()
    exit(1)
}

dispatchMain()
