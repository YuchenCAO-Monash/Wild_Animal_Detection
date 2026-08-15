import json
import os
import boto3
from decimal import Decimal

dynamodb = boto3.resource("dynamodb")

TABLE_NAME = os.environ.get("TABLE_NAME", "FIT5225_A2_test")
table = dynamodb.Table(TABLE_NAME)


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "http://127.0.0.1:3000",
            "Access-Control-Allow-Headers": "content-type,authorization",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
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
    HTTP API + Cognito JWT Authorizer claims path:
    event["requestContext"]["authorizer"]["jwt"]["claims"]
    """
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )


def normalize_tag(tag):
    """
    手动 tag 统一处理：
    - 去掉前后空格
    - 转小写
    - 多个空格合并
    """
    if not isinstance(tag, str):
        return ""

    tag = tag.strip().lower()
    tag = " ".join(tag.split())
    return tag


def normalize_tags_input(tags):
    """
    支持：
    "kangaroo"
    ["kangaroo", "koala"]
    """
    if isinstance(tags, str):
        tags = [tags]

    if not isinstance(tags, list):
        return []

    cleaned = []
    seen = set()

    for tag in tags:
        t = normalize_tag(tag)
        if t and t not in seen:
            cleaned.append(t)
            seen.add(t)

    return cleaned


def ensure_tags_map(value):
    """
    DynamoDB 中 tags 正常应该是：
    {
      "kangaroo": 1,
      "koala": 2
    }

    如果历史数据异常，也尽量兜底。
    """
    if isinstance(value, dict):
        return dict(value)

    if isinstance(value, list):
        result = {}
        for item in value:
            t = normalize_tag(item)
            if t:
                result[t] = Decimal(1)
        return result

    return {}


def lambda_handler(event, context):
    try:
        if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
            return response(200, {"message": "OK"})

        claims = get_claims(event)
        owner_sub = claims.get("sub", "")

        if not owner_sub:
            return response(401, {
                "message": "Unauthorized: missing Cognito sub"
            })

        body_raw = event.get("body") or "{}"

        if event.get("isBase64Encoded"):
            import base64
            body_raw = base64.b64decode(body_raw).decode("utf-8")

        try:
            body = json.loads(body_raw)
        except json.JSONDecodeError:
            return response(400, {
                "message": "Invalid JSON body"
            })

        file_ids = body.get("file_ids")
        operation = body.get("operation")
        tags_input = body.get("tags")

        if isinstance(file_ids, str):
            file_ids = [file_ids]

        if not isinstance(file_ids, list) or len(file_ids) == 0:
            return response(400, {
                "message": "file_ids must be a non-empty array"
            })

        file_ids = [str(x).strip() for x in file_ids if str(x).strip()]

        if len(file_ids) == 0:
            return response(400, {
                "message": "file_ids must contain valid file_id values"
            })

        if operation not in [0, 1]:
            return response(400, {
                "message": "operation must be 1 for add or 0 for remove"
            })

        tags = normalize_tags_input(tags_input)

        if len(tags) == 0:
            return response(400, {
                "message": "tags must be a non-empty string or array"
            })

        updated = []
        skipped = []
        not_found = []

        for file_id in file_ids:
            get_result = table.get_item(
                Key={
                    "file_id": file_id
                }
            )

            item = get_result.get("Item")

            if not item:
                not_found.append({
                    "file_id": file_id,
                    "reason": "not_found"
                })
                continue

            item_owner_sub = item.get("owner_sub", "")

            if item_owner_sub != owner_sub:
                skipped.append({
                    "file_id": file_id,
                    "reason": "not_owner"
                })
                continue

            current_tags = ensure_tags_map(item.get("tags", {}))

            if operation == 1:
                # 添加 tag：不存在则设置为 1，已存在则保持原来的 count
                for tag in tags:
                    if tag not in current_tags:
                        current_tags[tag] = Decimal(1)

            else:
                # 删除 tag：不存在直接忽略
                for tag in tags:
                    current_tags.pop(tag, None)

            table.update_item(
                Key={
                    "file_id": file_id
                },
                UpdateExpression="SET tags = :tags",
                ConditionExpression="owner_sub = :owner_sub",
                ExpressionAttributeValues={
                    ":tags": current_tags,
                    ":owner_sub": owner_sub,
                }
            )

            updated.append({
                "file_id": file_id,
                "tags": current_tags
            })

        return response(200, {
            "message": "Tags updated successfully",
            "operation": operation,
            "input_tags": tags,
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "not_found_count": len(not_found),
            "updated": updated,
            "skipped": skipped,
            "not_found": not_found,
        })

    except Exception as e:
        print("ERROR:", str(e))
        return response(500, {
            "message": "Internal server error",
            "error": str(e)
        })