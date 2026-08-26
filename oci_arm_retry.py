#!/usr/bin/env python3
"""
oci_arm_retry.py
================

Ловит Always Free инстанс в Oracle Cloud, обходя вечное "Out of host capacity",
и сразу разворачивает на нём TorrServer с HTTPS-адресом для Lampa.

Что делает за тебя:
  1. подтягивает свежий образ Ubuntu под нужную архитектуру;
  2. создаёт VCN, публичный сабнет, Internet Gateway и открывает порты;
  3. по кругу дёргает launch_instance по всем Availability Domains региона,
     перебирая план shape'ов (сначала ARM, при желании -- fallback на AMD micro);
  4. передаёт cloud-init, который на первом же бутe ставит Docker, TorrServer,
     Caddy с Let's Encrypt и обновление DuckDNS;
  5. дожидается RUNNING, забирает публичный IP и присылает в Telegram готовую
     строку для поля TorrServer в Lampa.

Конфигурация -- через переменные окружения (см. .env.example).

Коды выхода (их разбирает GitHub Actions):
  0 -- инстанс создан
  1 -- ёмкости так и не было (нормально, повторим по расписанию)
  2 -- ошибка конфигурации / авторизации (чинить руками)
  3 -- LimitExceeded: тенант уже исчерпал Always Free квоту
"""
from __future__ import annotations

import logging
import os
import random
import sys
import time
from typing import List, Optional, Tuple

import oci
from oci.exceptions import ServiceError

try:
    from dotenv import load_dotenv

    # override=True: .env важнее унаследованных переменных шелла.
    load_dotenv(override=True)
except ImportError:
    pass

from oci_bootstrap import ensure_network, render_cloud_init, resolve_domain, resolve_image

# ----- OCI auth -----
OCI_CONFIG_FILE = os.path.expanduser(os.getenv("OCI_CONFIG_FILE", "~/.oci/config"))
OCI_PROFILE = os.getenv("OCI_PROFILE", "DEFAULT")

# ----- Что запускаем -----
# План shape'ов: "shape:ocpus:memory_gb", через запятую, по приоритету.
# Актуальная Always Free квота ARM с 15.06.2026: 2 OCPU / 12 GB.
SHAPE_PLAN_RAW = os.getenv("OCI_SHAPE_PLAN", "VM.Standard.A1.Flex:2:12")
BOOT_VOLUME_GB = int(os.getenv("OCI_BOOT_VOLUME_GB", "100"))
DISPLAY_NAME = os.getenv("OCI_DISPLAY_NAME", "torrserver")
SSH_PUBLIC_KEY = os.getenv("OCI_SSH_PUBLIC_KEY", "").strip()

# ----- Где запускаем -----
COMPARTMENT_ID = os.getenv("OCI_COMPARTMENT_ID", "").strip()  # пусто -> root (tenancy)
SUBNET_ID = os.getenv("OCI_SUBNET_ID", "").strip()            # пусто -> создадим сами
IMAGE_ID = os.getenv("OCI_IMAGE_ID", "").strip()              # пусто -> найдём сами
AVAILABILITY_DOMAINS = os.getenv("OCI_AVAILABILITY_DOMAINS", "").strip()

# ----- TorrServer -----
TS_PEER_PORT = int(os.getenv("TS_PEER_PORT", "32000"))

# ----- Retry tuning -----
MIN_DELAY = int(os.getenv("MIN_DELAY", "30"))
MAX_DELAY = int(os.getenv("MAX_DELAY", "90"))
TOO_MANY_REQUESTS_DELAY = int(os.getenv("TOO_MANY_REQUESTS_DELAY", "120"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "0"))            # 0 = без ограничения
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "0"))

# ----- Telegram (опционально) -----
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("oci-arm-retry")


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #
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


def github_summary(text: str) -> None:
    """Пишем результат в Job Summary, чтобы было видно прямо в Actions."""
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


