#!/usr/bin/env python3
"""
oci_bootstrap.py
================

Всё, что нужно подготовить в тенанте ДО запуска инстанса, чтобы от человека
не требовалось ни одного клика в консоли OCI:

  * resolve_image()     -- сам находит свежий образ Canonical Ubuntu под нужный
                           shape (aarch64 для A1.Flex, x86 для E2.1.Micro);
  * ensure_network()    -- ПЕРЕИСПОЛЬЗУЕТ существующую сеть, если она есть, и
                           создаёт новую только когда её нет вообще. Always Free
                           тенант жёстко ограничен по vcn-count (обычно 2), так
                           что слепое создание упирается в LimitExceeded;
  * render_cloud_init() -- подставляет секреты в шаблон cloud-init и отдаёт
                           base64, готовый для metadata.user_data.

Все операции идемпотентны: повторный вызов ничего не ломает и не плодит дубли.
"""
from __future__ import annotations

import base64
import ipaddress
import logging
import os
from pathlib import Path
from typing import List, Optional

import oci
from oci.exceptions import ServiceError

log = logging.getLogger("oci-bootstrap")

VCN_NAME = os.getenv("OCI_VCN_NAME", "torr-vcn")
VCN_CIDR = os.getenv("OCI_VCN_CIDR", "10.0.0.0/16")
SUBNET_NAME = os.getenv("OCI_SUBNET_NAME", "torr-subnet")
SUBNET_PREFIX = int(os.getenv("OCI_SUBNET_PREFIX", "24"))

TCP = "6"
UDP = "17"

ALIVE = ("PROVISIONING", "AVAILABLE", "UPDATING")


def _is_limit(err: ServiceError) -> bool:
    return err.status == 400 and (err.code or "").lower() in {"limitexceeded", "quotaexceeded"}


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
def _alive(items) -> list:
    return [i for i in items if getattr(i, "lifecycle_state", None) in ALIVE]


def _by_name(items, name: str):
    for it in items:
        if it.display_name == name:
            return it
    return None


def _vcn_cidrs(vcn) -> List[str]:
    blocks = list(getattr(vcn, "cidr_blocks", None) or [])
    if not blocks and getattr(vcn, "cidr_block", None):
        blocks = [vcn.cidr_block]
    return blocks


def _free_cidr(vcn, taken: List[str], prefix: int) -> Optional[str]:
    """Первый свободный блок /prefix внутри адресного пространства VCN."""
    taken_nets = []
    for c in taken:
        try:
            taken_nets.append(ipaddress.ip_network(c))
        except ValueError:
            continue
    for block in _vcn_cidrs(vcn):
        try:
            net = ipaddress.ip_network(block)
        except ValueError:
            continue
        if net.prefixlen > prefix:
            continue
        for cand in net.subnets(new_prefix=prefix):
            if not any(cand.overlaps(t) for t in taken_nets):
                return str(cand)
    return None


def _ensure_vcn(network, compartment_id: str):
    """Своя VCN, иначе любая существующая, иначе создаём. Лимит vcn-count в Always Free мал."""
    existing = _alive(network.list_vcns(compartment_id=compartment_id).data)

    mine = _by_name(existing, VCN_NAME)
    if mine is not None:
        log.info("Reusing VCN %s", mine.display_name)
        return mine

    if existing:
        # Создавать новую нельзя: упрёмся в vcn-count. Живём в той, что есть.
        vcn = existing[0]
        log.info("VCN %s not found, reusing existing VCN %s (%s) to stay within vcn-count",
                 VCN_NAME, vcn.display_name, ", ".join(_vcn_cidrs(vcn)))
        return vcn

    log.info("Creating VCN %s (%s)", VCN_NAME, VCN_CIDR)
    try:
        vcn = network.create_vcn(
            oci.core.models.CreateVcnDetails(
                compartment_id=compartment_id,
                cidr_block=VCN_CIDR,
                display_name=VCN_NAME,
                dns_label="torrvcn",
            )
        ).data
    except ServiceError as e:
        if not _is_limit(e):
            raise
        retry = _alive(network.list_vcns(compartment_id=compartment_id).data)
        if not retry:
            raise RuntimeError(
                "vcn-count лимит исчерпан, но ни одной VCN не видно в этом компартменте. "
                "Проверь другие компартменты или удали старую VCN в консоли OCI."
            ) from e
        log.warning("vcn-count limit hit, reusing existing VCN %s", retry[0].display_name)
        return retry[0]

    oci.wait_until(network, network.get_vcn(vcn.id), "lifecycle_state", "AVAILABLE",
                   max_wait_seconds=300)
    return vcn


