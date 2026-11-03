"""Surge alerts: when a clip on a pinned streamer starts taking off, post it to Discord.

The path is deliberately split into small, separately retryable steps:

    poller  ->  SQS queue  ->  starter Lambda  ->  Step Functions  ->  notifier Lambda  ->  Discord

* The poller only decides what is worth announcing and drops a message on the queue. A slow or broken
  Discord never delays the hourly snapshot.
* SQS holds the message until the starter picks it up, and parks it in a dead letter queue if that keeps failing.
* The starter begins one Step Functions execution per clip. The execution name is derived from the clip id,
  so a redelivered message cannot announce the same clip twice.
* The state machine owns the retry policy (exponential backoff for rate limits and outages, no retries for a
  rejected webhook) and leaves a visible failed execution when it gives up.
* The notifier posts the message. The webhook URL lives in SSM Parameter Store and never in the template.

Everything here uses only the standard library plus boto3, which the Lambda runtime provides.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("clippulse.alerts")
logger.setLevel(logging.INFO)

TIMEOUT_SECONDS = 10
EMBED_COLOR = 0x9146FF                       # Twitch purple
CLIP_HOSTS = ("https://clips.twitch.tv/", "https://www.twitch.tv/")
EXECUTION_NAME_LIMIT = 80

# post(url, body_bytes, headers) -> (status, response_headers)
PostFn = Callable[[str, bytes, Dict[str, str]], Tuple[int, Dict[str, str]]]


# The names of these classes are what Step Functions matches in its Retry and Catch rules (the Lambda
# runtime reports an unhandled exception by its class name), so they must stay in step with template.yaml.
class DiscordRetryable(Exception):
    """Discord is busy or down (429, 5xx, network). Worth trying again later."""


class DiscordRejected(Exception):
    """Discord refused the message (bad webhook, bad payload). Trying again will not help."""


# -- building the message -------------------------------------------------------------------------

def make_alert(clip: dict) -> dict:
    """The small, plain message the poller puts on the queue. Only fields the notifier needs."""
    return {
        "clip_id": str(clip["id"]),
        "title": str(clip.get("title") or "")[:140],
        "url": clip.get("url"),
        "streamer": clip.get("streamer_name") or clip.get("streamer"),
        "velocity": clip.get("recent_velocity") if clip.get("recent_velocity") is not None else clip.get("velocity"),
        "views": int(clip.get("view_count") or 0),
        "age_hours": clip.get("age_hours"),
        "thumbnail_url": clip.get("thumbnail_url"),
    }


def _compact(number: float) -> str:
    number = float(number)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return f"{number:.0f}" if number >= 10 else f"{number:.1f}"


def _age_text(hours: Optional[float]) -> str:
    if hours is None:
        return ""
    if hours < 1:
        return f"{max(1, round(hours * 60))}m old"
    if hours < 48:
        return f"{hours:.0f}h old"
    return f"{hours / 24:.0f}d old"


def discord_payload(alert: dict) -> dict:
    """A Discord webhook body. Titles are written by strangers, so no mention can ever ping anyone
    (allowed_mentions) and only Twitch links are made clickable."""
    url = alert.get("url") if str(alert.get("url") or "").startswith(CLIP_HOSTS) else None
    parts = [f"{_compact(alert.get('velocity') or 0)} views/hr", f"{_compact(alert.get('views') or 0)} views"]
    age = _age_text(alert.get("age_hours"))
    if age:
        parts.append(age)
    embed: Dict[str, Any] = {
        "title": (alert.get("title") or "Untitled clip")[:256],
        "description": f"**{alert.get('streamer') or 'A streamer'}** has a clip that is surging: " + " · ".join(parts),
        "color": EMBED_COLOR,
        "footer": {"text": "ClipPulse surge alert"},
    }
    if url:
        embed["url"] = url
    thumb = alert.get("thumbnail_url")
    if isinstance(thumb, str) and thumb.startswith("https://"):
        embed["image"] = {"url": thumb}
    return {"username": "ClipPulse", "allowed_mentions": {"parse": []}, "embeds": [embed]}


# -- the notifier ---------------------------------------------------------------------------------

def urllib_post(url: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, Dict[str, str]]:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise DiscordRetryable(f"could not reach Discord: {err}") from err


def send_to_discord(webhook_url: str, alert: dict, post: PostFn = urllib_post) -> None:
    if not re.match(r"^https://(?:[a-z]+\.)?discord(?:app)?\.com/api/webhooks/", webhook_url or ""):
        raise DiscordRejected("the configured webhook is not a Discord webhook URL")
    body = json.dumps(discord_payload(alert)).encode("utf-8")
    # Discord's edge rejects the default Python user agent, so identify ourselves.
    headers = {"content-type": "application/json", "user-agent": "ClipPulse (https://github.com/a1issa369/clippulse)"}
    status, _ = post(webhook_url, body, headers)
    if status in (200, 204):
        return
    if status == 429 or status >= 500:
        raise DiscordRetryable(f"Discord answered {status}")
    raise DiscordRejected(f"Discord answered {status}")


_webhook_cache: Dict[str, str] = {}


def _webhook_from_ssm() -> str:
    name = os.environ["DISCORD_WEBHOOK_PARAM"]
    if name not in _webhook_cache:
        import boto3  # lazy: only the deployed function needs it
        _webhook_cache[name] = boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    return _webhook_cache[name]


def notify_handler(event: Any = None, context: Any = None, *, webhook: Optional[Callable[[], str]] = None,
                   post: PostFn = urllib_post) -> dict:
    """Step Functions task: post one alert. Raises DiscordRetryable or DiscordRejected, which the
    state machine treats differently."""
    send_to_discord((webhook or _webhook_from_ssm)(), event or {}, post)
    logger.info(json.dumps({"alert_posted": (event or {}).get("clip_id")}))
    return {"posted": True, "clip_id": (event or {}).get("clip_id")}


# -- the starter ----------------------------------------------------------------------------------

def execution_name(clip_id: str) -> str:
    """Step Functions execution names allow letters, digits, - and _ and must be unique for 90 days, which is
    what makes a redelivered queue message harmless."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", str(clip_id))
    return f"surge-{cleaned}"[:EXECUTION_NAME_LIMIT]


