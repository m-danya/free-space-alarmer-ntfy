# free-space-alarmer-ntfy

Проверяет реальные локальные диски и отправляет уведомление в ntfy, если на каком-то из них осталось меньше 10% свободного места.

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
- ntfy topic
- bearer token, опционально
- название машины, по умолчанию берется из `hostname`

После установки создаются:

- `/opt/free-space-alarmer-ntfy/free_space_alarmer_ntfy.py`
- `/etc/free-space-alarmer-ntfy/config.json`
- `/usr/local/bin/free-space-alarmer-ntfy`
- `free-space-alarmer-ntfy.service`
- `free-space-alarmer-ntfy.timer`

Таймер запускает проверку раз в час.

## Тестовое сообщение

Команда отправит проверочное сообщение по всем подходящим дискам на этой машине с текущим уровнем свободного места:

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