def _ensure_igw(network, compartment_id: str, vcn_id: str):
    """Internet Gateway: любой существующий сойдёт, их всё равно можно только один на VCN."""
    existing = _alive(
        network.list_internet_gateways(compartment_id=compartment_id, vcn_id=vcn_id).data
    )
    if existing:
        log.info("Reusing Internet Gateway %s", existing[0].display_name)
        return existing[0]

    log.info("Creating Internet Gateway")
    igw = network.create_internet_gateway(
        oci.core.models.CreateInternetGatewayDetails(
            compartment_id=compartment_id,
            vcn_id=vcn_id,
            display_name="torr-igw",
            is_enabled=True,
        )
    ).data
    oci.wait_until(network, network.get_internet_gateway(igw.id), "lifecycle_state",
                   "AVAILABLE", max_wait_seconds=300)
    return igw


def _ensure_route(network, route_table_id: str, igw_id: str) -> None:
    rt = network.get_route_table(route_table_id).data
    if any(r.network_entity_id == igw_id for r in rt.route_rules):
        return
    if any(r.destination == "0.0.0.0/0" for r in rt.route_rules):
        log.warning("Route table %s already routes 0.0.0.0/0 elsewhere, leaving as is",
                    rt.display_name)
        return
    log.info("Adding default route 0.0.0.0/0 -> IGW in %s", rt.display_name)
    network.update_route_table(
        rt.id,
        oci.core.models.UpdateRouteTableDetails(
            route_rules=list(rt.route_rules) + [
                oci.core.models.RouteRule(
                    destination="0.0.0.0/0",
                    destination_type="CIDR_BLOCK",
                    network_entity_id=igw_id,
                )
            ]
        ),
    )


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


def _ensure_ports(network, security_list_id: str, peer_port: int) -> None:
    sl = network.get_security_list(security_list_id).data
    rules = list(sl.ingress_security_rules)

    def covered(protocol: str, port: int) -> bool:
        for r in rules:
            if r.protocol not in (protocol, "all"):
                continue
            if r.protocol == "all":
                return True
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
    if not added:
        return

    log.info("Opening ports in %s: %s", sl.display_name, ", ".join(added))
    network.update_security_list(
        sl.id,
        oci.core.models.UpdateSecurityListDetails(
            ingress_security_rules=rules,
            egress_security_rules=list(sl.egress_security_rules),
        ),
    )


def ensure_network(network, compartment_id: str, peer_port: int) -> str:
    """Возвращает OCID публичного сабнета, переиспользуя всё, что уже есть."""
    vcn = _ensure_vcn(network, compartment_id)
    igw = _ensure_igw(network, compartment_id, vcn.id)

    subnets = _alive(
        network.list_subnets(compartment_id=compartment_id, vcn_id=vcn.id).data
    )
    public = [s for s in subnets if not s.prohibit_public_ip_on_vnic]

    subnet = _by_name(public, SUBNET_NAME)
    if subnet is None and public:
        subnet = public[0]
        log.info("Reusing public subnet %s (%s)", subnet.display_name, subnet.cidr_block)
    elif subnet is not None:
        log.info("Reusing subnet %s", subnet.display_name)

    if subnet is None:
        cidr = _free_cidr(vcn, [s.cidr_block for s in subnets], SUBNET_PREFIX)
        if cidr is None:
            raise RuntimeError(
                f"В VCN {vcn.display_name} нет свободного блока /{SUBNET_PREFIX} и нет "
                "публичного сабнета. Освободи место или удали лишний сабнет в консоли OCI."
            )
        log.info("Creating public subnet %s (%s)", SUBNET_NAME, cidr)
        try:
            subnet = network.create_subnet(
                oci.core.models.CreateSubnetDetails(
                    compartment_id=compartment_id,
                    vcn_id=vcn.id,
                    cidr_block=cidr,
                    display_name=SUBNET_NAME,
                    prohibit_public_ip_on_vnic=False,
                )
            ).data
        except ServiceError as e:
            if not _is_limit(e) or not subnets:
                raise
            subnet = subnets[0]
            log.warning("subnet limit hit, falling back to existing subnet %s",
                        subnet.display_name)
        else:
            oci.wait_until(network, network.get_subnet(subnet.id), "lifecycle_state",
                           "AVAILABLE", max_wait_seconds=300)

    # Маршрут и порты правим ровно у той сети, в которой окажется инстанс.
    _ensure_route(network, subnet.route_table_id or vcn.default_route_table_id, igw.id)
    for sl_id in (subnet.security_list_ids or [vcn.default_security_list_id]):
        _ensure_ports(network, sl_id, peer_port)

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
