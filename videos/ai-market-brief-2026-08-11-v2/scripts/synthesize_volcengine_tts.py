#!/usr/bin/env python3
"""Generate caption-locked Doubao TTS and a 60-second voiceover track.

The API key is read from macOS Keychain and is never written to the project.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
RESOURCE_ID = "seed-tts-2.0"
KEYCHAIN_SERVICE = "codex-volcengine-tts-api-key"
KEYCHAIN_ACCOUNT = "aigc-agent"


def api_key() -> str:
    return subprocess.check_output(
        [
            "security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            KEYCHAIN_ACCOUNT,
            "-w",
        ],
        text=True,
    ).strip()


def decode_chunks(payload: str) -> bytes:
    decoder = json.JSONDecoder()
    index = 0
    audio = bytearray()
    while index < len(payload):
        while index < len(payload) and payload[index].isspace():
            index += 1
        if index >= len(payload):
            break
        chunk, index = decoder.raw_decode(payload, index)
        code = chunk.get("code")
        if code not in (0, 20000000, None):
            message = chunk.get("message", "")
            raise RuntimeError(f"Doubao TTS failed: code={code}, message={message}")
        encoded = chunk.get("data")
        if encoded:
            audio.extend(base64.b64decode(encoded))
    if not audio:
        raise RuntimeError("Doubao TTS returned no audio bytes")
    return bytes(audio)


def synthesize(text: str, speaker: str, context: str, speech_rate: int, output: Path) -> None:
    body = {
        "user": {"uid": "stock-agent"},
        "req_params": {
            "text": text,
            "speaker": speaker,
            "model": "seed-tts-2.0-standard",
            "audio_params": {
                "format": "mp3",
                "sample_rate": 24000,
                "speech_rate": speech_rate,
            },
            "context_texts": [context],
        },
    }
    request = Request(
        ENDPOINT,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "X-Api-Key": api_key(),
            "X-Api-Resource-Id": RESOURCE_ID,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        audio = decode_chunks(response.read().decode("utf-8"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(audio)


def duration(path: Path) -> float:
    result = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        text=True,
    )
    return float(result.strip())


def build_voiceover(entries: list[dict], total_duration: float, output: Path) -> None:
    command = ["ffmpeg", "-y"]
    for entry in entries:
        command.extend(["-i", entry["path"]])

    filters = []
    delayed = []
    for index, entry in enumerate(entries):
        delay_ms = round(entry["start"] * 1000)
        label = f"a{index}"
        filters.append(
            f"[{index}:a]aresample=48000,adelay={delay_ms}:all=1[{label}]"
        )
        delayed.append(f"[{label}]")
    filters.append(
        "".join(delayed)
        + f"amix=inputs={len(entries)}:duration=longest:normalize=0,"
        + f"apad=pad_dur={total_duration},atrim=0:{total_duration},"
        + "loudnorm=I=-16:LRA=7:TP=-1.5[out]"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
    )
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", default="tts/tts-lines.json")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    project = Path(__file__).resolve().parent.parent
    spec_path = project / args.spec
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    segment_dir = project / "assets/audio/segments"
    entries: list[dict] = []
    measured_scenes: list[tuple[dict, list[tuple[dict, Path, float]]]] = []

    for scene in spec["scenes"]:
        measured = []
        for line in scene["lines"]:
            output = segment_dir / f"{line['id']}.mp3"
            if args.force or not output.exists():
                synthesize(
                    line["text"],
                    spec["speaker"],
                    spec["context"],
                    spec["speech_rate"],
                    output,
                )
            measured.append((line, output, duration(output)))

        measured_scenes.append((scene, measured))

    if spec.get("placement") == "continuous":
        cursor = float(spec.get("initial_padding", 0))
        gap = float(spec["inter_line_gap"])
        for scene, measured in measured_scenes:
            for line, output, measured_duration in measured:
                start = cursor
                end = start + measured_duration
                entries.append(
                    {
                        "id": line["id"],
                        "text": line["text"],
                        "path": str(output.relative_to(project)),
                        "duration": round(measured_duration, 3),
                        "start": round(start, 3),
                        "end": round(end, 3),
                        "scene": scene["id"],
                    }
                )
                cursor = end + gap

        last_voice_end = entries[-1]["end"] if entries else 0
        tail_padding = float(spec.get("tail_padding", 0))
        if last_voice_end + tail_padding > spec["timeline_duration"]:
            overrun = last_voice_end + tail_padding - spec["timeline_duration"]
            raise RuntimeError(
                f"continuous voice exceeds timeline by {overrun:.3f}s; increase speech rate"
            )
    else:
        for scene, measured in measured_scenes:
            spoken = sum(item[2] for item in measured)
            gaps = spec["inter_line_gap"] * max(0, len(measured) - 1)
            window = scene["end"] - scene["start"]
            slack = window - spoken - gaps
            if slack < 0:
                raise RuntimeError(
                    f"{scene['id']} voice exceeds its scene by {-slack:.3f}s; increase speech rate"
                )
            cursor = scene["start"] + slack / 2
            for index, (line, output, measured_duration) in enumerate(measured):
                start = cursor
                end = start + measured_duration
                entries.append(
                    {
                        "id": line["id"],
                        "text": line["text"],
                        "path": str(output.relative_to(project)),
                        "duration": round(measured_duration, 3),
                        "start": round(start, 3),
                        "end": round(end, 3),
                        "scene": scene["id"],
                    }
                )
                cursor = end
                if index < len(measured) - 1:
                    cursor += spec["inter_line_gap"]

    voiceover = project / "assets/audio/voiceover.wav"
    build_voiceover(entries, spec["timeline_duration"], voiceover)
    metadata = {
        "provider": spec["provider"],
        "speaker": spec["speaker"],
        "voice_label": spec["voice_label"],
        "speech_rate": spec["speech_rate"],
        "placement": spec.get("placement", "scene-centered"),
        "timeline_duration": spec["timeline_duration"],
        "last_voice_end": entries[-1]["end"] if entries else 0,
        "voiceover": str(voiceover.relative_to(project)),
        "lines": entries,
    }
    meta_path = project / "assets/audio/tts-meta.json"
    meta_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"generated {len(entries)} lines -> {voiceover.relative_to(project)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