def start_handler(event: Any, context: Any = None, *, sfn: Any = None, state_machine_arn: Optional[str] = None) -> dict:
    """SQS-triggered: start one execution per message. Returns the ids of failed messages so only those
    are retried (the event source mapping has ReportBatchItemFailures on)."""
    if sfn is None:
        import boto3
        sfn = boto3.client("stepfunctions")
    arn = state_machine_arn or os.environ["STATE_MACHINE_ARN"]
    failures = []
    for record in (event or {}).get("Records", []):
        try:
            alert = json.loads(record["body"])
            sfn.start_execution(stateMachineArn=arn, name=execution_name(alert["clip_id"]), input=json.dumps(alert))
        except Exception as err:
            if type(err).__name__ == "ExecutionAlreadyExists":
                continue                                 # already announced: the message was a duplicate
            logger.error(json.dumps({"start_failed": str(err), "message_id": record.get("messageId")}))
            failures.append({"itemIdentifier": record.get("messageId")})
    return {"batchItemFailures": failures}


# -- the publisher the poller uses ----------------------------------------------------------------

class SqsPublisher:
    """Puts an alert on the queue. Kept tiny so tests can substitute anything with a publish() method."""

    def __init__(self, queue_url: str, client: Any = None) -> None:
        self.queue_url = queue_url
        self._client = client

    def publish(self, alert: dict) -> None:
        if self._client is None:
            import boto3
            self._client = boto3.client("sqs")
        self._client.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps(alert))
