import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import islice

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableServiceClient


def stamp():
    return datetime.now(timezone.utc).isoformat()


RECENT_SPAN = 10 ** 13
INDEX_MARKER = "meta:index-v1"


class Conflict(Exception):
    pass


def _is_conflict(error):
    return error.status_code in (409, 412)


class Inbox:
    def __init__(self, table, partition):
        self.table, self.partition = table, partition

    def put(self, rule):
        row = {"PartitionKey": self.partition, "RowKey": f"{rule.run_id}:{rule.environment}:{rule.delivery_id}",
               "environment": rule.environment, "sha": rule.sha, "received_at": stamp()}
        try:
            self.table.create_entity(row)
        except ResourceExistsError:
            return False
        return True

    def rules(self, run_id):
        return list(self.table.query_entities(
            "PartitionKey eq @p and RowKey ge @low and RowKey lt @high",
            parameters={"p": self.partition, "low": f"{run_id}:", "high": f"{run_id};"}))

    # Change hints live beside protection-rule rows under their own prefixes: `chgid:<delivery>` makes a
    # delivery insert-once, and `chg:<UTC time>:<delivery>` orders hints by arrival for the change cursor.
    def put_change(self, change):
        try:
            self.table.create_entity({"PartitionKey": self.partition, "RowKey": f"chgid:{change.delivery_id}"})
        except ResourceExistsError:
            return False
        received = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.table.create_entity({
            "PartitionKey": self.partition, "RowKey": f"chg:{received}:{change.delivery_id}",
            "delivery_id": change.delivery_id, "run_id": str(change.run_id), "run_attempt": change.run_attempt,
            "kind": change.kind, "action": change.action, "received_at": received})
        return True

    def changes(self, after="chg:", limit=200):
        """Change hints whose row key sorts after `after` (a `chg:` row key or `chg:<UTC time>`), oldest first."""
        rows = self.table.query_entities(
            "PartitionKey eq @p and RowKey gt @low and RowKey lt @high",
            parameters={"p": self.partition, "low": after, "high": "chg;"},
            select=["RowKey", "delivery_id", "run_id", "run_attempt", "kind", "action", "received_at"])
        result = []
        for row in rows:
            result.append({**row, "run_id": int(row["run_id"])})
            if len(result) >= limit:
                break
        return result


def _recent_key(created_at, operation_id):
    millis = int(datetime.fromisoformat(created_at).timestamp() * 1000)
    return f"recent:{RECENT_SPAN - millis:013d}:{operation_id}"


class OperationsTable:
    """Audit rows: op:<id> (authoritative), lock:<slot>, claim:<claim>, plus pointer indexes
    recent:<reverse millis>:<id> and run:<run_id> that hold only operation_id."""

    def __init__(self, table, partition, clock=time.time):
        self.table, self.partition, self.clock = table, partition, clock
        self.indexed = False

    def _entity(self, key, values):
        row = {**values, "PartitionKey": self.partition, "RowKey": key}
        for field in ("run_id", "source_run_id"):
            if field in row:
                row[field] = str(row[field])
        return row

    @staticmethod
    def _decode(row):
        if row is not None:
            for field in ("run_id", "source_run_id"):
                if field in row:
                    row[field] = int(row[field])
        return row

    def _get(self, key):
        try:
            return self.table.get_entity(self.partition, key)
        except ResourceNotFoundError:
            return None

    def get(self, operation_id):
        return self._decode(self._get(f"op:{operation_id}"))

    def reserve(self, operation_id, values, slot="write"):
        created = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        row = self._entity(f"op:{operation_id}", {**values, "operation_id": operation_id,
                                               "status": "reserved", "created_at": created})
        lock = self._entity(f"lock:{slot}", {"owner": operation_id})
        recent = self._entity(_recent_key(created, operation_id), {"operation_id": operation_id})
        try:
            self.table.submit_transaction([("create", row), ("create", lock), ("create", recent)])
        except HttpResponseError as error:
            if _is_conflict(error):
                raise Conflict("Operation already requested or another write is unresolved") from None
            raise
        return row

    def claimed(self, claim):
        return self._get(f"claim:{claim}") is not None

    def finish(self, operation_id, updates, slot="write", release=True):
        row = self.get(operation_id)
        lock = self._get(f"lock:{slot}")
        if row is None or lock is None or lock["owner"] != operation_id or row["status"] != "reserved":
            raise Conflict("Operation is no longer reserved")
        # The entity transaction keeps the audit update and lock release together.
        from azure.core import MatchConditions

        match = {"etag": row.metadata["etag"], "match_condition": MatchConditions.IfNotModified, "mode": "replace"}
        changes = [("update", self._entity(row["RowKey"], {**row, **updates, "updated_at": stamp()}), match)]
        if row.get("claim") and updates["status"] == "accepted":
            changes.append(("create", self._entity(f"claim:{row['claim']}", {"owner": operation_id})))
        if updates.get("run_id"):
            changes.append(("upsert", self._entity(f"run:{updates['run_id']}", {"operation_id": operation_id}),
                            {"mode": "replace"}))
        if release:
            changes.append(("delete", self._entity(f"lock:{slot}", {}),
                            {"etag": lock.metadata["etag"], "match_condition": MatchConditions.IfNotModified}))
        self.table.submit_transaction(changes)

    def _range(self, prefix, limit=None):
        rows = self.table.query_entities(
            "PartitionKey eq @p and RowKey ge @low and RowKey lt @high",
            parameters={"p": self.partition, "low": prefix, "high": prefix[:-1] + chr(ord(prefix[-1]) + 1)},
            **({"results_per_page": limit} if limit else {}))
        return list(islice(rows, limit)) if limit else list(rows)

    def _ensure_index(self):
        # Rows written before the indexes existed are indexed once; the marker stops later scans.
        if self.indexed or self._get(INDEX_MARKER) is not None:
            self.indexed = True
            return
        for row in self._range("op:"):
            keys = []
            try:
                keys.append(_recent_key(row["created_at"], row["operation_id"]))
            except (KeyError, TypeError, ValueError):
                pass
            if row.get("run_id"):
                keys.append(f"run:{row['run_id']}")
            for key in keys:
                try:
                    self.table.create_entity(self._entity(key, {"operation_id": row["operation_id"]}))
                except ResourceExistsError:
                    pass
        try:
            self.table.create_entity(self._entity(INDEX_MARKER, {"created_at": stamp()}))
        except ResourceExistsError:
            pass
        self.indexed = True

    def by_run(self, run_id):
        self._ensure_index()
        index = self._get(f"run:{run_id}")
        row = self.get(index["operation_id"]) if index else None
        return row if row and str(row.get("run_id")) == str(run_id) else None

    def recent(self, limit=25):
        self._ensure_index()
        ids = [row["operation_id"] for row in self._range("recent:", limit)]
        with ThreadPoolExecutor(max_workers=min(8, len(ids) or 1)) as pool:
            return [row for row in pool.map(self.get, ids) if row is not None]

    def unresolved(self):
        return self._get("lock:write") is not None


def table_clients(settings, credential):
    return TableServiceClient(endpoint=settings.table_endpoint, credential=credential, retry_total=0)
