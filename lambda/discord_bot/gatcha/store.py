"""DynamoDB state. Tokens and message bodies are never stored here."""
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from .domain import Retryable


def conditional(error):
    return isinstance(error, ClientError) and error.response["Error"]["Code"] == "ConditionalCheckFailedException"


class Store:
    def __init__(self, table, client):
        self.table, self.client = table, client
        self.serializer = TypeSerializer()

    def get(self, pk, sk):
        return self.table.get_item(Key={"PK": pk, "SK": sk}, ConsistentRead=True).get("Item")

    def query(self, pk, *, latest=None):
        kwargs = {"KeyConditionExpression": "PK = :pk", "ExpressionAttributeValues": {":pk": pk}, "ConsistentRead": True}
        if latest is not None:
            kwargs.update(ScanIndexForward=False, Limit=latest)
        result = []
        while True:
            page = self.table.query(**kwargs)
            result.extend(page.get("Items", []))
            if latest is not None or not page.get("LastEvaluatedKey"):
                return result
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def put(self, pk, sk, **values):
        self.table.put_item(Item={"PK": pk, "SK": sk, **values})

    def put_revision(self, pk, sk, revision, **values):
        revision = str(revision).zfill(20)
        try:
            self.table.put_item(Item={"PK": pk, "SK": sk, **values, "revision": revision},
                                ConditionExpression="attribute_not_exists(#revision) OR #revision <= :revision",
                                ExpressionAttributeNames={"#revision": "revision"},
                                ExpressionAttributeValues={":revision": revision})
            return True
        except ClientError as error:
            if conditional(error):
                return False
            raise

    def disable_work(self, sk):
        try:
            self.table.update_item(Key={"PK": "WORKS", "SK": sk}, UpdateExpression="SET #active = :inactive",
                                   ConditionExpression="attribute_exists(PK)",
                                   ExpressionAttributeNames={"#active": "active"},
                                   ExpressionAttributeValues={":inactive": False})
        except ClientError as error:
            if not conditional(error):
                raise

    def update_job(self, job_id, **values):
        names = {f"#k{i}": key for i, key in enumerate(values)}
        expression_values = {f":v{i}": value for i, value in enumerate(values.values())}
        self.table.update_item(Key={"PK": f"JOB#{job_id}", "SK": "STATE"},
                               UpdateExpression="SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(values))),
                               ExpressionAttributeNames=names, ExpressionAttributeValues=expression_values)

    def claim(self, job_id, now):
        try:
            response = self.table.update_item(
                Key={"PK": f"JOB#{job_id}", "SK": "STATE"},
                UpdateExpression="SET #s = :running, leaseUntil = :lease",
                ConditionExpression="attribute_not_exists(PK) OR #s = :retry OR (#s = :running AND leaseUntil < :now)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":running": "RUNNING", ":retry": "RETRY", ":now": now, ":lease": now + 80},
                ReturnValues="ALL_NEW",
            )
            return response["Attributes"]
        except ClientError as error:
            if not conditional(error):
                raise
            return None

    def lock(self, guild, mode, job_id, now):
        try:
            self.table.put_item(Item={"PK": f"LOCK#{mode}#{guild}", "SK": "STATE", "owner": job_id, "until": now + 80},
                                ConditionExpression="attribute_not_exists(PK) OR #until < :now",
                                ExpressionAttributeNames={"#until": "until"}, ExpressionAttributeValues={":now": now})
        except ClientError as error:
            if conditional(error):
                raise Retryable("guild_busy") from None
            raise

    def unlock(self, guild, mode, job_id):
        try:
            self.table.delete_item(Key={"PK": f"LOCK#{mode}#{guild}", "SK": "STATE"},
                                   ConditionExpression="#owner = :owner", ExpressionAttributeNames={"#owner": "owner"},
                                   ExpressionAttributeValues={":owner": job_id})
        except ClientError as error:
            if not conditional(error):
                raise

    def cooldown(self, guild, job_id, now):
        try:
            self.table.update_item(Key={"PK": f"COOLDOWN#{guild}", "SK": "MANUAL"},
                                   UpdateExpression="SET nextAt = :next, jobId = :job",
                                   ConditionExpression="attribute_not_exists(PK) OR nextAt <= :now OR jobId = :job",
                                   ExpressionAttributeValues={":next": now + 30, ":now": now, ":job": job_id})
            return True
        except ClientError as error:
            if conditional(error):
                return False
            raise

    def complete_delivery(self, job_id, history):
        item = {"PK": f"HISTORY#{history['mode']}#{history['guild_id']}",
                "SK": f"{int(history['timestamp_ms']):016d}#{job_id}", **history}
        encode = lambda values: {key: self.serializer.serialize(value) for key, value in values.items()}
        self.client.transact_write_items(TransactItems=[
            {"Put": {"TableName": self.table.name, "Item": encode(item)}},
            {"Update": {"TableName": self.table.name, "Key": encode({"PK": f"JOB#{job_id}", "SK": "STATE"}),
                        "UpdateExpression": "SET #s = :done", "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": encode({":done": "SUCCESS"})}},
        ])
