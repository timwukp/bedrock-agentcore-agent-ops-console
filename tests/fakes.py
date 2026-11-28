"""Shared fakes for the offline suites: S3 with conditional writes (the lease) and the reset broker.
They refuse what AWS refuses: IfNoneMatch='*' on an existing key and IfMatch on a stale ETag answer 412, a missing key
answers 404. Errors carry botocore's response shape, which harness_lock._lost reads."""
import itertools
import json


class FakeClientError(Exception):
    def __init__(self, code, status):
        super().__init__(f"{code} ({status})")
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeS3Lease:
    def __init__(self):
        self.objects = {}  # key -> (bytes, etag)
        self.events = []
        self._n = itertools.count(1)

    def get_object(self, Bucket, Key, **kw):
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey", 404)
        data, etag = self.objects[Key]
        return {"Body": _Body(data), "ETag": etag}

    def put_object(self, Bucket, Key, Body, **kw):
        cur = self.objects.get(Key)
        if kw.get("IfNoneMatch") == "*" and cur is not None:
            raise FakeClientError("PreconditionFailed", 412)
        if "IfMatch" in kw and (cur is None or cur[1] != kw["IfMatch"]):
            raise FakeClientError("PreconditionFailed", 412)
        etag = f'"e{next(self._n)}"'
        self.objects[Key] = (Body if isinstance(Body, bytes) else str(Body).encode(), etag)
        self.events.append(("put", Key, "create" if cur is None else "replace"))
        return {"ETag": etag}

    def delete_object(self, Bucket, Key, **kw):
        cur = self.objects.get(Key)
        if cur is None:
            raise FakeClientError("NoSuchKey", 404)
        if "IfMatch" in kw and cur[1] != kw["IfMatch"]:
            raise FakeClientError("PreconditionFailed", 412)
        del self.objects[Key]
        self.events.append(("delete", Key))
        return {}

    def lease(self, key="locks/ui-harness.json"):
        return json.loads(self.objects[key][0]) if key in self.objects else None


class FakeBroker:
    """A lambda client whose invoke() answers like the reset broker. `answers` is consumed in order; the default
    answer is ok. An answer of Exception raises (unreachable); a dict is the reply."""

    def __init__(self, answers=None, log=None):
        self.requests, self.answers, self.log = [], list(answers or []), log

    def invoke(self, FunctionName, InvocationType, Payload):
        req = json.loads(Payload)
        self.requests.append({"function": FunctionName, **req})
        if self.log is not None:
            self.log.append(("rotate", req.get("signOut")))
        ans = self.answers.pop(0) if self.answers else {"ok": True, "versionId": "v-new"}
        if isinstance(ans, Exception):
            raise ans
        return {"Payload": _Body(json.dumps(ans).encode())}
