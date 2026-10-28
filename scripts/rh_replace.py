#!/usr/bin/env python3
"""Portable CLI for inspected RunningHub character-replacement workflows."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from fractions import Fraction

import media
from rh_api import APIError, Client, SubmissionUnknown, atomic_json, digest, scrub


def load_project(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["_base"] = path.parent
    if not str(cfg.get("workflow_id", "")).isdigit():
        raise ValueError("Set your own numeric workflow_id")
    ids = [s["id"] for s in cfg.get("segments", [])]
    if not ids or len(ids) != len(set(ids)) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", s) for s in ids):
        raise ValueError("Segment ids must be unique safe filenames")
    return cfg


def local(cfg, value):
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (cfg["_base"] / path).resolve()


def prune(graph, outputs):
    keep = set()
    def visit(nid):
        nid = str(nid)
        if nid in keep:
            return
        if nid not in graph:
            raise ValueError("Missing workflow node " + nid)
        keep.add(nid)
        for value in graph[nid].get("inputs", {}).values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int):
                if value[0] in graph or value[0].isdigit():
                    visit(value[0])
    for nid in outputs:
        visit(nid)
    return {k: v for k, v in graph.items() if k in keep}


def prompt_check(text):
    if re.search(r"\{\{.*?\}\}", text, flags=re.S):
        raise ValueError("Replace all prompt template placeholders before submitting")
    fields = ["subject_definitions", "summary", "retention_analysis", "detailed_description",
              "overall_soundscape", "non_diegetic_music"]
    positions = []
    for field in fields:
        found = list(re.finditer(r"(?m)^" + field + r"\s*:", text))
        if len(found) != 1:
            raise ValueError("Prompt requires exactly one " + field + " section")
        positions.append(found[0].start())
    if positions != sorted(positions) or "[Shot 1]" not in text:
        raise ValueError("Prompt sections or Shot 1 marker are invalid")
    if text.count("<d>") != text.count("</d>"):
        raise ValueError("Unbalanced dialogue tags")


def segment_files(cfg, segment):
    result = {}
    for role in ["character", "keyframe", "rgb", "depth"]:
        value = segment.get(role, cfg.get(role))
        if value:
            path = local(cfg, value)
            if not path.is_file():
                raise ValueError(f"Missing {role} input: {path}")
            result[role] = path
    for required in ["character", "keyframe", "rgb"]:
        if required not in result:
            raise ValueError("Missing input role " + required)
    return result


def validate_inputs(cfg, segment, files):
    duration = float(segment["duration_seconds"])
    limits = cfg.get("limits", {})
    if not limits.get("output_min_seconds", 4) <= duration <= limits.get("output_max_seconds", 15):
        raise ValueError("Output duration violates configured limits")
    if cfg.get("validate_media", True):
        videos = [media.probe(files[r]) for r in ["rgb", "depth"] if r in files]
        fps = cfg.get("fps", 24)
        if any(abs(v["duration"] - duration) > 2 / fps for v in videos):
            raise ValueError("Video references are not synchronized to the segment duration")
        if any(v["duration"] < limits.get("reference_min_seconds", 2) for v in videos):
            raise ValueError("A reference clip is too short")
        if sum(v["duration"] for v in videos) > limits.get("reference_total_seconds", 15) + 1e-3:
            raise ValueError("Combined reference-video duration exceeds the configured limit")


def compile_graph(cfg, segment, uploads=None):
    graph = json.loads(local(cfg, cfg["workflow_file"]).read_text(encoding="utf-8"))
    if not isinstance(graph, dict) or not all(isinstance(n, dict) and "class_type" in n for n in graph.values()):
        raise ValueError("Use API-format workflow JSON, not a UI nodes/links export")
    files = segment_files(cfg, segment)
    validate_inputs(cfg, segment, files)
    prompt = local(cfg, segment["prompt_file"]).read_text(encoding="utf-8")
    prompt_check(prompt)
    values = {r: uploads[r] if uploads else f"LOCAL_INPUT:{r}:{digest(p)}" for r, p in files.items()}
    values.update(prompt=prompt, duration=float(segment["duration_seconds"]))
    bindings = cfg["bindings"]
    for role, value in values.items():
        if role not in bindings:
            raise ValueError("Configure a binding for " + role)
        binding = bindings[role]
        nid, field = str(binding["node"]), binding["field"]
        if nid not in graph or field not in graph[nid].get("inputs", {}):
            raise ValueError(f"Binding {role} points to a missing node/input")
        if binding.get("class_type") and graph[nid]["class_type"] != binding["class_type"]:
            raise ValueError("Node class mismatch for " + role)
        graph[nid]["inputs"][field] = value
    # Opt-in adapter for the inspected MiniMax T8 reference-input convention.
    for nid in cfg.get("t8_conditioning_nodes", []):
        nid = str(nid)
        if nid not in graph or graph[nid]["class_type"] != "MiniMaxH3AudioConditioningT8":
            raise ValueError("T8 conditioning adapter does not match this workflow")
        inputs = graph[nid]["inputs"]
        for field in list(inputs):
            if field.startswith("ref_images.") or field.startswith("ref_videos."):
                del inputs[field]
        inputs["ref_images.ref_image_0"] = [str(bindings["character"]["node"]), 0]
        inputs["ref_images.ref_image_1"] = [str(bindings["keyframe"]["node"]), 0]
        inputs["ref_videos.ref_video_0"] = [str(bindings["rgb"]["node"]), 0]
        if "depth" in files:
            inputs["ref_videos.ref_video_1"] = [str(bindings["depth"]["node"]), 0]
    for edit in cfg.get("overrides", []) + segment.get("overrides", []):
        nid, field = str(edit["node"]), edit["field"]
        if nid not in graph or field not in graph[nid].get("inputs", {}):
            raise ValueError("Override points to a missing node/input")
        if edit.get("class_type") and graph[nid]["class_type"] != edit["class_type"]:
            raise ValueError("Override node class mismatch")
        graph[nid]["inputs"][field] = copy.deepcopy(edit["value"])
    selected = prune(graph, [str(n) for n in cfg["output_nodes"]])
    for role in values:
        if str(bindings[role]["node"]) not in selected:
            raise ValueError("Binding is disconnected from selected output: " + role)
    return selected


def execution_scope(cfg):
    key = os.environ.get("RUNNINGHUB_API_KEY", "").strip()
    return {"workflow_id": str(cfg["workflow_id"]),
            "base_url": (cfg.get("base_url") or os.environ.get("RUNNINGHUB_BASE_URL") or "https://www.runninghub.ai").rstrip("/"),
            "instance_type": cfg.get("instance_type", "default"),
            "credential_hash": hashlib.sha256(key.encode()).hexdigest() if key else None}


def fingerprint(cfg, segment):
    payload = {"graph": compile_graph(cfg, segment), "scope": execution_scope(cfg),
               "fps": cfg.get("fps", 24), "frame_count": segment.get("frame_count"),
               "output_dimensions": cfg.get("output_dimensions")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Runner:
    def __init__(self, cfg, client_factory=None):
        self.cfg = cfg
        self.folder = local(cfg, cfg.get("state_dir", "work/runninghub"))
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state_path = self.folder / "state.private.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"jobs": {}}
        self.cache_path = self.folder / "uploads.private.json"
        self.cache = json.loads(self.cache_path.read_text()) if self.cache_path.exists() else {}
        self.cache_scope = hashlib.sha256(json.dumps(execution_scope(cfg), sort_keys=True).encode()).hexdigest()
        self.lock, self.upload_lock = threading.RLock(), threading.Lock()
        self.stop = threading.Event()
        self.factory = client_factory or (lambda: Client(cfg.get("base_url")))

    def update(self, sid, **fields):
        with self.lock:
            self.state["jobs"].setdefault(sid, {}).update(fields)
            atomic_json(self.state_path, self.state)

    def upload_files(self, client, segment):
        uploads = {}
        for role, path in segment_files(self.cfg, segment).items():
            hashed = self.cache_scope + ":" + digest(path)
            # Serializing short uploads deduplicates the shared character sheet;
            # generation workers retain separate slots for their full lifecycle.
            with self.upload_lock:
                if hashed not in self.cache:
                    self.cache[hashed] = client.upload(path)
                    atomic_json(self.cache_path, self.cache)
                uploads[role] = self.cache[hashed]
        return uploads

    def work(self, segment, retry_failed=False):
        sid, client = segment["id"], self.factory()
        fp = fingerprint(self.cfg, segment)
        with self.lock:
            previous = copy.deepcopy(self.state["jobs"].get(sid, {}))
        if previous and previous.get("fingerprint") != fp:
            raise ValueError(sid + ": inputs changed; use a new segment id/state directory")
        if previous.get("status") in ["SUBMITTING", "SUBMISSION_UNKNOWN"]:
            raise SubmissionUnknown(sid + ": reconcile task history with attach before resuming")
        if previous.get("status") == "SUCCESS" and Path(previous.get("file", "")).is_file():
            if digest(previous["file"]) != previous.get("file_sha256"):
                raise ValueError(sid + ": downloaded output changed on disk")
            return sid, "CACHED"
        if previous.get("status") == "FAILED" and not retry_failed:
            return sid, "FAILED (use --retry-failed only after fixing the cause)"
        task_id = previous.get("task_id")
        if previous.get("status") == "FAILED" and retry_failed:
            self.update(sid, task_id=None, previous_task_id=task_id)
            task_id = None
        if not task_id:
            if self.stop.is_set():
                return sid, "NOT_SUBMITTED"
            uploads = self.upload_files(client, segment)
            graph = compile_graph(self.cfg, segment, uploads)
            if self.stop.is_set():
                return sid, "NOT_SUBMITTED"
            atomic_json(self.folder / f"workflow-{sid}.private.json", graph)
            self.update(sid, fingerprint=fp, status="SUBMITTING")
            try:
                task_id = client.create(str(self.cfg["workflow_id"]), graph, self.cfg.get("instance_type", "default"))
            except SubmissionUnknown as error:
                self.update(sid, status="SUBMISSION_UNKNOWN", error=str(scrub(str(error), getattr(client, "key", ""))))
                self.stop.set()
                raise
            except APIError as error:
                self.update(sid, status="FAILED", error=str(error))
                raise
            self.update(sid, task_id=task_id, status="SUBMITTED")
        deadline = time.monotonic() + self.cfg.get("timeout_seconds", 2400)
        last = None
        try:
            while time.monotonic() < deadline:
                status = client.status(task_id)
                self.update(sid, status=status)
                if status != last:
                    print(sid, status, flush=True)
                    last = status
                if status == "FAILED":
                    return sid, status
                if status == "SUCCESS":
                    break
                if status not in ["RUNNING", "QUEUED", "PENDING", "SUBMITTED"]:
                    raise APIError("Unexpected task state; task id preserved for reconciliation")
                time.sleep(self.cfg.get("poll_seconds", 15))
            else:
                self.update(sid, status="TRACKING_TIMEOUT")
                raise APIError(sid + ": tracking timed out; resume to poll the same task")
        except Exception:
            # The cloud task may still occupy a slot. Do not replace it with a new submission.
            self.stop.set()
            raise
        outputs = client.outputs(task_id)
        allowed = {str(n) for n in self.cfg["output_nodes"]}
        videos = [o for o in outputs if str(o.get("nodeId")) in allowed and o.get("fileType") in ["mp4", "video"]]
        if len(videos) != 1:
            raise APIError("Expected one selected video output; inspect the task before downloading")
        target = self.folder / "generated" / f"{sid}.mp4"
        client.download(videos[0]["fileUrl"], target)
        if self.cfg.get("validate_outputs", True):
            info = media.probe(target)
            expected = segment.get("frame_count")
            if expected and info["frames"] != expected:
                raise APIError("Output frame count differs; inspect before stitching")
            fps = self.cfg.get("fps", 24)
            if abs(info["duration"] - float(segment["duration_seconds"])) > 1 / fps + 1e-3:
                raise APIError("Output duration differs; inspect before stitching")
            try:
                actual_fps = float(Fraction(info["fps"]))
            except (ValueError, ZeroDivisionError, TypeError):
                raise APIError("Output frame rate is unknown; inspect before stitching") from None
            if abs(actual_fps - fps) > .01:
                raise APIError("Output frame rate differs; inspect before stitching")
            dimensions = self.cfg.get("output_dimensions")
            if dimensions and list(dimensions) != [info["width"], info["height"]]:
                raise APIError("Output dimensions differ from the selected quality profile")
        costs = {k: videos[0].get(k) for k in ["consumeCoins", "consumeMoney", "taskCostTime"]}
        self.update(sid, status="SUCCESS", file=str(target), file_sha256=digest(target), costs=costs)
        return sid, "SUCCESS"

    def run(self, only=None, concurrency=None, retry_failed=False):
        workers = concurrency if concurrency is not None else self.cfg.get("concurrency", 1)
        if not isinstance(workers, int) or not 1 <= workers <= 64:
            raise ValueError("Concurrency must be an integer from 1 to 64 and fit your account entitlement")
        wanted = set(only or [])
        available = {s["id"] for s in self.cfg["segments"]}
        if wanted - available:
            raise ValueError("Unknown segment id in --only")
        jobs = [s for s in self.cfg["segments"] if not wanted or s["id"] in wanted]
        selected = {s["id"] for s in jobs}
        unresolved = {sid for sid, job in self.state["jobs"].items()
                      if job.get("status") in ["SUBMITTING", "SUBMISSION_UNKNOWN"] or
                      (job.get("task_id") and job.get("status") not in ["SUCCESS", "FAILED"])}
        if unresolved - selected:
            raise ValueError("Include unresolved tasks when resuming: " + ", ".join(sorted(unresolved - selected)))
        if len(unresolved) > workers:
            raise ValueError("Tracked tasks exceed requested concurrency; reconcile them before new submissions")
        if any(self.state["jobs"][sid].get("status") in ["SUBMITTING", "SUBMISSION_UNKNOWN"] for sid in unresolved):
            raise SubmissionUnknown("Reconcile ambiguous submissions with attach before resuming")
        jobs.sort(key=lambda job: job["id"] not in unresolved)
        # Preflight every selected job before any paid submission.
        for job in jobs:
            fp = fingerprint(self.cfg, job)
            previous = self.state["jobs"].get(job["id"])
            if previous and previous.get("fingerprint") != fp:
                raise ValueError(job["id"] + ": inputs or execution scope changed; use a new segment id/state directory")
        results = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.work, s, retry_failed): s["id"] for s in jobs}
            for future in as_completed(futures):
                sid = futures[future]
                try:
                    _, results[sid] = future.result()
                except Exception as error:
                    results[sid] = "ERROR: " + str(scrub(str(error), os.environ.get("RUNNINGHUB_API_KEY", "")))
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    p = subs.add_parser("probe"); p.add_argument("video")
    p = subs.add_parser("prepare")
    p.add_argument("--source", required=True); p.add_argument("--depth"); p.add_argument("--out", required=True)
    p.add_argument("--fps", type=int, default=24); p.add_argument("--reference-limit", type=float, default=15)
    p.add_argument("--output-limit", type=float, default=15); p.add_argument("--overlap", type=float, default=1)
    p.add_argument("--minimum", type=float, default=4); p.add_argument("--modulus", type=int, default=1)
    p.add_argument("--remainder", type=int, default=0); p.add_argument("--depth-tail-tolerance", type=float, default=.25)
    p = subs.add_parser("fetch-workflow")
    p.add_argument("--workflow-id", required=True); p.add_argument("--base-url"); p.add_argument("--out", default="work/workflow.private.json")
    p = subs.add_parser("inspect-workflow"); p.add_argument("workflow")
    for name in ["compile", "run"]:
        p = subs.add_parser(name); p.add_argument("project"); p.add_argument("--only", nargs="+")
        if name == "run":
            p.add_argument("--concurrency", type=int); p.add_argument("--retry-failed", action="store_true")
    p = subs.add_parser("attach"); p.add_argument("project"); p.add_argument("--segment", required=True); p.add_argument("--task-id", required=True)
    p = subs.add_parser("stitch"); p.add_argument("manifest"); p.add_argument("--clips", nargs="+", required=True)
    p.add_argument("--out", required=True); p.add_argument("--cuts", nargs="*", type=int); p.add_argument("--audio")
    p = subs.add_parser("verify"); p.add_argument("video"); p.add_argument("--audio")
    args = parser.parse_args()
    if args.action == "probe":
        result = media.probe(args.video)
    elif args.action == "prepare":
        result = media.prepare(args.source, args.depth, args.out, fps=args.fps, reference_limit=args.reference_limit,
            output_limit=args.output_limit, overlap=args.overlap, minimum=args.minimum,
            modulus=args.modulus, remainder=args.remainder, depth_tail_tolerance=args.depth_tail_tolerance)
    elif args.action == "fetch-workflow":
        if not str(args.out).endswith(".private.json"):
            raise ValueError("Save fetched workflows with a .private.json suffix; they may contain credentials")
        graph = Client(args.base_url).workflow(args.workflow_id)
        atomic_json(args.out, graph)
        result = {"saved": str(args.out), "nodes": len(graph)}
    elif args.action == "inspect-workflow":
        graph = json.loads(Path(args.workflow).read_text(encoding="utf-8"))
        result = scrub(graph, os.environ.get("RUNNINGHUB_API_KEY", ""))
    elif args.action in ["compile", "run"]:
        cfg = load_project(args.project)
        if args.action == "compile":
            if set(args.only or []) - {s["id"] for s in cfg["segments"]}:
                raise ValueError("Unknown segment id in --only")
            folder = local(cfg, cfg.get("state_dir", "work/runninghub"))
            result = {}
            for segment in cfg["segments"]:
                if args.only and segment["id"] not in args.only:
                    continue
                path = folder / f"dry-{segment['id']}.private.json"
                atomic_json(path, compile_graph(cfg, segment))
                result[segment["id"]] = str(path)
        else:
            result = Runner(cfg).run(args.only, args.concurrency, args.retry_failed)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if all(s in ["SUCCESS", "CACHED"] for s in result.values()) else 1
    elif args.action == "attach":
        cfg = load_project(args.project); runner = Runner(cfg)
        segment = next((s for s in cfg["segments"] if s["id"] == args.segment), None)
        if segment is None or not args.task_id.isdigit():
            raise ValueError("Unknown segment or invalid task id")
        existing = runner.state["jobs"].get(args.segment, {})
        if existing.get("task_id") and existing["task_id"] != args.task_id:
            raise ValueError("Segment already tracks another task; use a new state directory")
        runner.update(args.segment, fingerprint=fingerprint(cfg, segment), task_id=args.task_id, status="SUBMITTED")
        result = {"attached": args.segment, "task_id": args.task_id}
    elif args.action == "stitch":
        result = media.stitch(args.manifest, args.clips, args.out, args.cuts, args.audio)
    else:
        result = media.verify(args.video, args.audio)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, APIError, OSError, KeyError) as error:
        print("ERROR:", scrub(str(error), os.environ.get("RUNNINGHUB_API_KEY", "")))
        raise SystemExit(1)
