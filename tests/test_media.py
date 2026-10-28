import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import media


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class MediaIntegrationTests(unittest.TestCase):
    def test_synthetic_split_stitch_and_copied_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source, depth = folder / "source.mp4", folder / "depth.mp4"
            media.command(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=90x160:rate=10:duration=4.8", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=44100:duration=4.8",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", source])
            media.command(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "color=c=gray:s=90x160:r=10:d=4.8", "-c:v", "libx264", depth])
            for overlap in [.3, 0]:
                with self.subTest(overlap=overlap):
                    out = folder / ("media-" + str(overlap))
                    plan = media.prepare(source, depth, out, fps=10, reference_limit=4,
                                         output_limit=2, minimum=1, overlap=overlap)
                    self.assertEqual(plan["total_frames"], 48)
                    self.assertGreater(len(plan["segments"]), 1)
                    clips = [out / s["files"]["rgb"] for s in plan["segments"]]
                    audio = out / plan["original_audio"]
                    final = folder / ("final-" + str(overlap) + ".mp4")
                    result = media.stitch(out / "manifest.json", clips, final, source_audio=audio)
                    self.assertEqual(result["frames"], 48)
                    verified = media.verify(final, audio)
                    self.assertTrue(verified["audio_stream_identical"])
                    self.assertEqual(verified["full_video_decode"], "PASS")

    def test_depth_duration_mismatch_is_not_silently_stretched(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            with patch("media.probe", side_effect=[{"duration": 10}, {"duration": 7}]):
                with self.assertRaisesRegex(ValueError, "align"):
                    media.prepare("source", "depth", Path(temp) / "out")

    def test_zero_overlap_with_rounded_container_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            clip = folder / "clip.mp4"
            media.command(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "color=c=gray:s=90x160:r=24", "-frames:v", "152", "-c:v", "libx264", clip])
            plan = media.plan_frames(19, fps=24, overlap=0)
            self.assertEqual([s["frame_count"] for s in plan["segments"]], [152, 152, 152])
            manifest = folder / "manifest.json"
            manifest.write_text(json.dumps(plan))
            original_probe = media.probe
            def rounded_probe(path):
                result = original_probe(path)
                if Path(path) == clip:
                    result["duration"] = 6.333008
                return result
            with patch("media.probe", side_effect=rounded_probe):
                result = media.stitch(manifest, [clip, clip, clip], folder / "final.mp4")
            self.assertEqual(result["frames"], 456)
            self.assertAlmostEqual(result["duration"], 19, places=2)


if __name__ == "__main__":
    unittest.main()
