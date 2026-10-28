"""HTTP-shape checks using local transports only."""
from pathlib import Path
import sys
import tempfile
import unittest

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from rh_api import APIError, Client, SubmissionUnknown


class Response:
    def __init__(self, data=None, status=200):
        self.data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("simulated HTTP error", response=self)

    def json(self):
        return self.data


class Transport:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class APITransportTests(unittest.TestCase):
    def test_ambiguous_create_is_never_retried(self):
        cases = [Transport(error=requests.Timeout("simulated timeout")),
                 Transport(Response(status=503)), Transport(Response({"code": 0, "data": {}})),
                 Transport(Response(None)), Transport(Response({"data": {}}))]
        for transport in cases:
            with self.subTest(transport=transport):
                client = Client(key="offline-test-only", transport=transport)
                with self.assertRaises(SubmissionUnknown):
                    client.create("123", {})
                self.assertEqual(len(transport.calls), 1)

    def test_known_rejection_is_distinct_from_unknown_submission(self):
        transport = Transport(Response({"code": 999, "msg": "simulated rejection"}))
        with self.assertRaises(APIError) as caught:
            Client(key="offline-test-only", transport=transport).create("123", {})
        self.assertNotIsInstance(caught.exception, SubmissionUnknown)
        self.assertEqual(len(transport.calls), 1)

    def test_current_upload_filename_shape(self):
        transport = Transport(Response({"code": 200, "data": {"filename": "input/offline.png"}}))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "image.png"
            path.write_bytes(b"offline-fixture")
            result = Client(key="offline-test-only", transport=transport).upload(path)
        self.assertEqual(result, "input/offline.png")
        url, kwargs = transport.calls[0]
        self.assertTrue(url.endswith("/openapi/v2/media/upload/binary"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer offline-test-only")


if __name__ == "__main__":
    unittest.main()