def parse_shape_plan(raw: str) -> List[Tuple[str, float, float]]:
    plan: List[Tuple[str, float, float]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        shape = parts[0].strip()
        ocpus = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
        memory = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
        plan.append((shape, ocpus, memory))
    return plan


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


def build_launch_details(ad: str, shape: str, ocpus: float, memory: float,
                         image_id: str, subnet_id: str, user_data: Optional[str]):
    metadata = {}
    if SSH_PUBLIC_KEY:
        metadata["ssh_authorized_keys"] = SSH_PUBLIC_KEY
    if user_data:
        metadata["user_data"] = user_data

    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=COMPARTMENT_ID,
        display_name=DISPLAY_NAME,
        shape=shape,
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id,
            boot_volume_size_in_gbs=BOOT_VOLUME_GB,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id,
            assign_public_ip=True,
        ),
        metadata=metadata,
    )
    # shape_config допустим только для Flex-шейпов.
    if shape.endswith(".Flex") and ocpus and memory:
        details.shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=ocpus, memory_in_gbs=memory
        )
    return details


def wait_for_public_ip(compute, network, instance_id: str) -> Optional[str]:
    try:
        oci.wait_until(
            compute, compute.get_instance(instance_id),
            "lifecycle_state", "RUNNING", max_wait_seconds=600,
        )
        attachments = compute.list_vnic_attachments(
            compartment_id=COMPARTMENT_ID, instance_id=instance_id
        ).data
        for att in attachments:
            if not att.vnic_id:
                continue
            vnic = network.get_vnic(att.vnic_id).data
            if vnic.public_ip:
                return vnic.public_ip
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not resolve public IP: %s", exc)
    return None


# ----- классификация ошибок OCI -----
def is_out_of_capacity(err: ServiceError) -> bool:
    msg = str(err.message or "")
    code = (err.code or "").lower()
    return (
        err.status in (500, 502)
        and ("out of capacity" in msg.lower() or code == "internalerror")
    ) or "outofcapacity" in code


def is_too_many_requests(err: ServiceError) -> bool:
    return err.status == 429


def is_limit_exceeded(err: ServiceError) -> bool:
    return err.status == 400 and (err.code or "").lower() in {
        "limitexceeded", "quotaexceeded",
    }


