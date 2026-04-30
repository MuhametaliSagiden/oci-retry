#!/usr/bin/env python3
"""
oci_arm_retry.py
================

Бесконечно пытается создать Always Free инстанс VM.Standard.A1.Flex
(Ampere ARM, до 4 OCPU / 24 GB RAM) в Oracle Cloud, обходя ошибку
"Out of host capacity". Перебирает все Availability Domains региона,
делает рандомизированный backoff, при успехе шлёт уведомление в Telegram.

Конфигурация — через переменные окружения или файл .env (см. .env.example).

Поведение по типам ошибок OCI:
  * 500 + "Out of host capacity"  -> ждём MIN_DELAY..MAX_DELAY секунд, retry
  * 429 TooManyRequests           -> ждём TOO_MANY_REQUESTS_DELAY, retry
  * 400 LimitExceeded             -> выходим (превышен лимит тенанта)
  * 401/403/404                   -> выходим (ошибка конфигурации)
  * прочее                        -> ждём 60с и retry (transient API error)
"""
from __future__ import annotations

import logging
import os
import random
import sys
import time
from typing import List, Optional

import oci
from oci.exceptions import ServiceError

try:
    from dotenv import load_dotenv

    # override=True so that .env wins over inherited shell env vars,
    # which is the more useful default for this script.
    load_dotenv(override=True)
except ImportError:
    pass


# ----- OCI auth / client -----
OCI_CONFIG_FILE = os.path.expanduser(os.getenv("OCI_CONFIG_FILE", "~/.oci/config"))
OCI_PROFILE = os.getenv("OCI_PROFILE", "DEFAULT")

# ----- Required IDs -----
COMPARTMENT_ID = os.getenv("OCI_COMPARTMENT_ID", "").strip()
SUBNET_ID = os.getenv("OCI_SUBNET_ID", "").strip()
IMAGE_ID = os.getenv("OCI_IMAGE_ID", "").strip()

# ----- Instance shape -----
SHAPE = os.getenv("OCI_SHAPE", "VM.Standard.A1.Flex").strip()
OCPUS = float(os.getenv("OCI_OCPUS", "4"))
MEMORY_GB = float(os.getenv("OCI_MEMORY_GB", "24"))
BOOT_VOLUME_GB = int(os.getenv("OCI_BOOT_VOLUME_GB", "100"))

# ----- Instance metadata -----
DISPLAY_NAME = os.getenv("OCI_DISPLAY_NAME", "atm10-arm")
SSH_PUBLIC_KEY = os.getenv("OCI_SSH_PUBLIC_KEY", "").strip()

# ----- Availability Domain selection -----
# Comma-separated names or substrings, e.g. "AD-1,AD-2,AD-3" or full names.
# Empty -> try every AD in the region.
AVAILABILITY_DOMAINS = os.getenv("OCI_AVAILABILITY_DOMAINS", "").strip()

# ----- Retry tuning -----
MIN_DELAY = int(os.getenv("MIN_DELAY", "60"))
MAX_DELAY = int(os.getenv("MAX_DELAY", "300"))
TOO_MANY_REQUESTS_DELAY = int(os.getenv("TOO_MANY_REQUESTS_DELAY", "120"))

# Stop after N attempts (0 = unlimited). Useful for GitHub Actions runs.
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "0"))
# Stop after N seconds of runtime (0 = unlimited).
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "0"))

# ----- Telegram (optional) -----
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("oci-arm-retry")


def notify_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import requests

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram notify failed: %s", exc)


def list_availability_domains(identity_client, compartment_id: str) -> List[str]:
    ads = identity_client.list_availability_domains(compartment_id=compartment_id).data
    return [ad.name for ad in ads]


def pick_ads(all_ads: List[str]) -> List[str]:
    if not AVAILABILITY_DOMAINS:
        return all_ads
    wanted = [a.strip() for a in AVAILABILITY_DOMAINS.split(",") if a.strip()]
    selected: List[str] = []
    for w in wanted:
        for ad in all_ads:
            if w in ad and ad not in selected:
                selected.append(ad)
    return selected or all_ads


def build_launch_details(ad: str) -> "oci.core.models.LaunchInstanceDetails":
    metadata = {}
    if SSH_PUBLIC_KEY:
        metadata["ssh_authorized_keys"] = SSH_PUBLIC_KEY

    return oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=COMPARTMENT_ID,
        display_name=DISPLAY_NAME,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS,
            memory_in_gbs=MEMORY_GB,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=IMAGE_ID,
            boot_volume_size_in_gbs=BOOT_VOLUME_GB,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=SUBNET_ID,
            assign_public_ip=True,
        ),
        metadata=metadata,
    )


def is_out_of_capacity(err: ServiceError) -> bool:
    msg = str(err.message or "")
    return err.status == 500 and (
        "Out of host capacity" in msg
        or "out of capacity" in msg.lower()
    )


def is_too_many_requests(err: ServiceError) -> bool:
    return err.status == 429


