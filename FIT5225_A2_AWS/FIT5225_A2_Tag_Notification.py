import json
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeDeserializer


sns = boto3.client("sns")
deserializer = TypeDeserializer()

SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
WATCHED_TAGS = {
    " ".join(tag.strip().lower().replace("_", " ").split())
    for tag in os.environ.get("WATCHED_TAGS", "").split(",")
    if tag.strip()
}


def normalize_tag(tag):
    return " ".join(str(tag or "").strip().lower().replace("_", " ").split())


def to_json_safe(value):
    if isinstance(value, dict):
        return {key: to_json_safe(child) for key, child in value.items()}

    if isinstance(value, list):
        return [to_json_safe(child) for child in value]

    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)

    return value


def deserialize_image(stream_image):
    if not stream_image:
        return {}

    return {
        key: deserializer.deserialize(value)
        for key, value in stream_image.items()
    }


def parse_tags(item):
    tags = item.get("tags") or {}

    if isinstance(tags, dict):
        parsed = {}

        for tag, count in tags.items():
            tag_name = normalize_tag(tag)

            try:
                count_value = int(count)
            except Exception:
                count_value = 1

            if tag_name and count_value > 0:
                parsed[tag_name] = count_value

        return parsed

    if isinstance(tags, list):
        return {
            normalize_tag(tag): 1
            for tag in tags
            if normalize_tag(tag)
        }

    return {}


def find_added_watched_tags(old_item, new_item):
    old_tags = parse_tags(old_item)
    new_tags = parse_tags(new_item)
    added = []

    for tag, new_count in new_tags.items():
        if WATCHED_TAGS and tag not in WATCHED_TAGS:
            continue

        old_count = old_tags.get(tag, 0)

        if new_count > old_count:
            added.append(tag)

    return added


def publish_notification(item, matched_tags, event_name):
    filename = item.get("filename") or item.get("s3_key") or "Unknown file"
    owner_email = item.get("owner_email") or "Unknown owner"
    file_id = item.get("file_id") or "Unknown file_id"

    subject = "FIT5225 watched tag detected"
    message = "\n".join(
        [
            "Watched tag notification",
            "",
            f"Event: {event_name}",
            f"Matched tag(s): {', '.join(matched_tags)}",
            f"File: {filename}",
            f"File ID: {file_id}",
            f"Owner: {owner_email}",
            "",
            "Full item:",
            json.dumps(to_json_safe(item), indent=2),
        ]
    )

    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject,
        Message=message,
    )


def lambda_handler(event, context):
    if not WATCHED_TAGS:
        print("WATCHED_TAGS is empty. No notifications will be sent.")
        return {"notified": 0, "reason": "WATCHED_TAGS is empty"}

    notified = 0

    for record in event.get("Records", []):
        event_name = record.get("eventName")

        if event_name not in {"INSERT", "MODIFY"}:
            continue

        dynamodb_record = record.get("dynamodb", {})
        old_item = deserialize_image(dynamodb_record.get("OldImage"))
        new_item = deserialize_image(dynamodb_record.get("NewImage"))

        matched_tags = find_added_watched_tags(old_item, new_item)

        if not matched_tags:
            continue

        print(
            "Publishing watched tag notification:",
            json.dumps(
                {
                    "event_name": event_name,
                    "matched_tags": matched_tags,
                    "file_id": new_item.get("file_id"),
                    "filename": new_item.get("filename"),
                },
                default=str,
            ),
        )

        publish_notification(new_item, matched_tags, event_name)
        notified += 1

    return {"notified": notified}
