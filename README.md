# free-space-alarmer-ntfy

Проверяет реальные локальные диски и отправляет уведомление в настроенные каналы, если на каком-то из них осталось меньше заданного процента свободного места. Поддерживаются ntfy и Mattermost incoming webhook. По умолчанию порог — 10%, уведомления отправляются только с 10:00 по 20:00 по локальному времени машины.

Сообщение выглядит так:

```text
🚨 На диске <disk> (<mount point>) на машине <machine> осталось <N>% свободного места (<M> GB)
```

## Что учитывается

Скрипт читает `/proc/self/mountinfo`, проверяет место через `statvfs` и оставляет только локальные дисковые файловые системы. Подходят, например:

- `/dev/sda3` на `/`
- `/dev/nvme0n1p4` на `/home`
- `/dev/sda1` на `/space`
- `/dev/sdd1` на `/mnt/Transcend`

Не учитываются временные, системно-виртуальные и сетевые mount points: `tmpfs`, `devtmpfs`, `efivarfs`, `proc`, `sysfs`, `overlay`, `nfs`, `cifs`, `sshfs`, источники вида `host:/path` и похожие.

Если один и тот же filesystem смонтирован в несколько мест, он проверяется один раз, чтобы не отправлять дубли про один и тот же пул свободного места.

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
- номера дисков для blacklist; перед этим инсталлер покажет test-сообщения по всем найденным дискам

Для ntfy и Mattermost можно ввести `-` вместо URL, чтобы отключить канал. У ntfy дефолт берется из существующего конфига, если он есть. У Mattermost по умолчанию выбран `-`, то есть канал отключен.

После установки создаются:

- `/opt/free-space-alarmer-ntfy/free_space_alarmer_ntfy.py`
- `/etc/free-space-alarmer-ntfy/config.json`
- `/usr/local/bin/free-space-alarmer-ntfy`
- `free-space-alarmer-ntfy.service`
- `free-space-alarmer-ntfy.timer`

Таймер запускает проверку раз в час. Скрипт сам пропускает обычные уведомления вне настроенного временного окна.

На шаге blacklist можно указать номера через пробел. Если просто нажать Enter, по умолчанию будут исключены mount points, где есть `/boot`.

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
  "blacklist": {
    "mount_points": ["/boot", "/boot/efi"]
  }
}
```

Если оба канала отключены, скрипт не отправляет уведомления наружу, но пишет сформированные сообщения в stdout/journal.

`notify_not_before` и `notify_not_after` задаются в формате `HH:MM` и сравниваются с текущим локальным временем машины.

## Тестовое сообщение

Команда отправит проверочное сообщение по всем подходящим дискам на этой машине с текущим уровнем свободного места. Blacklist из конфига учитывается, порог и временное окно игнорируются:

```bash
sudo free-space-alarmer-ntfy --config /etc/free-space-alarmer-ntfy/config.json --test
```

Посмотреть, какие диски будут учитываться, без отправки уведомлений:

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