def is_fatal_auth(err: ServiceError) -> bool:
    return err.status in (401, 403, 404)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    global COMPARTMENT_ID, SUBNET_ID

    try:
        config = oci.config.from_file(OCI_CONFIG_FILE, OCI_PROFILE)
        oci.config.validate_config(config)
    except Exception as exc:  # noqa: BLE001
        log.error("Bad OCI config (%s, profile=%s): %s", OCI_CONFIG_FILE, OCI_PROFILE, exc)
        return 2

    compute = oci.core.ComputeClient(config)
    identity = oci.identity.IdentityClient(config)
    network = oci.core.VirtualNetworkClient(config)

    # Компартмент по умолчанию -- корневой (он же tenancy OCID).
    if not COMPARTMENT_ID:
        COMPARTMENT_ID = config["tenancy"]
        log.info("OCI_COMPARTMENT_ID not set, using root compartment")

    try:
        all_ads = [ad.name for ad in
                   identity.list_availability_domains(compartment_id=COMPARTMENT_ID).data]
    except ServiceError as e:
        log.error("Failed to list ADs: status=%s code=%s message=%s", e.status, e.code, e.message)
        return 2

    candidates = pick_ads(all_ads)
    plan = parse_shape_plan(SHAPE_PLAN_RAW)
    if not plan:
        log.error("OCI_SHAPE_PLAN is empty")
        return 2

    log.info("Region ADs: %s", candidates)
    log.info("Shape plan: %s", plan)

    # --- подготовка сети ---
    if not SUBNET_ID:
        try:
            SUBNET_ID = ensure_network(network, COMPARTMENT_ID, TS_PEER_PORT)
        except ServiceError as e:
            log.error("Network bootstrap failed: status=%s code=%s message=%s",
                      e.status, e.code, e.message)
            return 2

    # --- образы под каждый shape ---
    images: dict = {}
    for shape, _, _ in plan:
        if IMAGE_ID:
            images[shape] = IMAGE_ID
            continue
        try:
            images[shape] = resolve_image(compute, COMPARTMENT_ID, shape)
        except Exception as exc:  # noqa: BLE001
            log.error("Image lookup failed for %s: %s", shape, exc)

    plan = [p for p in plan if p[0] in images]
    if not plan:
        log.error("No usable image for any shape in the plan")
        return 2

    user_data = render_cloud_init(TS_PEER_PORT)
    if user_data:
        log.info("cloud-init payload prepared (%d bytes base64)", len(user_data))

    started = time.time()
    attempt = 0
    combos = [(ad, shape, ocpus, mem) for shape, ocpus, mem in plan for ad in candidates]

    def sleep_bounded(base_delay: int) -> None:
        """Спим base_delay секунд, но не дольше остатка бюджета времени."""
        if MAX_RUNTIME_SECONDS:
            remaining = MAX_RUNTIME_SECONDS - (time.time() - started)
            if remaining <= 0:
                return
            base_delay = min(base_delay, int(remaining) + 1)
        time.sleep(base_delay)

    while True:
        if MAX_ATTEMPTS and attempt >= MAX_ATTEMPTS:
            log.info("Reached MAX_ATTEMPTS=%d, still no capacity.", MAX_ATTEMPTS)
            return 1
        if MAX_RUNTIME_SECONDS and (time.time() - started) > MAX_RUNTIME_SECONDS:
            log.info("Reached MAX_RUNTIME_SECONDS=%d, still no capacity.", MAX_RUNTIME_SECONDS)
            return 1

        ad, shape, ocpus, mem = combos[attempt % len(combos)]
        attempt += 1
        log.info("Attempt %d: %s in %s", attempt, shape, ad)

        try:
            details = build_launch_details(
                ad, shape, ocpus, mem, images[shape], SUBNET_ID, user_data
            )
            inst = compute.launch_instance(details).data

            log.info("LAUNCHED %s, waiting for public IP...", inst.id)
            ip = wait_for_public_ip(compute, network, inst.id)
            domain = resolve_domain()
            address = f"https://{domain}" if domain else f"http://{ip}"

            msg = (
                "TorrServer поймал инстанс в OCI\n"
                f"Shape: {shape} ({ocpus:g} OCPU / {mem:g} GB)\n"
                f"AD: {ad}\n"
                f"IP: {ip or 'unknown'}\n"
                f"Адрес для Lampa: {address}\n"
                f"Попыток: {attempt}, времени: {int(time.time() - started)}s\n"
                "cloud-init доустанавливает Docker/TorrServer/Caddy ~3-5 минут."
            )
            log.info(msg.replace("\n", " | "))
            notify_telegram(msg)
            github_summary("## Инстанс создан\n\n```\n" + msg + "\n```")
            return 0

        except ServiceError as e:
            if is_out_of_capacity(e):
                delay = random.randint(MIN_DELAY, MAX_DELAY)
                log.warning("%s in %s: out of host capacity. Sleep %ds.", shape, ad, delay)
                sleep_bounded(delay)
            elif is_too_many_requests(e):
                log.warning("429 TooManyRequests. Sleep %ds.", TOO_MANY_REQUESTS_DELAY)
                sleep_bounded(TOO_MANY_REQUESTS_DELAY)
            elif is_limit_exceeded(e):
                log.error("Always Free квота исчерпана (%s): %s", e.code, e.message)
                notify_telegram(f"OCI retry FAILED: {e.code} -- {e.message}")
                return 3
            elif is_fatal_auth(e):
                log.error("Fatal auth/config error: status=%s code=%s message=%s",
                          e.status, e.code, e.message)
                notify_telegram(f"OCI retry FAILED: {e.status} {e.code} -- {e.message}")
                return 2
            else:
                log.error("Unhandled ServiceError: status=%s code=%s message=%s -- sleep 60s",
                          e.status, e.code, e.message)
                sleep_bounded(60)

        except KeyboardInterrupt:
            log.info("Interrupted.")
            return 130

        except Exception:  # noqa: BLE001
            log.exception("Unexpected error; sleeping 60s before retry")
            sleep_bounded(60)


if __name__ == "__main__":
    sys.exit(main())
