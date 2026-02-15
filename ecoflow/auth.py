"""
EcoFlow Developer API authentication.

Uses HMAC-SHA256 signed requests with accessKey/secretKey to obtain
temporary MQTT credentials from the EcoFlow REST API.

Reference: https://developer.ecoflow.com/us/document/generalInfo
"""

import hashlib
import hmac
import logging
import random
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)


def _hmac_sha256(data: str, key: str) -> str:
    """Compute HMAC-SHA256 hex digest."""
    digest = hmac.new(
        key.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return "".join(format(b, "02x") for b in digest)


def _build_headers(access_key: str, secret_key: str, params: dict | None = None) -> dict:
    """
    Build the signed request headers required by every EcoFlow API call.

    The signature covers all query/body parameters (sorted alphabetically)
    plus the three meta-fields: accessKey, nonce, timestamp.
    """
    nonce = str(random.randint(100_000, 999_999))
    timestamp = str(int(time.time() * 1000))

    sign_parts: list[str] = []
    if params:
        for key in sorted(params.keys()):
            sign_parts.append(f"{key}={params[key]}")

    sign_parts += [
        f"accessKey={access_key}",
        f"nonce={nonce}",
        f"timestamp={timestamp}",
    ]
    sign_str = "&".join(sign_parts)
    sign = _hmac_sha256(sign_str, secret_key)

    return {
        "accessKey": access_key,
        "nonce": nonce,
        "timestamp": timestamp,
        "sign": sign,
    }


@dataclass
class MqttCredentials:
    """Credentials returned by the EcoFlow certification endpoint."""
    username: str      # certificateAccount
    password: str      # certificatePassword
    host: str          # mqtt broker host
    port: int          # mqtt broker port (usually 8883)
    protocol: str      # usually "mqtts"


def get_device_quota(api_host: str, access_key: str, secret_key: str, device_sn: str) -> dict:
    """
    Call GET /iot-open/sign/device/quota/all to retrieve all current device properties.

    Returns a flat dict of property key → value pairs (same dot-notation format
    used by the MQTT stream cache, e.g. {"pd.soc": 85, "inv.inputWatts": 0, ...}).

    Raises:
        requests.HTTPError: on non-2xx HTTP responses
        ValueError: if the EcoFlow API returns a non-zero error code
    """
    url = f"{api_host}/iot-open/sign/device/quota/all"
    params = {"sn": device_sn}
    headers = _build_headers(access_key, secret_key, params)

    logger.info("Fetching device quota from %s (sn=%s)", url, device_sn)
    response = requests.get(url, headers=headers, params=params, timeout=15)
    response.raise_for_status()

    body = response.json()
    code = body.get("code")
    if code != "0":
        raise ValueError(
            f"EcoFlow API error (code={code}): {body.get('message', 'Unknown error')}"
        )

    print(body)
    print(f"\n\n\n\n{body['data']}\n\n\n\n")
    return body["data"]


def get_mqtt_credentials(api_host: str, access_key: str, secret_key: str) -> MqttCredentials:
    """
    Call GET /iot-open/sign/certification to retrieve MQTT broker credentials.

    Raises:
        requests.HTTPError: on non-2xx HTTP responses
        ValueError: if the EcoFlow API returns a non-zero error code
    """
    url = f"{api_host}/iot-open/sign/certification"
    headers = _build_headers(access_key, secret_key)

    logger.info("Fetching MQTT credentials from %s", url)
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()

    body = response.json()
    code = body.get("code")
    if code != "0":
        raise ValueError(
            f"EcoFlow API error (code={code}): {body.get('message', 'Unknown error')}"
        )

    data = body["data"]
    return MqttCredentials(
        username=data["certificateAccount"],
        password=data["certificatePassword"],
        host=data["url"],
        port=int(data["port"]),
        protocol=data.get("protocol", "mqtts"),
    )
