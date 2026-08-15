import json
import os
import boto3
from decimal import Decimal

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

TABLE_NAME = os.environ.get("TABLE_NAME", "FIT5225_A2_test")
table = dynamodb.Table(TABLE_NAME)


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "http://127.0.0.1:3000",
            "Access-Control-Allow-Headers": "content-type,authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=decimal_default),
    }


def decimal_default(obj):
    if isinstance(obj, Decimal):
        if obj % 1 == 0:
            return int(obj)
        return float(obj)
    raise TypeError


def get_claims(event):
    """
    HTTP API + JWT Authorizer claims path:
    event["requestContext"]["authorizer"]["jwt"]["claims"]
    """
    try:
        return event.get("requestContext", {}) \
            .get("authorizer", {}) \
            .get("jwt", {}) \
            .get("claims", {}) or {}
    except Exception:
        return {}


def parse_body(event):
    body = event.get("body")

    if not body:
        return {}

    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")

    if isinstance(body, str):
        return json.loads(body)

    return body


def safe_delete_s3_object(bucket, key):
    """
    删除 S3 对象。
    如果 key 为空，直接跳过。
    如果对象不存在，S3 delete_object 本身也不会报错。
    """
    if not bucket or not key:
        return {
            "bucket": bucket,
            "key": key,
            "deleted": False,
            "reason": "missing bucket or key",
        }

    try:
        s3.delete_object(Bucket=bucket, Key=key)
        return {
            "bucket": bucket,
            "key": key,
            "deleted": True,
        }
    except Exception as e:
        return {
            "bucket": bucket,
            "key": key,
            "deleted": False,
            "reason": str(e),
        }


def lambda_handler(event, context):
    print("EVENT:", json.dumps(event))

    # CORS preflight
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return response(200, {"message": "OK"})

    claims = get_claims(event)
    owner_sub = claims.get("sub", "")
    owner_email = claims.get("email", "")

    if not owner_sub:
        return response(401, {
            "message": "Unauthorized: missing Cognito owner_sub"
        })

    try:
        body = parse_body(event)
    except Exception as e:
        return response(400, {
            "message": "Invalid JSON body",
            "error": str(e)
        })

    file_ids = body.get("file_ids", [])

    if not isinstance(file_ids, list) or len(file_ids) == 0:
        return response(400, {
            "message": "file_ids must be a non-empty list"
        })

    deleted = []
    skipped = []
    not_found = []
    errors = []

    for file_id in file_ids:
        if not file_id or not isinstance(file_id, str):
            skipped.append({
                "file_id": file_id,
                "reason": "invalid file_id"
            })
            continue

        try:
            # 1. 读取 DynamoDB item
            get_result = table.get_item(
                Key={
                    "file_id": file_id
                }
            )

            item = get_result.get("Item")

            if not item:
                not_found.append({
                    "file_id": file_id,
                    "reason": "DynamoDB item not found"
                })
                continue

            item_owner_sub = item.get("owner_sub", "")

            # 2. 用户隔离检查
            if item_owner_sub != owner_sub:
                skipped.append({
                    "file_id": file_id,
                    "reason": "not owner of this file"
                })
                continue

            bucket = item.get("bucket", "")
            s3_key = item.get("s3_key", "")

            thumbnail_bucket = item.get("thumbnail_bucket") or bucket
            thumbnail_s3_key = item.get("thumbnail_s3_key", "")

            s3_results = []

            # 3. 删除原文件
            if s3_key:
                s3_results.append(
                    safe_delete_s3_object(bucket, s3_key)
                )

            # 4. 删除缩略图
            if thumbnail_s3_key:
                s3_results.append(
                    safe_delete_s3_object(thumbnail_bucket, thumbnail_s3_key)
                )

            # 5. 删除 DynamoDB 记录
            table.delete_item(
                Key={
                    "file_id": file_id
                },
                ConditionExpression="owner_sub = :owner_sub",
                ExpressionAttributeValues={
                    ":owner_sub": owner_sub
                }
            )

            deleted.append({
                "file_id": file_id,
                "filename": item.get("filename", ""),
                "s3_key": s3_key,
                "thumbnail_s3_key": thumbnail_s3_key,
                "s3_delete_results": s3_results
            })

        except Exception as e:
            print("DELETE ERROR:", file_id, str(e))
            errors.append({
                "file_id": file_id,
                "error": str(e)
            })

    return response(200, {
        "message": "Delete operation completed",
        "owner_sub": owner_sub,
        "owner_email": owner_email,
        "requested_count": len(file_ids),
        "deleted_count": len(deleted),
        "skipped_count": len(skipped),
        "not_found_count": len(not_found),
        "error_count": len(errors),
        "deleted": deleted,
        "skipped": skipped,
        "not_found": not_found,
        "errors": errors
    })