#!/usr/bin/env python3
"""
oci_bootstrap.py
================

Всё, что нужно подготовить в тенанте ДО запуска инстанса, чтобы от человека
не требовалось ни одного клика в консоли OCI:

  * resolve_image()     -- сам находит свежий образ Canonical Ubuntu под нужный
                           shape (aarch64 для A1.Flex, x86 для E2.1.Micro);
  * ensure_network()    -- создаёт (или переиспользует) VCN + публичный сабнет +
                           Internet Gateway + route rule + security list с
                           открытыми 22/80/443 и портом пиров BitTorrent;
  * render_cloud_init() -- подставляет секреты в шаблон cloud-init и отдаёт
                           base64, готовый для metadata.user_data.

Все операции идемпотентны: повторный вызов ничего не ломает и не плодит дубли.
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Optional

import oci

log = logging.getLogger("oci-bootstrap")

VCN_NAME = os.getenv("OCI_VCN_NAME", "torr-vcn")
VCN_CIDR = os.getenv("OCI_VCN_CIDR", "10.0.0.0/16")
SUBNET_NAME = os.getenv("OCI_SUBNET_NAME", "torr-subnet")
SUBNET_CIDR = os.getenv("OCI_SUBNET_CIDR", "10.0.0.0/24")

TCP = "6"
UDP = "17"


# --------------------------------------------------------------------------- #
# Образ
# --------------------------------------------------------------------------- #
def resolve_image(compute, compartment_id: str, shape: str) -> str:
    """Самый свежий образ Ubuntu, совместимый с указанным shape."""
    os_name = os.getenv("OCI_OS_NAME", "Canonical Ubuntu")
    os_version = os.getenv("OCI_OS_VERSION", "24.04")

    images = compute.list_images(
        compartment_id=compartment_id,
        operating_system=os_name,
        operating_system_version=os_version,
        shape=shape,
        sort_by="TIMECREATED",
        sort_order="DESC",
        lifecycle_state="AVAILABLE",
    ).data
    if not images:
        raise RuntimeError(f"No {os_name} {os_version} image available for shape {shape}")

    # Отбрасываем minimal-сборки: в них нет части пакетов, которые ждёт cloud-init.
    usable = [i for i in images if "minimal" not in (i.display_name or "").lower()]
    chosen = (usable or images)[0]
    log.info("Image for %s: %s", shape, chosen.display_name)
    return chosen.id


# --------------------------------------------------------------------------- #
# Сеть
# --------------------------------------------------------------------------- #
def _find(items, name: str):
    for it in items:
        if it.display_name == name and it.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            return it
    return None


def _ingress(protocol: str, port: int, source: str = "0.0.0.0/0"):
    rule = oci.core.models.IngressSecurityRule(
        protocol=protocol, source=source, is_stateless=False
    )
    port_range = oci.core.models.PortRange(min=port, max=port)
    if protocol == TCP:
        rule.tcp_options = oci.core.models.TcpOptions(destination_port_range=port_range)
    elif protocol == UDP:
        rule.udp_options = oci.core.models.UdpOptions(destination_port_range=port_range)
    return rule


def ensure_network(network, compartment_id: str, peer_port: int) -> str:
    """Возвращает OCID публичного сабнета, создавая всю обвязку при необходимости."""
    vcn = _find(network.list_vcns(compartment_id=compartment_id).data, VCN_NAME)
    if vcn is None:
        log.info("Creating VCN %s (%s)", VCN_NAME, VCN_CIDR)
        vcn = network.create_vcn(
            oci.core.models.CreateVcnDetails(
                compartment_id=compartment_id,
                cidr_block=VCN_CIDR,
                display_name=VCN_NAME,
                dns_label="torrvcn",
            )
        ).data
        oci.wait_until(network, network.get_vcn(vcn.id), "lifecycle_state", "AVAILABLE",
                       max_wait_seconds=300)
    else:
        log.info("Reusing VCN %s", VCN_NAME)

    igw = _find(
        network.list_internet_gateways(compartment_id=compartment_id, vcn_id=vcn.id).data,
        "torr-igw",
    )
    if igw is None:
        log.info("Creating Internet Gateway")
        igw = network.create_internet_gateway(
            oci.core.models.CreateInternetGatewayDetails(
                compartment_id=compartment_id,
                vcn_id=vcn.id,
                display_name="torr-igw",
                is_enabled=True,
            )
        ).data
        oci.wait_until(network, network.get_internet_gateway(igw.id), "lifecycle_state",
                       "AVAILABLE", max_wait_seconds=300)

    rt = network.get_route_table(vcn.default_route_table_id).data
    if not any(r.network_entity_id == igw.id for r in rt.route_rules):
        log.info("Adding default route 0.0.0.0/0 -> IGW")
        network.update_route_table(
            rt.id,
            oci.core.models.UpdateRouteTableDetails(
                route_rules=list(rt.route_rules) + [
                    oci.core.models.RouteRule(
                        destination="0.0.0.0/0",
                        destination_type="CIDR_BLOCK",
                        network_entity_id=igw.id,
                    )
                ]
            ),
        )

    sl = network.get_security_list(vcn.default_security_list_id).data
    rules = list(sl.ingress_security_rules)

    def covered(protocol: str, port: int) -> bool:
        for r in rules:
            if r.protocol != protocol:
                continue
            opts = r.tcp_options if protocol == TCP else r.udp_options
            if opts is None or opts.destination_port_range is None:
                return True  # весь диапазон уже открыт
            pr = opts.destination_port_range
            if pr.min <= port <= pr.max:
                return True
        return False

    added = []
    for port in (22, 80, 443, peer_port):
        if not covered(TCP, port):
            rules.append(_ingress(TCP, port))
            added.append(f"{port}/tcp")
    if not covered(UDP, peer_port):
        rules.append(_ingress(UDP, peer_port))
        added.append(f"{peer_port}/udp")
    if added:
        log.info("Opening ports in default security list: %s", ", ".join(added))
        network.update_security_list(
            sl.id,
            oci.core.models.UpdateSecurityListDetails(
                ingress_security_rules=rules,
                egress_security_rules=list(sl.egress_security_rules),
            ),
        )

    # Regional-сабнет (без привязки к AD) -- важно: один сабнет обслуживает
    # попытки запуска во всех Availability Domains региона.
    subnet = _find(
        network.list_subnets(compartment_id=compartment_id, vcn_id=vcn.id).data,
        SUBNET_NAME,
    )
    if subnet is None:
        log.info("Creating public subnet %s (%s)", SUBNET_NAME, SUBNET_CIDR)
        subnet = network.create_subnet(
            oci.core.models.CreateSubnetDetails(
                compartment_id=compartment_id,
                vcn_id=vcn.id,
                cidr_block=SUBNET_CIDR,
                display_name=SUBNET_NAME,
                dns_label="torrsub",
                prohibit_public_ip_on_vnic=False,
            )
        ).data
        oci.wait_until(network, network.get_subnet(subnet.id), "lifecycle_state", "AVAILABLE",
                       max_wait_seconds=300)
    else:
        log.info("Reusing subnet %s", SUBNET_NAME)

    return subnet.id


# --------------------------------------------------------------------------- #
# cloud-init
# --------------------------------------------------------------------------- #
def resolve_domain() -> str:
    """Домен, по которому сервер будет доступен (пусто -> работаем по IP)."""
    custom = os.getenv("TS_DOMAIN", "").strip()
    if custom:
        return custom
    sub = os.getenv("DUCKDNS_SUBDOMAIN", "").strip()
    if not sub:
        return ""
    return sub if "." in sub else f"{sub}.duckdns.org"


def render_cloud_init(peer_port: int) -> Optional[str]:
    """Читает шаблон, подставляет секреты из env, возвращает base64 или None."""
    tpl_path = Path(__file__).with_name("cloud-init") / "torrserver.yaml.tpl"
    if not tpl_path.exists():
        log.warning("cloud-init template not found at %s -- instance will boot bare", tpl_path)
        return None

    domain = resolve_domain()
    duck_sub = os.getenv("DUCKDNS_SUBDOMAIN", "").strip().split(".")[0]

    subs = {
        "__SITE_ADDRESS__": domain if domain else ":80",
        "__DUCKDNS_SUB__": duck_sub,
        "__DUCKDNS_TOKEN__": os.getenv("DUCKDNS_TOKEN", "").strip(),
        "__TS_USER__": os.getenv("TS_USER", "lampa"),
        "__TS_PASS__": os.getenv("TS_PASS", "changeme"),
        "__TS_PEER_PORT__": str(peer_port),
        "__CACHE_BYTES__": str(int(float(os.getenv("TS_CACHE_GB", "2")) * 1024 ** 3)),
    }

    text = tpl_path.read_text(encoding="utf-8")
    for key, val in subs.items():
        text = text.replace(key, val)

    leftovers = {w for w in text.split() if w.startswith("__") and w.endswith("__")}
    if leftovers:
        log.warning("Unsubstituted placeholders in cloud-init: %s", leftovers)

    return base64.b64encode(text.encode("utf-8")).decode("ascii")
