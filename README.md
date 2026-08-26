# oci-retry -> бесплатный TorrServer с HTTPS-адресом

Ловит Always Free инстанс в Oracle Cloud (обходя вечное `Out of host capacity`)
и **сразу разворачивает на нём TorrServer MatriX** за Caddy с сертификатом
Let's Encrypt. На выходе получаешь адрес вида `https://мой-торр.duckdns.org`,
который можно вбить в Lampa: ни `localhost`, ни `192.168.x.x`, ни голого IP.

Всё бесплатно и навсегда: Always Free инстанс не сгорает через 12 месяцев,
а 10 ТБ исходящего трафика в месяц с запасом покрывают домашний просмотр.

## Как это работает

```
GitHub Actions (каждые 15 мин)
   -> oci_arm_retry.py
        ├─ находит свежий образ Ubuntu 24.04 под нужную архитектуру
        ├─ создаёт VCN + публичный сабнет + IGW, открывает 22/80/443/32000
        ├─ по кругу дёргает launch_instance по всем Availability Domains
        └─ поймал -> отдаёт cloud-init и глушит собственное расписание
             -> на инстансе: Docker + TorrServer + Caddy + DuckDNS
             -> Telegram: готовый адрес для Lampa
```

Ничего вручную в консоли OCI создавать не нужно: сеть, порты и образ скрипт
готовит сам, идемпотентно.

## Что нужно сделать один раз

### 1. API-ключ Oracle
Консоль OCI -> аватар -> **My profile** -> **API keys** -> *Add API key* ->
*Generate API key pair* -> скачать приватный ключ. В открывшемся окне
*Configuration file preview* лежат `user`, `fingerprint`, `tenancy`, `region`.

### 2. Домен на DuckDNS
[duckdns.org](https://www.duckdns.org) -> вход через Google/GitHub -> создать
поддомен -> скопировать token. Свой домен тоже можно: положи его в переменную
репозитория `TS_DOMAIN` вместо DuckDNS.

### 3. Секреты репозитория
`Settings` -> `Secrets and variables` -> `Actions` -> *New repository secret*:

| Секрет | Обязателен | Что кладём |
| --- | --- | --- |
| `OCI_PRIVATE_KEY` | да | содержимое скачанного `.pem` целиком |
| `OCI_USER_OCID` | да | `ocid1.user.oc1..` |
| `OCI_TENANCY_OCID` | да | `ocid1.tenancy.oc1..` |
| `OCI_FINGERPRINT` | да | отпечаток ключа |
| `OCI_REGION` | да | например `eu-frankfurt-1` |
| `DUCKDNS_SUBDOMAIN` | да | только имя, например `mytorr` |
| `DUCKDNS_TOKEN` | да | token из DuckDNS |
| `TS_USER` / `TS_PASS` | да | логин и пароль веб-морды TorrServer |
| `OCI_SSH_PUBLIC_KEY` | нет | твой `id_ed25519.pub`, иначе в машину не зайти |
| `OCI_COMPARTMENT_ID` | нет | по умолчанию корневой компартмент |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | нет | уведомление о поимке |

Переменные (`Variables`, не секреты): `TS_DOMAIN`, `OCI_SHAPE_PLAN`.

### 4. Минуты Actions
В приватном репозитории бесплатно 2000 минут/месяц, а расписание `*/15`
сожрёт их за пару дней. Поэтому либо **сделай репозиторий публичным**
(`Settings` -> `General` -> `Change visibility`) -- у публичных репозиториев
минуты Actions не тарифицируются, секреты при этом остаются зашифрованными,
либо оставь приватным и поменяй cron на `0 */4 * * *`.

### 5. Запустить
`Actions` -> *OCI capacity hunter* -> **Run workflow**. Дальше само:
каждые 15 минут новая попытка, при успехе прилетит Telegram с адресом,
а расписание отключится.

### 6. Прописать в Lampa
Настройки -> Торренты -> TorrServer:

```
Адрес:  https://мой-поддомен.duckdns.org
Логин:  TS_USER
Пароль: TS_PASS
```

Первый бут доустанавливает докер и тянет сертификат ~3-5 минут, так что
не пугайся, если сразу после уведомления адрес ещё не отвечает.

## Локальный запуск

```bash
pip install -r requirements.txt
cp .env.example .env   # заполнить
python oci_arm_retry.py
```

## Файлы

| Файл | Зачем |
| --- | --- |
| `oci_arm_retry.py` | цикл попыток, классификация ошибок OCI, уведомления |
| `oci_bootstrap.py` | образ, сеть/порты, рендер cloud-init |
| `cloud-init/torrserver.yaml.tpl` | что ставится на инстансе при первом бутe |
| `.github/workflows/oci-retry.yml` | расписание охоты + самоотключение |
| `.github/workflows/watchdog.yml` | ежедневная проверка живости, алерт в Telegram |

## Грабли, о которых стоит знать

* **Квоту ARM урезали.** С 15.06.2026 Always Free это 2 OCPU / 12 GB вместо
  4 / 24. План шейпов по умолчанию уже 2/12; попытка взять 4/24 вернёт
  `LimitExceeded` (код выхода 3), а не отсутствие капасити.
* **Idle reclamation.** Простаивающий Always Free инстанс Oracle может забрать.
  Раздача торрентов обычно даёт достаточную нагрузку, но watchdog не просто так.
* **Торренты против Acceptable Use Policy Oracle.** Бесплатный аккаунт могут
  закрыть без переписки. Держи бэкап `/opt/torr/ts/torrents.db`.
* **Scheduled workflows** в публичном репозитории отключаются после 60 дней
  без коммитов. Капасити обычно ловится гораздо раньше.
* **Порт пиров** 32000 tcp+udp открывается и в security list, и в iptables
  на самом хосте: в образах OCI Ubuntu INPUT закрыт для всего, кроме 22.
