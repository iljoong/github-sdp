from datetime import datetime, timezone

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableServiceClient


def stamp():
    return datetime.now(timezone.utc).isoformat()


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


class OperationsTable:
    def __init__(self, table, partition):
        self.table, self.partition = table, partition

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
        row = self._entity(f"op:{operation_id}", {**values, "operation_id": operation_id,
                                               "status": "reserved", "created_at": stamp()})
        lock = self._entity(f"lock:{slot}", {"owner": operation_id})
        try:
            self.table.submit_transaction([("create", row), ("create", lock)])
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
        if release:
            changes.append(("delete", self._entity(f"lock:{slot}", {}),
                            {"etag": lock.metadata["etag"], "match_condition": MatchConditions.IfNotModified}))
        self.table.submit_transaction(changes)

    def by_run(self, run_id):
        return self._decode(next(iter(self.table.query_entities(
            "PartitionKey eq @p and run_id eq @id", parameters={"p": self.partition, "id": str(run_id)})), None))

    def recent(self, limit=25):
        rows = self.table.query_entities(
            "PartitionKey eq @p and RowKey ge 'op:' and RowKey lt 'op;'",
            parameters={"p": self.partition})
        return [self._decode(row) for row in sorted(rows, key=lambda row: row["created_at"], reverse=True)[:limit]]

    def unresolved(self):
        return self._get("lock:write") is not None


def table_clients(settings, credential):
    return TableServiceClient(endpoint=settings.table_endpoint, credential=credential, retry_total=0)
