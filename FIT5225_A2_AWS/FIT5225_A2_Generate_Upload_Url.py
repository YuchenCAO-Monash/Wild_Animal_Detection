import json
import os
import uuid
import boto3
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

BUCKET_NAME = os.environ.get("BUCKET_NAME", "fit5225-a2-test")
TABLE_NAME = os.environ.get("TABLE_NAME", "FIT5225_A2_test")
UPLOAD_PREFIX = os.environ.get("UPLOAD_PREFIX", "uploads/")
URL_EXPIRES_IN = int(os.environ.get("URL_EXPIRES_IN", "300"))

table = dynamodb.Table(TABLE_NAME)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "http://127.0.0.1:3000",
    "Access-Control-Allow-Headers": "content-type,authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            **CORS_HEADERS,
            "Content-Type": "application/json",
        },
        "body": json.dumps(body),
    }


def get_claims(event):
    authorizer = event.get("requestContext", {}).get("authorizer", {})

    claims = authorizer.get("claims")
    if claims:
        return claims

    return authorizer.get("jwt", {}).get("claims", {})


def safe_filename(filename):
    filename = filename.replace("\\", "_").replace("/", "_").strip()
    return filename or "upload.bin"


def normalize_checksum(checksum):
    checksum = str(checksum or "").strip().lower()

    if len(checksum) != 64:
        return ""

    if any(char not in "0123456789abcdef" for char in checksum):
        return ""

    return checksum


def find_duplicate_file(owner_sub, owner_email, checksum):
    scan_kwargs = {
        "FilterExpression": Attr("checksum").eq(checksum),
    }

    while True:
        scan_result = table.scan(**scan_kwargs)

        for item in scan_result.get("Items", []):
            belongs_to_user = (
                (owner_sub and item.get("owner_sub") == owner_sub)
                or (owner_email and item.get("owner_email") == owner_email)
            )

            if belongs_to_user:
                return item

        last_key = scan_result.get("LastEvaluatedKey")

        if not last_key:
            break

        scan_kwargs["ExclusiveStartKey"] = last_key

    return None


def build_metadata_headers(metadata):
    return {
        f"x-amz-meta-{key}": value
        for key, value in metadata.items()
        if value
    }


def lambda_handler(event, context):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
    )

    if method == "OPTIONS":
        return response(200, {"message": "ok"})

    claims = get_claims(event)

    owner_sub = claims.get("sub", "")
    owner_email = claims.get("email", "")

    if not owner_sub and not owner_email:
        return response(401, {"message": "Missing Cognito user identity."})

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return response(400, {"message": "Invalid JSON body"})

    filename = safe_filename(body.get("filename", "upload.bin"))
    content_type = body.get("content_type") or "application/octet-stream"
    checksum = normalize_checksum(body.get("checksum"))

    if not checksum:
        return response(
            400,
            {
                "message": "A valid SHA-256 checksum is required before upload.",
            },
        )

    duplicate_file = find_duplicate_file(owner_sub, owner_email, checksum)

    if duplicate_file:
        return response(
            409,
            {
                "message": "Duplicate upload prevented. This file has already been uploaded.",
                "duplicate": True,
                "file_id": duplicate_file.get("file_id"),
                "filename": duplicate_file.get("filename"),
                "s3_key": duplicate_file.get("s3_key"),
                "checksum": checksum,
            },
        )

    owner_identity = owner_sub or owner_email
    file_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{owner_identity}:{checksum}"))
    s3_key = f"{UPLOAD_PREFIX}{file_id}_{filename}"
    created_at = datetime.now(timezone.utc).isoformat()
    metadata = {
        "owner-sub": owner_sub,
        "owner-email": owner_email,
        "checksum": checksum,
    }

    try:
        table.put_item(
            Item={
                "file_id": file_id,
                "bucket": BUCKET_NAME,
                "s3_key": s3_key,
                "filename": filename,
                "content_type": content_type,
                "status": "upload_requested",
                "owner_sub": owner_sub,
                "owner_email": owner_email,
                "checksum": checksum,
                "created_at": created_at,
            },
            ConditionExpression="attribute_not_exists(file_id)",
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return response(
                409,
                {
                    "message": "Duplicate upload prevented. This file has already been uploaded.",
                    "duplicate": True,
                    "file_id": file_id,
                    "checksum": checksum,
                },
            )

        raise

    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": BUCKET_NAME,
            "Key": s3_key,
            "ContentType": content_type,
            "Metadata": metadata,
        },
        ExpiresIn=URL_EXPIRES_IN,
    )

    return response(
        200,
        {
            "file_id": file_id,
            "bucket": BUCKET_NAME,
            "s3_key": s3_key,
            "upload_url": upload_url,
            "expires_in": URL_EXPIRES_IN,
            "method": "PUT",
            "content_type": content_type,
            "checksum": checksum,
            "metadata_headers": build_metadata_headers(metadata),
            "owner_sub": owner_sub,
            "owner_email": owner_email,
            "created_at": created_at,
        },
    )