def is_limit_exceeded(err: ServiceError) -> bool:
    return err.status == 400 and (err.code or "").lower() in {
        "limitexceeded",
        "quotaexceeded",
    }


def is_fatal_auth(err: ServiceError) -> bool:
    return err.status in (401, 403, 404)


def main() -> int:
    missing = [
        name
        for name, val in (
            ("OCI_COMPARTMENT_ID", COMPARTMENT_ID),
            ("OCI_SUBNET_ID", SUBNET_ID),
            ("OCI_IMAGE_ID", IMAGE_ID),
        )
        if not val
    ]
    if missing:
        log.error("Missing required env vars: %s", ", ".join(missing))
        return 2

    try:
        config = oci.config.from_file(OCI_CONFIG_FILE, OCI_PROFILE)
        oci.config.validate_config(config)
    except Exception as exc:  # noqa: BLE001
        log.error("Bad OCI config (%s, profile=%s): %s", OCI_CONFIG_FILE, OCI_PROFILE, exc)
        return 2

    compute = oci.core.ComputeClient(config)
    identity = oci.identity.IdentityClient(config)

    try:
        all_ads = list_availability_domains(identity, COMPARTMENT_ID)
    except ServiceError as e:
        log.error("Failed to list ADs: status=%s code=%s message=%s", e.status, e.code, e.message)
        return 2

    log.info("Region ADs: %s", all_ads)
    candidates = pick_ads(all_ads)
    log.info("Will try ADs: %s", candidates)
    log.info(
        "Shape=%s ocpus=%g memory=%gGB boot=%dGB display_name=%s",
        SHAPE, OCPUS, MEMORY_GB, BOOT_VOLUME_GB, DISPLAY_NAME,
    )

    started = time.time()
    attempt = 0
    while True:
        if MAX_ATTEMPTS and attempt >= MAX_ATTEMPTS:
            log.info("Reached MAX_ATTEMPTS=%d. Exiting (still no capacity).", MAX_ATTEMPTS)
            return 1
        if MAX_RUNTIME_SECONDS and (time.time() - started) > MAX_RUNTIME_SECONDS:
            log.info("Reached MAX_RUNTIME_SECONDS=%d. Exiting.", MAX_RUNTIME_SECONDS)
            return 1

        attempt += 1
        ad = candidates[(attempt - 1) % len(candidates)]
        log.info("Attempt %d in AD=%s", attempt, ad)

        def _sleep_bounded(base_delay: int) -> None:
            """Sleep `base_delay` seconds, but no longer than remaining runtime budget."""
            if MAX_RUNTIME_SECONDS:
                remaining = MAX_RUNTIME_SECONDS - (time.time() - started)
                if remaining <= 0:
                    return
                actual = min(base_delay, int(remaining) + 1)
            else:
                actual = base_delay
            time.sleep(actual)

        try:
            response = compute.launch_instance(build_launch_details(ad))
            inst = response.data
            msg = (
                "OCI A1.Flex SUCCESS\n"
                f"OCID: {inst.id}\n"
                f"AD: {ad}\n"
                f"Shape: {SHAPE} ({OCPUS} OCPU / {MEMORY_GB} GB / {BOOT_VOLUME_GB} GB boot)\n"
                f"Display name: {inst.display_name}\n"
                f"Attempts: {attempt}\n"
                f"Elapsed: {int(time.time() - started)}s"
            )
            log.info(msg.replace("\n", " | "))
            notify_telegram(msg)
            return 0

        except ServiceError as e:
            if is_out_of_capacity(e):
                delay = random.randint(MIN_DELAY, MAX_DELAY)
                log.warning("AD=%s out of capacity. Sleep %ds.", ad, delay)
                _sleep_bounded(delay)
            elif is_too_many_requests(e):
                log.warning("429 TooManyRequests. Sleep %ds.", TOO_MANY_REQUESTS_DELAY)
                _sleep_bounded(TOO_MANY_REQUESTS_DELAY)
            elif is_limit_exceeded(e):
                log.error(
                    "Tenancy limit reached (%s). Reduce OCPU/memory or delete unused A1 instances.",
                    e.code,
                )
                notify_telegram(f"OCI retry FAILED: {e.code} — {e.message}")
                return 3
            elif is_fatal_auth(e):
                log.error(
                    "Fatal auth/config error: status=%s code=%s message=%s",
                    e.status, e.code, e.message,
                )
                notify_telegram(f"OCI retry FAILED: {e.status} {e.code} — {e.message}")
                return 2
            else:
                log.error(
                    "Unhandled ServiceError: status=%s code=%s message=%s — sleeping 60s",
                    e.status, e.code, e.message,
                )
                _sleep_bounded(60)

        except KeyboardInterrupt:
            log.info("Interrupted.")
            return 130

        except Exception:  # noqa: BLE001
            log.exception("Unexpected error; sleeping 60s before retry")
            _sleep_bounded(60)


if __name__ == "__main__":
    sys.exit(main())
