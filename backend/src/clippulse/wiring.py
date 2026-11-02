"""Builds the real service for AWS: DynamoDB for storage, SSM Parameter Store for the
Twitch credentials. boto3 is provided by the Lambda Python runtime, so nothing is packaged."""
from __future__ import annotations

from typing import Optional

from .config import load_settings
from .service import ClipService
from .store import DynamoStore
from .twitch import TwitchClient, TwitchError

_service: Optional[ClipService] = None


def build_service() -> ClipService:
    """Created once per warm Lambda container, so the Twitch token is reused too."""
    global _service
    if _service is not None:
        return _service

    import boto3  # noqa: WPS433 (deliberately lazy)

    settings = load_settings()
    table = boto3.resource("dynamodb").Table(settings.table_name)
    ssm = boto3.client("ssm")
    cache: dict = {}

    def twitch() -> TwitchClient:
        if "client" not in cache:
            try:
                def param(name: str) -> str:
                    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
                cache["client"] = TwitchClient(param(settings.client_id_param), param(settings.client_secret_param))
            except Exception as err:
                raise TwitchError("Twitch credentials are not configured") from err
        return cache["client"]

    _service = ClipService(DynamoStore(table), twitch, settings)
    return _service
