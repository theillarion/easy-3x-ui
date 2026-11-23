# Структура проекта

| Путь | Что здесь лежит |
| --- | --- |
| `install.sh`, `setup-3x-ui` | Установка и запуск CLI из каталога проекта. |
| `setup_3x_ui.py` | Точка входа для shell-скриптов и установленной команды. |
| `setup3ui/cli.py`, `setup3ui/config.py` | Команды CLI и правила чтения YAML. |
| `setup3ui/host/` | Подготовка Debian, Docker, NTP, DNS и проверка портов. |
| `setup3ui/panel/` | 3x-ui API, инбаунды, HTTPS и база панели. |
| `setup3ui/deploy/` | Чистая установка, Compose, Caddy, управление установкой и обновление утилиты. |
| `setup3ui/certificates/` | Выпуск сертификатов Certbot и его таймер. |
| `setup3ui/diagnostics/` | Команда `test` и загрузка Xray для проверок. |
| `setup3ui/assets/` | Docker Compose, Dockerfile Caddy, Xray-шаблоны и файлы сайта. |
| `tests/` | Автоматические проверки. |
| `docs/`, `features/` | Документация и идеи будущих фич. |

Запусти тесты командой `python3 -m unittest discover -s tests`.

## Установка и обновление утилиты из GitHub

Корневой `install.sh` доступен по raw-ссылке на ветке `master`. Он скачивает обычный архив исходников GitHub во временный каталог и запускает CLI. При запуске из локальной копии проекта установщик берёт исходники из этой копии.

```sh
sudo sh -c "$(curl -fsSL https://raw.githubusercontent.com/theillarion/easy-3x-ui/master/install.sh)"
```

Чтобы обновить установленную утилиту, включая переход со старой структуры, передай `update --component installer` тому же загрузчику.

```sh
sudo sh -c "$(curl -fsSL https://raw.githubusercontent.com/theillarion/easy-3x-ui/master/install.sh)" -- update --component installer
```
