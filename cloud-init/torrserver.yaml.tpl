#cloud-config
# ---------------------------------------------------------------------------
# TorrServer MatriX + Caddy (Let's Encrypt) + DuckDNS, всё в докере.
# Плейсхолдеры в двойных подчёркиваниях подставляет
# oci_bootstrap.render_cloud_init() из секретов, поэтому в репозитории
# не лежит ни одного пароля.
# ---------------------------------------------------------------------------
package_update: true
package_upgrade: false
packages:
  - ca-certificates
  - curl
  - jq
  - iptables-persistent

write_files:
  # Логин/пароль для TS_HTTPAUTH. Файл должен лежать именно в TS_PATH.
  - path: /opt/torr/ts/accs.db
    permissions: '0600'
    content: |
      {"__TS_USER__": "__TS_PASS__"}

  - path: /opt/torr/docker-compose.yml
    permissions: '0644'
    content: |
      services:
        torrserver:
          image: ghcr.io/yourok/torrserver:latest
          container_name: torrserver
          restart: unless-stopped
          network_mode: host
          environment:
            TS_PORT: "8090"
            TS_PATH: /opt/ts
            TS_HTTPAUTH: "1"
            TS_TORRENTADDR: ":__TS_PEER_PORT__"
            TS_PUBIPV4: "${PUBLIC_IP}"
          volumes:
            - /opt/torr/ts:/opt/ts

        caddy:
          image: caddy:2-alpine
          container_name: caddy
          restart: unless-stopped
          network_mode: host
          volumes:
            - /opt/torr/Caddyfile:/etc/caddy/Caddyfile:ro
            - /opt/torr/caddy/data:/data
            - /opt/torr/caddy/config:/config

  # flush_interval -1 обязателен: без него Caddy буферизует видеопоток.
  - path: /opt/torr/Caddyfile
    permissions: '0644'
    content: |
      __SITE_ADDRESS__ {
          reverse_proxy 127.0.0.1:8090 {
              flush_interval -1
          }
      }

  - path: /usr/local/bin/duckdns-update.sh
    permissions: '0755'
    content: |
      #!/bin/sh
      SUB="__DUCKDNS_SUB__"
      TOKEN="__DUCKDNS_TOKEN__"
      [ -n "$SUB" ] || exit 0
      [ -n "$TOKEN" ] || exit 0
      curl -fsS "https://www.duckdns.org/update?domains=${SUB}&token=${TOKEN}&ip=" \
        -o /var/log/duckdns.log 2>/dev/null
      exit 0

  - path: /etc/systemd/system/duckdns.service
    permissions: '0644'
    content: |
      [Unit]
      Description=Update DuckDNS record
      After=network-online.target
      Wants=network-online.target

      [Service]
      Type=oneshot
      ExecStart=/usr/local/bin/duckdns-update.sh

  - path: /etc/systemd/system/duckdns.timer
    permissions: '0644'
    content: |
      [Unit]
      Description=Update DuckDNS every 5 minutes

      [Timer]
      OnBootSec=30
      OnUnitActiveSec=5min

      [Install]
      WantedBy=timers.target

  - path: /usr/local/bin/torr-tune.sh
    permissions: '0755'
    content: |
      #!/bin/sh
      # Кэш держим в RAM: диск на Always Free медленный, а SSD жалко.
      for i in $(seq 1 60); do
        if curl -fsS -m 3 http://127.0.0.1:8090/echo >/dev/null 2>&1; then
          break
        fi
        sleep 5
      done
      curl -fsS -m 10 -u "__TS_USER__:__TS_PASS__" \
        -X POST http://127.0.0.1:8090/settings \
        -H 'Content-Type: application/json' \
        -d '{"action":"set","sets":{"CacheSize":__CACHE_BYTES__,"UseDisk":false,"PreloadBuffer":true,"ReaderReadAHead":95,"RemoveCacheOnDrop":true}}' \
        >/dev/null 2>&1
      exit 0

runcmd:
  # --- 1. Хостовый firewall. В образах OCI INPUT закрыт всё, кроме 22. ---
  - iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
  - iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT
  - iptables -I INPUT 1 -p tcp --dport __TS_PEER_PORT__ -j ACCEPT
  - iptables -I INPUT 1 -p udp --dport __TS_PEER_PORT__ -j ACCEPT
  - netfilter-persistent save

  # --- 2. DNS: сначала прописываем IP в DuckDNS, иначе Let's Encrypt не выдаст сертификат ---
  - systemctl daemon-reload
  - systemctl enable --now duckdns.timer
  - /usr/local/bin/duckdns-update.sh
  - sleep 20

  # --- 3. Docker (скрипт сам определит arm64/amd64 и поставит compose-плагин) ---
  - curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  - sh /tmp/get-docker.sh
  - systemctl enable --now docker

  # --- 4. Публичный IP -> TS_PUBIPV4, чтобы пиры видели нас корректно ---
  - |
    PUB=$(curl -fsS -m 5 -H "Authorization: Bearer Oracle" \
      http://169.254.169.254/opc/v2/vnics/ | jq -r '.[0].publicIp' 2>/dev/null)
    if [ -z "$PUB" ] || [ "$PUB" = "null" ]; then
      PUB=$(curl -fsS -m 5 https://api.ipify.org)
    fi
    echo "PUBLIC_IP=${PUB}" > /opt/torr/.env

  # --- 5. Поехали ---
  - cd /opt/torr && docker compose up -d
  - /usr/local/bin/torr-tune.sh
  - touch /opt/torr/READY

final_message: "TorrServer готов после $UPTIME секунд"
