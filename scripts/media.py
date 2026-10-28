"""Frame-accurate local media helpers. No cloud calls or credentials."""
import json
import math
from pathlib import Path
import shutil
import subprocess
from fractions import Fraction


def command(args):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr[-2000:] or "Media command failed")
    return result.stdout


def probe(path):
    data = json.loads(command(["ffprobe", "-v", "error", "-show_streams",
                               "-show_format", "-of", "json", path]))
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not video:
        raise ValueError("No video stream")
    duration = float(video.get("duration") or data["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Invalid video duration")
    return {"width": video["width"], "height": video["height"],
            "duration": duration, "fps": video.get("avg_frame_rate"),
            "frames": int(video["nb_frames"]) if video.get("nb_frames", "").isdigit() else None,
            "has_audio": any(s["codec_type"] == "audio" for s in data["streams"])}


def legal_up(n, modulus, remainder):
    return n + (remainder - n) % modulus


def legal_down(n, modulus, remainder):
    return n - (n - remainder) % modulus


def plan_frames(duration, fps=24, tracks=2, reference_limit=15, output_limit=15,
                overlap=1, minimum=4, modulus=1, remainder=0):
    """Balance legal segment lengths; all reference tracks count toward the cap.

    Frame modularity is an adapter setting, not an assumed H3 universal rule.
    A final hold is used only if legal rounding requires it.
    """
    values = [duration, reference_limit, output_limit, overlap, minimum]
    if any(not math.isfinite(v) for v in values):
        raise ValueError("All timing values must be finite")
    if duration <= 0 or fps <= 0 or tracks < 1 or modulus < 1:
        raise ValueError("Positive duration, fps, track count and modulus required")
    if not 0 <= remainder < modulus or overlap < 0 or minimum <= 0:
        raise ValueError("Invalid remainder, overlap or minimum")
    total = math.ceil(duration * fps - 1e-9)
    maximum = legal_down(math.floor(min(output_limit, reference_limit / tracks) * fps + 1e-9), modulus, remainder)
    smallest = legal_up(math.ceil(minimum * fps), modulus, remainder)
    ov = math.ceil(overlap * fps)
    if maximum < smallest or ov >= maximum:
        raise ValueError("Limits cannot fit a legal minimum clip and overlap")
    count = 1 if total <= maximum else math.ceil((total - ov) / (maximum - ov))
    while True:
        length = legal_up(max(smallest, math.ceil((total + (count - 1) * ov) / count)), modulus, remainder)
        if length <= maximum:
            break
        count += 1
    if count == 1:
        starts = [0]
    else:
        # Spread the total overlap to avoid a tiny/illegal final segment.
        starts = [round(i * (total - length) / (count - 1)) for i in range(count)]
    segments = []
    for i, start in enumerate(starts):
        segments.append({"id": f"S{i + 1:03d}", "start_frame": start,
                         "frame_count": length, "start_seconds": start / fps,
                         "duration_seconds": length / fps,
                         "reference_seconds": tracks * length / fps,
                         "tail_hold_frames": max(0, start + length - total)})
    return {"fps": fps, "total_frames": total, "duration_seconds": total / fps,
            "reference_tracks": tracks, "reference_limit_seconds": reference_limit,
            "frame_modulus": modulus, "frame_remainder": remainder,
            "segments": segments}


def prepare(source, depth, out, **options):
    out = Path(out).resolve()
    if (out / "manifest.json").exists():
        raise ValueError("Output already contains a manifest; choose another directory")
    metadata = probe(source)
    depth_metadata = probe(depth) if depth else None
    fps = options.get("fps", 24)
    tolerance = options.pop("depth_tail_tolerance", 0.25)
    if tolerance < 0:
        raise ValueError("Depth tail tolerance must be non-negative")
    if depth_metadata and abs(metadata["duration"] - depth_metadata["duration"]) > tolerance:
        raise ValueError("Depth duration differs beyond tolerance; inspect/align the timelines first")
    plan = plan_frames(metadata["duration"], tracks=2 if depth else 1, **options)
    plan["source_metadata"] = metadata
    plan["depth_metadata"] = depth_metadata
    plan["spatial_registration"] = "Not inferred. Inspect cropping/aspect ratio; native geometry retained."
    out.mkdir(parents=True, exist_ok=True)
    for segment in plan["segments"]:
        segment["files"] = {}
        for role, path in [("rgb", source), ("depth", depth)]:
            if not path:
                continue
            target = out / f"{segment['id']}_{role}.mp4"
            extra = segment["frame_count"] / fps + tolerance + 1
            vf = (f"fps={fps},tpad=stop_mode=clone:stop_duration={extra},"
                  f"trim=start_frame={segment['start_frame']}:end_frame={segment['start_frame'] + segment['frame_count']},"
                  "setpts=PTS-STARTPTS,setsar=1")
            command(["ffmpeg", "-v", "error", "-n", "-i", path, "-vf", vf, "-an",
                     "-c:v", "libx264", "-crf", "17", "-preset", "fast", "-pix_fmt", "yuv420p",
                     "-map_metadata", "-1", "-movflags", "+faststart", target])
            result = probe(target)
            if result["frames"] != segment["frame_count"]:
                raise ValueError("Unexpected segment frame count")
            segment["files"][role] = target.name
    if metadata["has_audio"]:
        # Matroska preserves AAC, MP3, PCM and other common original codecs.
        command(["ffmpeg", "-v", "error", "-n", "-i", source, "-vn", "-c:a", "copy",
                 "-map_metadata", "-1", out / "original-audio.mka"])
        plan["original_audio"] = "original-audio.mka"
    (out / "manifest.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan


def stitch(manifest_path, clips, output, cuts=None, source_audio=None, crf=16):
    """Cuts are global 0-based frame boundaries, one per adjacent pair."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    segments = manifest["segments"]
    fps, total = manifest["fps"], manifest["total_frames"]
    if len(clips) != len(segments):
        raise ValueError("Provide exactly one generated clip per planned segment")
    if cuts is None:
        cuts = [(a["start_frame"] + a["frame_count"] + b["start_frame"]) // 2
                for a, b in zip(segments, segments[1:])]
    if len(cuts) != len(segments) - 1 or list(cuts) != sorted(set(cuts)):
        raise ValueError("Cuts must be strictly increasing, one per overlap")
    for cut, a, b in zip(cuts, segments, segments[1:]):
        if not b["start_frame"] <= cut <= min(a["start_frame"] + a["frame_count"], total):
            raise ValueError("Cut lies outside the adjacent overlap")
    boundaries = [0] + list(cuts) + [total]
    args = ["ffmpeg", "-v", "error", "-n"]
    filters, dimensions = [], None
    for i, (segment, clip) in enumerate(zip(segments, clips)):
        info = probe(clip)
        current = (info["width"], info["height"])
        if dimensions is not None and current != dimensions:
            raise ValueError("Generated clips have different dimensions; inspect before stitching")
        dimensions = current
        start, end = boundaries[i] - segment["start_frame"], boundaries[i + 1] - segment["start_frame"]
        if start < 0 or end <= start or end > segment["frame_count"]:
            raise ValueError("Invalid selected interval")
        try:
            actual_fps = float(Fraction(info["fps"]))
        except (ValueError, ZeroDivisionError, TypeError):
            raise ValueError("Generated frame rate is unknown; inspect before stitching") from None
        if abs(actual_fps - fps) > .01:
            raise ValueError("Generated frame rate differs from the planned timeline")
        # Container duration may be rounded to milliseconds. Prefer the exact frame count.
        available = info["frames"] if info["frames"] is not None else info["duration"] * fps + .05
        if available < end:
            raise ValueError("Generated clip is too short for the selected interval")
        args += ["-i", clip]
        filters.append(f"[{i}:v]fps={fps},trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,setsar=1[v{i}]")
    filters.append("".join(f"[v{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[v]")
    if source_audio:
        args += ["-i", source_audio]
    args += ["-filter_complex", ";".join(filters), "-map", "[v]", "-c:v", "libx264",
             "-crf", str(crf), "-preset", "fast", "-pix_fmt", "yuv420p"]
    if source_audio:
        args += ["-map", f"{len(clips)}:a:0", "-c:a", "copy"]
    args += ["-map_metadata", "-1", "-movflags", "+faststart", output]
    command(args)
    result = probe(output)
    if result["frames"] != total:
        raise ValueError("Assembled video frame count does not match the planned timeline")
    return result


def audio_hash(path):
    return command(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-c:a", "copy",
                    "-f", "hash", "-hash", "sha256", "-"]).strip()


def verify(path, original_audio=None):
    info = probe(path)
    command(["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0", "-f", "null", "-"])
    result = {"video": info, "full_video_decode": "PASS"}
    if original_audio:
        result["audio_stream_identical"] = audio_hash(path) == audio_hash(original_audio)
        if not result["audio_stream_identical"]:
            raise ValueError("Original audio payload was not preserved")
    return result
