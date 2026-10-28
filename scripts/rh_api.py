"""Small RunningHub Workflow API client. Generation submissions are never retried."""
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit
import requests


class APIError(RuntimeError):
    pass


class SubmissionUnknown(APIError):
    """A request may have been accepted. Reconcile task history before resubmitting."""


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def scrub(value, key=""):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if any(t in k.lower() for t in
                    ["apikey", "api_key", "authorization", "password", "secret", "netwssurl"])
                    else scrub(v, key)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    if isinstance(value, str):
        if key:
            value = value.replace(key, "[REDACTED]")
        # Suppress signed URL query strings in persisted/logged responses.
        def clean_url(match):
            u = urlsplit(match.group(0))
            return urlunsplit((u.scheme, u.netloc, u.path, "[REDACTED]" if u.query else "", ""))
        return re.sub(r"https?://[^\s\"<>]+", clean_url, value)
    return value


class Client:
    def __init__(self, base_url=None, key=None, transport=None):
        self.base = (base_url or os.environ.get("RUNNINGHUB_BASE_URL") or "https://www.runninghub.ai").rstrip("/")
        parsed = urlsplit(self.base)
        if parsed.scheme != "https" or parsed.netloc not in {"www.runninghub.ai", "www.runninghub.cn"} or parsed.path:
            raise APIError("Use the official HTTPS RunningHub .ai or .cn origin")
        self.key = (key or os.environ.get("RUNNINGHUB_API_KEY", "")).strip()
        if not self.key:
            raise APIError("Set RUNNINGHUB_API_KEY in the environment")
        self.http = transport or requests

    def post(self, endpoint, body, mutation=False):
        try:
            response = self.http.post(self.base + endpoint, json={"apiKey": self.key, **body},
                                      headers={"Authorization": "Bearer " + self.key}, timeout=(10, 45))
            if response.status_code >= 500 and mutation:
                raise SubmissionUnknown("Server error after submission; check RunningHub task history")
            response.raise_for_status()
            data = response.json()
        except SubmissionUnknown:
            raise
        except (requests.RequestException, ValueError) as error:
            message = str(scrub(str(error), self.key))
            if mutation and (not isinstance(error, requests.HTTPError) or
                             getattr(error.response, "status_code", 500) >= 500):
                raise SubmissionUnknown("Submission outcome unknown: " + message) from None
            raise APIError(message) from None
        if not isinstance(data, dict) or "code" not in data:
            if mutation:
                raise SubmissionUnknown("Submission returned an unrecognized response; reconcile task history")
            raise APIError("Unrecognized API response")
        if data.get("code") not in (0, 200):
            raise APIError(str(scrub({"code": data.get("code"), "message": data.get("msg", data.get("message"))}, self.key)))
        return data

    def workflow(self, workflow_id):
        result = self.post("/api/openapi/getJsonApiFormat", {"workflowId": workflow_id})
        value = result.get("data", {}).get("prompt")
        if not value:
            raise APIError("Workflow response contains no prompt graph")
        return json.loads(value) if isinstance(value, str) else value

    def upload(self, path):
        try:
            with Path(path).open("rb") as stream:
                response = self.http.post(self.base + "/openapi/v2/media/upload/binary",
                    headers={"Authorization": "Bearer " + self.key},
                    files={"file": (Path(path).name, stream)}, timeout=(10, 45))
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as error:
            raise APIError("Upload failed: " + str(scrub(str(error), self.key))) from None
        if data.get("code") not in (0, 200):
            raise APIError("Upload rejected: " + str(scrub(data.get("msg", data.get("message")), self.key)))
        info = data.get("data") or {}
        name = info.get("fileName") or info.get("filename")
        if not name:
            raise APIError("Upload response has no fileName/filename")
        return name

    def create(self, workflow_id, graph, instance="default"):
        result = self.post("/task/openapi/create", {"workflowId": workflow_id,
            "workflow": json.dumps(graph, ensure_ascii=False), "nodeInfoList": [],
            "instanceType": instance, "addMetadata": False}, mutation=True)
        info = result.get("data") or {}
        if not info.get("taskId"):
            raise SubmissionUnknown("Submission returned success without a taskId; reconcile task history")
        return str(info["taskId"])

    def status(self, task_id):
        result = self.post("/task/openapi/status", {"taskId": task_id})
        status = result.get("data")
        if not isinstance(status, str):
            raise APIError("Unexpected task status response")
        return status.upper()

    def outputs(self, task_id):
        result = self.post("/task/openapi/outputs", {"taskId": task_id})
        if not isinstance(result.get("data"), list):
            raise APIError("Task has no successful output list")
        return result["data"]

    def download(self, url, target):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise APIError("Only HTTPS output URLs without embedded credentials are accepted")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        # No RunningHub authentication header is forwarded to the CDN.
        response = self.http.get(url, stream=True, timeout=(10, 45))
        response.raise_for_status()
        with temporary.open("wb") as stream:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    stream.write(chunk)
        temporary.replace(target)
