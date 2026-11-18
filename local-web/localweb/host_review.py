"""Allow an active local job to wait for a decision about freshly observed unknown keys."""

from threading import Condition
from time import monotonic
from uuid import uuid4


class HostKeyReview:
    def __init__(self):
        self.changed = Condition()
        self.pending = {}

    def ask(self, checks, cancel):
        if not checks or cancel.is_set():
            return []
        request_id = uuid4().hex
        offered = [
            {
                "endpoint": c.target.endpoint,
                "key_type": c.observation.key_type,
                "fingerprint": c.observation.fingerprint,
            }
            for c in checks
        ]
        request = {"id": request_id, "checks": offered, "answer": None}
        deadline = monotonic() + 300
        with self.changed:
            self.pending[request_id] = request
            try:
                while request["answer"] is None and not cancel.is_set():
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return []
                    self.changed.wait(min(remaining, 0.25))
                if cancel.is_set():
                    return []
                return [c for c in checks if c.target.endpoint in request["answer"]]
            finally:
                self.pending.pop(request_id, None)

    def snapshot(self):
        with self.changed:
            return [
                {"id": r["id"], "checks": r["checks"]}
                for r in self.pending.values()
                if r["answer"] is None
            ]

    def answer(self, body):
        with self.changed:
            request_id = body.get("requestId")
            req = self.pending.get(request_id) if isinstance(request_id, str) else None
            if req is None or req["answer"] is not None:
                raise ValueError("이미 종료된 호스트 키 검토입니다.")
            rows = body.get("approved")
            if not isinstance(rows, list) or len(rows) > len(req["checks"]):
                raise ValueError("승인할 호스트 키를 확인하세요.")
            offered = {c["endpoint"]: c["fingerprint"] for c in req["checks"]}
            accepted = set()
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("잘못된 호스트 키입니다.")
                endpoint = row.get("endpoint")
                if (
                    not isinstance(endpoint, str)
                    or endpoint not in offered
                    or row.get("fingerprint") != offered[endpoint]
                ):
                    raise ValueError("현재 확인한 지문과 일치하지 않습니다.")
                accepted.add(endpoint)
            req["answer"] = accepted
            self.changed.notify_all()
