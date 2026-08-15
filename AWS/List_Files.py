import json
import os
import boto3
from decimal import Decimal

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

TABLE_NAME = os.environ.get("TABLE_NAME", "FIT5225_A2_test")
URL_EXPIRES_IN = int(os.environ.get("URL_EXPIRES_IN", "300"))

table = dynamodb.Table(TABLE_NAME)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "http://127.0.0.1:3000",
    "Access-Control-Allow-Headers": "content-type,authorization",
    "Access-Control-Allow-Methods": "OPTIONS,GET",
}


def to_json_safe(obj):
    if isinstance(obj, list):
        return [to_json_safe(x) for x in obj]

    if isinstance(obj, dict):
        return {k: to_json_safe(v) for k, v in obj.items()}

    if isinstance(obj, Decimal):
        if obj % 1 == 0:
            return int(obj)
        return float(obj)

    return obj


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            **CORS_HEADERS,
            "Content-Type": "application/json",
        },
        "body": json.dumps(to_json_safe(body)),
    }


def get_claims(event):
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )


def add_presigned_urls(item):
    bucket = item.get("bucket")
    s3_key = item.get("s3_key")
    thumbnail_bucket = item.get("thumbnail_bucket") or bucket
    thumbnail_s3_key = item.get("thumbnail_s3_key")

    if bucket and s3_key:
        try:
            item["file_url"] = s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": bucket,
                    "Key": s3_key,
                },
                ExpiresIn=URL_EXPIRES_IN,
            )
        except Exception:
            item["file_url"] = None

    if thumbnail_bucket and thumbnail_s3_key:
        try:
            item["thumbnail_url"] = s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": thumbnail_bucket,
                    "Key": thumbnail_s3_key,
                },
                ExpiresIn=URL_EXPIRES_IN,
            )
        except Exception:
            item["thumbnail_url"] = None

    return item


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")

    if method == "OPTIONS":
        return response(200, {"message": "ok"})

    claims = get_claims(event)

    owner_sub = claims.get("sub")
    owner_email = claims.get("email", "")

    if not owner_sub:
        return response(401, {"message": "Missing Cognito user identity."})

    all_items = []
    scan_kwargs = {}

    while True:
        scan_result = table.scan(**scan_kwargs)

        for item in scan_result.get("Items", []):
            if item.get("owner_sub") == owner_sub:
                # --- 新增：将 tags 字典转换为列表 ---
                tags_data = item.get("tags", {})
                # 如果数据库里的 tags 是一个字典，就提取它所有的键(keys)变成一个列表
                if isinstance(tags_data, dict):
                    item["tags"] = list(tags_data.keys())
                # ---------------------------------
                
                all_items.append(add_presigned_urls(item))

        last_key = scan_result.get("LastEvaluatedKey")

        if not last_key:
            break

        scan_kwargs["ExclusiveStartKey"] = last_key

    all_items.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    return response(
        200,
        {
            "count": len(all_items),
            "owner_sub": owner_sub,
            "owner_email": owner_email,
            "files": all_items,
        },
    )