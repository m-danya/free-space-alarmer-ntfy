# free-space-alarmer-ntfy

Проверяет реальные локальные диски и отправляет уведомление в настроенные каналы, если на каком-то из них осталось меньше заданного процента свободного места. Может дополнительно ходить по SSH на машины из `~/.ssh/config` пользователя, который запускал установку. Поддерживаются ntfy и Mattermost incoming webhook. По умолчанию порог — 10%, уведомления отправляются только с 10:00 по 20:00 по локальному времени машины.

Сообщение выглядит так:

```text
🚨 На диске <disk> (<mount point>) на машине **<machine>** осталось <N>% свободного места (<M> GB)
```

## Что учитывается

Скрипт читает `/proc/self/mountinfo`, проверяет место через `statvfs` и оставляет только локальные дисковые файловые системы. Подходят, например:

- `/dev/sda3` на `/`
- `/dev/nvme0n1p4` на `/home`
- `/dev/sda1` на `/space`
- `/dev/sdd1` на `/mnt/Transcend`

Не учитываются временные, системно-виртуальные и сетевые mount points: `tmpfs`, `devtmpfs`, `efivarfs`, `proc`, `sysfs`, `overlay`, `nfs`, `cifs`, `sshfs`, источники вида `host:/path` и похожие.

Если один и тот же filesystem смонтирован в несколько мест, он проверяется один раз, чтобы не отправлять дубли про один и тот же пул свободного места.

Если включен SSH-обход, на удаленной машине запускается тот же Python-скрипт в probe-режиме через `ssh <host> python3 - --probe-json`. Удаленной машине нужен доступ по SSH без интерактивного пароля и `python3`; `uv` на удаленной машине не нужен.

## Установка

Нужны `uv`, `systemd` и права `sudo`.

```bash
chmod +x install.sh uninstall.sh free_space_alarmer_ntfy.py
./install.sh
```

Инсталлер спросит:

- ntfy base URL, например `https://ntfy-base-server.ru`
- ntfy topic, если ntfy включен
- bearer token, опционально
- Mattermost incoming webhook URL, опционально
- название машины, по умолчанию берется из `hostname`, если команда доступна
- порог свободного места в процентах, по умолчанию `10`
- время, раньше которого не отправлять уведомления, по умолчанию `10:00`
- время, позже которого не отправлять уведомления, по умолчанию `20:00`
- период проверки в часах, по умолчанию `1`
- `Check disks on SSH hosts from your config?`; перед вопросом инсталлер перечислит конкретные `Host`-алиасы из `~/.ssh/config`
- номера дисков для blacklist; перед этим инсталлер покажет test-сообщения по всем найденным дискам

Для ntfy и Mattermost можно ввести `-` вместо URL, чтобы отключить канал. У ntfy дефолт берется из существующего конфига, если он есть. У Mattermost по умолчанию выбран `-`, то есть канал отключен.

Для SSH берутся конкретные `Host` из ssh config пользователя-установщика. Wildcard-записи вроде `Host *` или `Host prod-*` не перечисляются. Systemd-сервис запускается от пользователя, который запускал установку, чтобы `ssh` использовал его config, ключи и `known_hosts`.

После установки создаются:

- `/opt/free-space-alarmer-ntfy/free_space_alarmer_ntfy.py`
- `/etc/free-space-alarmer-ntfy/config.json`
- `/var/lib/free-space-alarmer-ntfy/state.json`
- `/usr/local/bin/free-space-alarmer-ntfy`
- `free-space-alarmer-ntfy.service`
- `free-space-alarmer-ntfy.timer`

Таймер запускает проверку с настроенной периодичностью. Скрипт сам пропускает обычные уведомления вне настроенного временного окна.

На шаге blacklist можно указать номера через пробел. Если просто нажать Enter, по умолчанию будут исключены mount points `/boot`, `/boot/...`, `/dump` и `/dump/...`.

Настройки хранятся в `/etc/free-space-alarmer-ntfy/config.json`:

```json
{
  "ntfy_base_url": "https://ntfy-base-server.ru",
  "ntfy_topic": "topic",
  "ntfy_bearer_token": null,
  "mattermost_webhook_url": null,
  "threshold_free_percent": 10.0,
  "notify_not_before": "10:00",
  "notify_not_after": "20:00",
  "timer_interval_hours": 1,
  "repeat_alert_interval_hours": 24,
  "alert_state_path": "/var/lib/free-space-alarmer-ntfy/state.json",
  "ssh": {
    "enabled": true,
    "config_file": "/home/user/.ssh/config",
    "connect_timeout_seconds": 10,
    "command_timeout_seconds": 60
  },
  "blacklist": {
    "mount_points": ["/boot", "/boot/efi"]
  }
}
```

Если оба канала отключены, скрипт не отправляет уведомления наружу, но пишет сформированные сообщения в stdout/journal.

`notify_not_before` и `notify_not_after` задаются в формате `HH:MM` и сравниваются с текущим локальным временем машины.

`repeat_alert_interval_hours` ограничивает повторные уведомления по одному и тому же диску на одной и той же машине. По умолчанию повторный алерт отправляется не чаще одного раза в 24 часа. Время последней успешной отправки хранится в `alert_state_path`; когда диск снова становится выше порога, запись для него очищается.

## Тестовое сообщение

Команда отправит проверочное сообщение по всем подходящим дискам на этой машине и на включенных SSH-машинах с текущим уровнем свободного места. Blacklist из конфига учитывается, порог и временное окно игнорируются:

```bash
free-space-alarmer-ntfy --config /etc/free-space-alarmer-ntfy/config.json --test
```

Запускайте ручную проверку от того же пользователя, который запускал установку, чтобы использовались его SSH config и ключи.

Посмотреть, какие диски будут учитываться локально и на включенных SSH-машинах, без отправки уведомлений:

```bash
free-space-alarmer-ntfy --list-disks
```

Запустить обычную проверку вручную:

```bash
sudo systemctl start free-space-alarmer-ntfy.service
```

## Удаление

```bash
./uninstall.sh
```
