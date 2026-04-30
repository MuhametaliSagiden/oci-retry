# oci-retry — GitHub Actions backup

Бесконечный retry для создания Always Free A1.Flex (24 GB ARM) в OCI,
работающий на бесплатных раннерах GitHub.

## Setup (один раз)

1. **Создай ПРИВАТНЫЙ** репо на GitHub: https://github.com/new (важно — приватный, чтобы секреты были только у тебя). Назови как угодно, например `oci-retry`.

2. Скопируй файлы из этого пакета в репо и запушь:
   ```bash
   cd <папка с распакованным пакетом>
   git init
   git add .
   git commit -m "OCI retry"
   git branch -M main
   git remote add origin https://github.com/<твой-юзер>/oci-retry.git
   git push -u origin main
   ```

3. Добавь секреты: **Settings → Secrets and variables → Actions → New repository secret**.
   Список ниже.

4. Запусти workflow вручную проверить: **Actions → OCI ARM retry → Run workflow**.
   Должны пойти попытки, в логах увидишь "Out of capacity ... Sleep N s.".

5. Дальше cron `*/10` сам работает.

## Список секретов

Все обязательные:

| Имя | Значение | Откуда |
| --- | --- | --- |
| `OCI_PRIVATE_KEY` | Содержимое .pem файла **целиком** (BEGIN/END + body, с переносами строк) | твой `oci_api_key.pem` |
| `OCI_USER_OCID` | `ocid1.user.oc1..xxx` | OCI Console → My profile → OCID |
| `OCI_FINGERPRINT` | `aa:bb:cc:...` | Configuration preview, строка `fingerprint=` |
| `OCI_TENANCY_OCID` | `ocid1.tenancy.oc1..xxx` | Profile → Tenancy → OCID |
| `OCI_REGION` | `eu-frankfurt-1` | Slug, не "Germany Central" |
| `OCI_COMPARTMENT_ID` | `ocid1.tenancy.oc1..xxx` (= tenancy OCID) | то же |
| `OCI_SUBNET_ID` | `ocid1.subnet.oc1.<region>.xxx` | Networking → VCN → Subnets |
| `OCI_IMAGE_ID` | `ocid1.image.oc1.<region>.xxx` | Compute → Custom Images или известный ID |
| `OCI_SSH_PUBLIC_KEY` | `ssh-ed25519 AAAA... user@host` | `cat ~/.ssh/id_ed25519.pub` |

Опционально (Telegram-уведомление при успехе):

| Имя | Значение |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Токен бота из @BotFather |
| `TELEGRAM_CHAT_ID` | Твой chat_id |

## Лимиты GitHub Actions

Личный аккаунт: **2000 минут/месяц** бесплатно.
Cron `*/10` × ~9 мин = ~3900 мин/мес → переберёшь лимит. Поставь `*/15` или `*/20` если нужно надолго.

После успеха инстанс уже в OCI — workflow можно отключить (Actions → ... → Disable workflow).
