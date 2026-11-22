from dataclasses import dataclass
from uuid import UUID, uuid4

from .auth import APPROVER, OPERATOR, Denied, authorize
from .config import ENVIRONMENTS, REGIONS, positive
from .github import RemoteError
from .store import Conflict

WORKFLOW = {"deploy": "release.yml", "rollback": "restore.yml", "retire": "demo-retire.yml"}


class InvalidAction(Exception):
    pass


def review_claim(run_id, environment):
    return f"review:{run_id}:{environment}"


@dataclass(frozen=True)
class Command:
    action: str
    request_id: str
    source_run_id: int | None = None
    region: str | None = None
    profile: str | None = None

    def __post_init__(self):
        try:
            if str(UUID(self.request_id)) != self.request_id:
                raise ValueError
            if self.action == "deploy":
                valid = self.profile in ("demo", "production") and self.source_run_id is None and self.region is None
            elif self.action in ("rollback", "retire", "cancel"):
                positive(self.source_run_id)
                valid = self.profile is None and (self.region is None if self.action == "cancel" else self.region in REGIONS)
            else:
                valid = False
            if not valid:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise InvalidAction("Invalid operation target") from None


class Operations:
    def __init__(self, store, github, reader):
        self.store, self.github, self.reader = store, github, reader

    def request(self, actor, command):
        authorize(actor, OPERATOR)
        key = command.request_id
        row = {"kind": command.action, "actor": actor.object_id, "actor_name": actor.display_name,
               "tenant": actor.tenant_id}
        claim = f"cancel:{command.source_run_id}" if command.action == "cancel" else None
        if claim:
            row["claim"] = claim
        row.update({name: getattr(command, name) for name in ("source_run_id", "region", "profile")
                    if getattr(command, name) is not None})
        self.store.reserve(key, row)
        try:
            if claim and self.store.claimed(claim):
                raise Conflict("A cancellation was already accepted")
            self.reader.check(command)
        except Exception:
            self.store.finish(key, {"status": "blocked"})
            raise

        def send():
            if command.action == "cancel":
                self.github.cancel(command.source_run_id)
                return {}
            inputs = ({"profile": command.profile} if command.action == "deploy" else
                      {"source_run_id": str(command.source_run_id), "region": command.region})
            run_id = self.github.dispatch(WORKFLOW[command.action], inputs)
            return {"run_id": run_id}

        return self._write(key, send)

    def review(self, actor, operation_id, environment, approved):
        authorize(actor, APPROVER)
        try:
            if environment not in ENVIRONMENTS or str(UUID(operation_id)) != operation_id or type(approved) is not bool:
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise InvalidAction("Invalid approval target") from None
        source = self.store.get(operation_id)
        if not source or source.get("status") != "accepted" or not source.get("run_id"):
            raise InvalidAction("The operation cannot be verified")
        if actor.tenant_id != source["tenant"] or source["actor"] == actor.object_id:
            raise Denied("A different Entra approver is required")
        run_id = source["run_id"]
        claim = review_claim(run_id, environment)
        if self.store.claimed(claim):
            raise Conflict("A decision already exists")
        if not self.reader.gate(source, environment):
            raise InvalidAction("The environment is no longer waiting for this approval")
        key = str(uuid4())
        self.store.reserve(key, {"kind": "approve" if approved else "reject", "actor": actor.object_id,
                                 "actor_name": actor.display_name,
                                 "tenant": actor.tenant_id, "source_run_id": run_id, "environment": environment,
                                 "claim": claim})
        try:
            if self.store.claimed(claim):
                raise Conflict("A decision already exists")
            if not self.reader.gate(source, environment):
                raise InvalidAction("The environment is no longer waiting for this approval")
        except Exception:
            self.store.finish(key, {"status": "blocked"})
            raise

        def send():
            self.github.review_rule(run_id, environment, approved, "Decision recorded by the Entra deployment console.")
            return {}

        return self._write(key, send)

    def _write(self, key, send):
        try:
            result = {"status": "accepted", **send()}
        except RemoteError as error:
            result = {"status": "rejected" if error.status in (400, 401, 403, 404, 409, 422) else "confirmation_required"}
        except Exception:
            # A post-send exception must never lead to an automatic replay.
            result = {"status": "confirmation_required"}
        self.store.finish(key, result, release=result["status"] != "confirmation_required")
        return {"operation_id": key, **result}
