import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import media
from rh_api import SubmissionUnknown, APIError, Client, scrub
from rh_replace import Runner, compile_graph, fingerprint


PROMPT = """subject_definitions:
<Subject 1> is the replacement person in <Picture 1>.
summary:
[video editing] The target video edits <Video 1>.
retention_analysis:
<Subject 1>: fully_preserved - retain appearance.
detailed_description:
[Shot 1] Keep the original small steps and hand motion.
overall_soundscape:
Quiet ambience.
non_diegetic_music:
N/A
"""


def fixture(folder, count=2):
    graph = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
        "2": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
        "3": {"class_type": "VHS_LoadVideo", "inputs": {"video": "old.mp4"}},
        "4": {"class_type": "VHS_LoadVideo", "inputs": {"video": "old.mp4"}},
        "5": {"class_type": "PromptFixture", "inputs": {"text": "old"}},
        "6": {"class_type": "FloatFixture", "inputs": {"value": 4}},
        "7": {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {
            "prompt": ["5", 0], "ref_images.ref_image_8": ["10", 0]}},
        "9": {"class_type": "RenderFixture", "inputs": {"condition": ["7", 0], "duration": ["6", 0]}},
        "10": {"class_type": "LoadImage", "inputs": {"image": "stale.png"}}
    }
    (folder / "workflow.private.json").write_text(json.dumps(graph))
    for name in ["character.png", "keyframe.png", "rgb.mp4", "depth.mp4"]:
        (folder / name).write_bytes(("offline-fixture-" + name).encode())
    (folder / "prompt.txt").write_text(PROMPT)
    mapping = [("character", "1", "image", "LoadImage"), ("keyframe", "2", "image", "LoadImage"),
               ("rgb", "3", "video", "VHS_LoadVideo"), ("depth", "4", "video", "VHS_LoadVideo"),
               ("prompt", "5", "text", "PromptFixture"), ("duration", "6", "value", "FloatFixture")]
    return {"_base": folder, "workflow_id": "123", "workflow_file": "workflow.private.json",
            "state_dir": "runtime", "concurrency": 2, "poll_seconds": .001,
            "validate_media": False, "validate_outputs": False,
            "bindings": {r: {"node": n, "field": f, "class_type": c} for r, n, f, c in mapping},
            "t8_conditioning_nodes": ["7"], "output_nodes": ["9"],
            "character": "character.png", "segments": [
                {"id": f"S{i+1:03d}", "keyframe": "keyframe.png", "rgb": "rgb.mp4",
                 "depth": "depth.mp4", "prompt_file": "prompt.txt", "duration_seconds": 4}
                for i in range(count)]}


class Service:
    def __init__(self, simultaneous=False, unknown=False, poll_error=False):
        self.lock = threading.Lock()
        self.creates = self.active = self.peak = 0
        self.calls = {}
        self.barrier = threading.Barrier(2) if simultaneous else None
        self.unknown, self.poll_error = unknown, poll_error

    def client(self):
        service = self
        class Fake:
            key = "offline-test-only"
            def upload(self, path):
                return "input/" + Path(path).name
            def create(self, *args):
                with service.lock:
                    service.creates += 1
                    task = str(service.creates)
                    service.active += 1
                    service.peak = max(service.peak, service.active)
                if service.unknown:
                    raise SubmissionUnknown("offline simulated lost response")
                return task
            def status(self, task):
                with service.lock:
                    if service.poll_error:
                        service.poll_error = False
                        raise APIError("offline simulated tracking error")
                    call = service.calls.get(task, 0)
                    service.calls[task] = call + 1
                if call == 0:
                    if service.barrier:
                        service.barrier.wait(timeout=5)
                    return "RUNNING"
                with service.lock:
                    if call == 1:
                        service.active -= 1
                return "SUCCESS"
            def outputs(self, task):
                return [{"nodeId": "9", "fileType": "mp4", "fileUrl": "https://example.invalid/fixture.mp4",
                         "consumeCoins": "1"}]
            def download(self, url, path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"offline-video-fixture")
        return Fake()


