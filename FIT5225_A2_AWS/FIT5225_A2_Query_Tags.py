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
    "Access-Control-Allow-Methods": "OPTIONS,POST",
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
    authorizer = event.get("requestContext", {}).get("authorizer", {})

    claims = authorizer.get("claims")
    if claims:
        return claims

    return authorizer.get("jwt", {}).get("claims", {})


def unwrap_dynamodb_value(value):
    if isinstance(value, dict):
        if "S" in value:
            return value.get("S")

        if "N" in value:
            return value.get("N")

    return value


def normalize_tag_name(name):
    return " ".join(str(unwrap_dynamodb_value(name) or "").strip().lower().replace("_", " ").split())


def parse_count(value):
    try:
        return int(unwrap_dynamodb_value(value))
    except Exception:
        return 0


def get_tag_count(tags, query_tag):
    """
    Supports partial match:
    query 'kangaroo' can match 'eastern gray kangaroo'.
    """
    query_tag_norm = normalize_tag_name(query_tag)
    total = 0

    if isinstance(tags, list):
        tag_items = [(tag, 1) for tag in tags]
    elif isinstance(tags, dict):
        tag_items = tags.items()
    else:
        return 0

    for stored_tag, stored_count in tag_items:
        stored_tag_norm = normalize_tag_name(stored_tag)

        count_value = parse_count(stored_count)

        if query_tag_norm == stored_tag_norm or query_tag_norm in stored_tag_norm:
            total += count_value

    return total


def get_single_species_count(item, query_tag):
    """
    Some detector results store the species count in top-level `count`
    while the taxonomy tag itself is recorded as 1.
    Example: species=Cattle, count=6, tags.cattle=1.
    """
    query_tag_norm = normalize_tag_name(query_tag)
    species = item.get("species")

    if isinstance(species, list):
        species_names = [normalize_tag_name(name) for name in species]
    elif species:
        species_names = [normalize_tag_name(species)]
    else:
        species_names = []

    species_names = [name for name in species_names if name]

    if not species_names:
        all_animals = str(item.get("all_animals") or "")
        species_names = [
            normalize_tag_name(name)
            for name in all_animals.replace('"', "").split(",")
            if normalize_tag_name(name)
        ]

    if len(species_names) != 1:
        return 0

    species_name = species_names[0]

    if query_tag_norm == species_name or query_tag_norm in species_name:
        return parse_count(item.get("count"))

    return 0


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

    query_tags = body.get("tags")

    if not isinstance(query_tags, dict) or not query_tags:
        return response(
            400,
            {
                "message": "Request body must include tags object, for example: {'tags': {'kangaroo': 1}}"
            },
        )

    normalized_query = {}

    for tag, minimum_count in query_tags.items():
        tag_name = normalize_tag_name(tag)

        try:
            count_value = int(minimum_count)
        except Exception:
            return response(400, {"message": f"Invalid count for tag: {tag}"})

        if not tag_name:
            return response(400, {"message": "Tag name cannot be empty"})

        if count_value < 1:
            count_value = 1

        normalized_query[tag_name] = max(normalized_query.get(tag_name, 0), count_value)

    matched_files = []
    scan_kwargs = {}

    while True:
        scan_result = table.scan(**scan_kwargs)

        for item in scan_result.get("Items", []):
            belongs_to_user = (
                (owner_sub and item.get("owner_sub") == owner_sub)
                or (owner_email and item.get("owner_email") == owner_email)
            )

            if not belongs_to_user:
                continue

            tags = item.get("tags", {})

            is_match = True

            # AND logic:
            # {"koala": 3, "wombat": 2}
            # means both conditions must be satisfied.
            for query_tag, minimum_count in normalized_query.items():
                actual_count = max(
                    get_tag_count(tags, query_tag),
                    get_single_species_count(item, query_tag),
                )

                if actual_count < minimum_count:
                    is_match = False
                    break

            if is_match:
                matched_files.append(add_presigned_urls(item))

        last_key = scan_result.get("LastEvaluatedKey")

        if not last_key:
            break

        scan_kwargs["ExclusiveStartKey"] = last_key

    matched_files.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    return response(
        200,
        {
            "count": len(matched_files),
            "owner_sub": owner_sub,
            "owner_email": owner_email,
            "query": {
                "tags": normalized_query,
            },
            "files": matched_files,
        },
    )