class PlannerTests(unittest.TestCase):
    def test_legal_coverage_and_combined_reference_cap(self):
        for duration in [1, 4, 7.3, 7.5, 18.78, 31, 89, 180]:
            plan = media.plan_frames(duration, tracks=2, modulus=17, remainder=5)
            covered = 0
            previous = None
            for item in plan["segments"]:
                self.assertEqual(item["frame_count"] % 17, 5)
                self.assertLessEqual(item["reference_seconds"], 15)
                self.assertLessEqual(item["start_frame"], covered)
                if previous is not None:
                    self.assertGreater(item["start_frame"], previous)
                covered = item["start_frame"] + item["frame_count"]
                previous = item["start_frame"]
            self.assertGreaterEqual(covered, plan["total_frames"])

    def test_impossible_policy_fails(self):
        with self.assertRaises(ValueError):
            media.plan_frames(20, tracks=4, minimum=4)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.cfg = fixture(self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def test_adapter_removes_stale_references_without_assuming_switch_65(self):
        graph = compile_graph(self.cfg, self.cfg["segments"][0])
        self.assertNotIn("10", graph)
        self.assertNotIn("ref_images.ref_image_8", graph["7"]["inputs"])
        self.assertEqual(graph["7"]["inputs"]["ref_videos.ref_video_1"], ["4", 0])
        self.assertNotIn("65", graph)

    def test_wrong_node_class_fails_before_submission(self):
        self.cfg["bindings"]["character"]["class_type"] = "Incorrect"
        with self.assertRaises(ValueError):
            compile_graph(self.cfg, self.cfg["segments"][0])

    def test_unfilled_prompt_template_fails_before_submission(self):
        (self.folder / "prompt.txt").write_text(PROMPT + "\n{{SEGMENT_ACTIONS}}")
        service = Service()
        with self.assertRaisesRegex(ValueError, "placeholders"):
            Runner(self.cfg, service.client).run()
        self.assertEqual(service.creates, 0)

    def test_disconnected_binding_fails_before_submission(self):
        self.cfg["t8_conditioning_nodes"] = []
        with self.assertRaisesRegex(ValueError, "disconnected"):
            compile_graph(self.cfg, self.cfg["segments"][0])

    def test_zero_concurrency_is_rejected(self):
        with self.assertRaises(ValueError):
            Runner(self.cfg, Service().client).run(concurrency=0)

    def test_reference_budget_counts_both_tracks(self):
        self.cfg["validate_media"] = True
        self.cfg["segments"][0]["duration_seconds"] = 8
        with patch("media.probe", return_value={"duration": 8}):
            with self.assertRaisesRegex(ValueError, "Combined"):
                compile_graph(self.cfg, self.cfg["segments"][0])

    def test_two_lifecycle_workers_and_idempotent_resume(self):
        service = Service(simultaneous=True)
        first = Runner(self.cfg, service.client).run()
        self.assertTrue(all(s == "SUCCESS" for s in first.values()))
        self.assertEqual(service.peak, 2)
        second = Runner(self.cfg, service.client).run()
        self.assertTrue(all(s == "CACHED" for s in second.values()))
        self.assertEqual(service.creates, 2)

    def test_unknown_submission_is_not_automatically_repeated(self):
        cfg = copy.deepcopy(self.cfg); cfg["segments"] = cfg["segments"][:1]
        service = Service(unknown=True)
        Runner(cfg, service.client).run()
        with self.assertRaises(SubmissionUnknown):
            Runner(cfg, service.client).run()
        self.assertEqual(service.creates, 1)
        state = json.loads((self.folder / "runtime/state.private.json").read_text())
        self.assertEqual(state["jobs"]["S001"]["status"], "SUBMISSION_UNKNOWN")

    def test_poll_failure_resumes_same_task(self):
        cfg = copy.deepcopy(self.cfg); cfg["segments"] = cfg["segments"][:1]
        service = Service(poll_error=True)
        Runner(cfg, service.client).run()
        result = Runner(cfg, service.client).run()
        self.assertEqual(result["S001"], "SUCCESS")
        self.assertEqual(service.creates, 1)

    def test_changed_prompt_does_not_reuse_old_task(self):
        cfg = copy.deepcopy(self.cfg); cfg["segments"] = cfg["segments"][:1]
        service = Service()
        Runner(cfg, service.client).run()
        (self.folder / "prompt.txt").write_text(PROMPT.replace("small steps", "two slow steps"))
        with self.assertRaisesRegex(ValueError, "changed"):
            Runner(cfg, service.client).run()
        self.assertEqual(service.creates, 1)

    def test_execution_scope_changes_invalidate_fingerprint(self):
        segment = self.cfg["segments"][0]
        baseline = fingerprint(self.cfg, segment)
        for key, value in [("workflow_id", "456"), ("base_url", "https://www.runninghub.cn"),
                           ("instance_type", "plus"), ("fps", 30)]:
            cfg = copy.deepcopy(self.cfg); cfg[key] = value
            self.assertNotEqual(baseline, fingerprint(cfg, segment))
        with patch.dict("os.environ", {"RUNNINGHUB_API_KEY": "another-offline-test-key"}):
            self.assertNotEqual(baseline, fingerprint(self.cfg, segment))

    def test_upload_cache_is_scoped_to_region(self):
        service = Service()
        first = Runner(self.cfg, service.client)
        first.upload_files(service.client(), self.cfg["segments"][0])
        cfg = copy.deepcopy(self.cfg); cfg["base_url"] = "https://www.runninghub.cn"
        second = Runner(cfg, service.client)
        self.assertNotEqual(first.cache_scope, second.cache_scope)
        second.upload_files(service.client(), cfg["segments"][0])
        self.assertEqual(len(second.cache), 8)

    def test_tracking_error_does_not_free_cloud_slot_for_new_submission(self):
        cfg = fixture(self.folder, count=3)
        service = Service()
        barrier = threading.Barrier(2)
        hold_second = threading.Event()
        original = service.client
        def factory():
            client = original()
            def status(task):
                if task == "1":
                    barrier.wait(timeout=3)
                    raise APIError("simulated query failure while cloud task stays active")
                if task == "2":
                    barrier.wait(timeout=3)
                    hold_second.wait(timeout=.15)
                with service.lock:
                    service.active -= 1
                return "SUCCESS"
            client.status = status
            return client
        results = Runner(cfg, factory).run()
        self.assertLessEqual(service.peak, 2)
        self.assertEqual(service.creates, 2)
        self.assertEqual(results["S003"], "NOT_SUBMITTED")

    def test_unselected_unresolved_task_blocks_new_submission(self):
        runner = Runner(self.cfg, Service().client)
        runner.update("S001", task_id="123", status="RUNNING", fingerprint=fingerprint(self.cfg, self.cfg["segments"][0]))
        with self.assertRaisesRegex(ValueError, "unresolved"):
            runner.run(only=["S002"])

    def test_same_frame_count_does_not_hide_wrong_duration_or_fps(self):
        cfg = copy.deepcopy(self.cfg); cfg["segments"] = cfg["segments"][:1]
        cfg["segments"][0]["frame_count"] = 96
        cfg["validate_outputs"] = True
        service = Service()
        bad = {"frames": 96, "width": 90, "height": 160, "duration": 8, "fps": "12/1"}
        with patch("media.probe", return_value=bad):
            result = Runner(cfg, service.client).run()
        self.assertIn("Output duration differs", result["S001"])
        state = json.loads((self.folder / "runtime/state.private.json").read_text())
        self.assertNotIn("file_sha256", state["jobs"]["S001"])


class CredentialTests(unittest.TestCase):
    def test_rejects_non_provider_origins(self):
        with self.assertRaises(APIError):
            Client("https://example.invalid", key="offline-test-only")

    def test_redacts_key_and_signed_query(self):
        data = scrub({"apiKey": "offline-test-only", "msg": "offline-test-only",
                      "fileUrl": "https://cdn.invalid/x.mp4?signature=private"}, "offline-test-only")
        self.assertNotIn("offline-test-only", json.dumps(data))
        self.assertNotIn("signature=private", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
