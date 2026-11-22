<div align="center">

# easy-3x-ui

**Установщик 3x-ui и Xray для Debian.**

Одна команда для установки панели, подписок и VPN-узла с Certbot или Caddy.

[![Release](https://img.shields.io/github/v/release/theillarion/easy-3x-ui?display_name=tag&label=Release&logo=github)](https://github.com/theillarion/easy-3x-ui/releases/latest)
[![Stars](https://img.shields.io/github/stars/theillarion/easy-3x-ui?style=flat&label=Stars&logo=github&color=E3A21A)](https://github.com/theillarion/easy-3x-ui/stargazers)
[![CI status](https://img.shields.io/github/actions/workflow/status/theillarion/easy-3x-ui/ci.yml?label=CI&logo=githubactions)](https://github.com/theillarion/easy-3x-ui/actions/workflows/ci.yml)

[![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](#требования)
[![MIT License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[Содержание](#содержание) · [Установка](#установка) · [Настройки](#настройки) · [Управление](#управление-установкой) · [Изменения](CHANGELOG.md) · [Лицензия](#лицензия)

</div>

---

## Содержание

1. [Что умеет](#что-умеет)
2. [Требования](#требования)
3. [Установка](#установка)
   1. [Интерактивный запуск](#интерактивный-запуск)
   2. [Запуск с YAML](#запуск-с-yaml)
4. [Настройки](#настройки)
5. [Управление установкой](#управление-установкой)
6. [Проверка подключений](#проверка-подключений)
7. [Тестирование](#тестирование)
8. [История изменений](CHANGELOG.md)
9. [Лицензия](#лицензия)

## Что умеет

- Настраивает отдельные HTTPS-адреса для панели и подписок.
- Поднимает TCP REALITY и прямой XHTTP REALITY. В режиме Caddy можно добавить XHTTP через CDN.
- Устанавливает системный Docker и добавляет выбранного пользователя в группу `docker`. Чтобы пользоваться Docker без `sudo`, войди в систему заново.
- Делает бэкапы, восстанавливает 3x-ui из файла базы и обновляет компоненты установки.

## Требования

- Сервер с Debian 12 или 13, systemd и доступом к `sudo`.
- Два DNS-имени для панели и подписок, указывающие на VPS. Для CDN также нужны домен origin на VPS и публичный домен edge у провайдера.
- Входящий TCP `80` для выпуска сертификатов HTTP-01. Остальные порты зависят от выбранного режима и инбаундов.

## Установка

### Интерактивный запуск

Установщик можно запускать напрямую с GitHub.

```sh
sudo sh -c "$(curl -fsSL https://raw.githubusercontent.com/theillarion/easy-3x-ui/master/install.sh)"
```

### Запуск с YAML

Для запуска с готовым YAML передай путь к файлу на сервере.

```sh
sudo sh -c "$(curl -fsSL https://raw.githubusercontent.com/theillarion/easy-3x-ui/master/install.sh)" -- --config /path/to/config.yml
```

При запуске с GitHub скрипт скачивает исходники во временный каталог и удаляет его при завершении. Из локальной копии проекта можно запустить `sudo ./install.sh` или `sudo ./install.sh --config /path/to/config.yml`.

Перед настройкой Docker установщик проверяет часы. Он использует существующую службу NTP или устанавливает `systemd-timesyncd`, если службы нет. На синхронизацию даётся до 60 секунд.

| Возможность | Certbot | Caddy |
| --- | --- | --- |
| HTTPS панели | 3x-ui на `2053` | Caddy на `443` |
| HTTPS подписок | 3x-ui на `2096` | Caddy на `443` |
| TCP REALITY при включении | Xray на `443` | Caddy принимает соединение на `443` и по SNI передаёт его Xray на локальном `8444` |
| Прямой XHTTP REALITY при включении | Xray на `8443` | Xray на `8443` |
| XHTTP через CDN | — | Caddy передаёт запросы на локальные порты Xray, начиная с `8080` |
| Сертификаты | Certbot с таймером systemd | Caddy |

## Настройки

Поля, значения по умолчанию и примеры описаны в [справочнике YAML](docs/configuration.md). Остальные материалы собраны в [оглавлении документации](docs/README.md).

## Управление установкой

```sh
sudo setup-3x-ui status
sudo setup-3x-ui discover
sudo setup-3x-ui backup
sudo setup-3x-ui restore --from /path/to/x-ui.db
sudo setup-3x-ui update --component panel
sudo setup-3x-ui update --component panel --rotate-token
sudo setup-3x-ui reset
sudo setup-3x-ui uninstall
sudo setup-3x-ui uninstall --purge-data
```

Чтобы обновить саму утилиту, запусти установщик с GitHub с командой `update`:

```sh
sudo sh -c "$(curl -fsSL https://raw.githubusercontent.com/theillarion/easy-3x-ui/master/install.sh)" -- update --component installer
```

Бэкап содержит базу SQLite, `config.yml`, `compose.yml` и Caddyfile, если он используется. При восстановлении на действующем сервере адреса панели и подписок сохраняются. Для чистой установки из бэкапа используй `sudo ./install.sh --config /path/to/config.yml --from /path/to/x-ui.db`. После восстановления входи с логином, паролем и, если включена 2FA, свежим кодом из прежнего аутентификатора.

Команда `sudo setup-3x-ui adopt --config /path/to/config.yml` переносит поддерживаемую старую установку на системном Docker Compose. Утилита архивирует прежний каталог и восстанавливает базу с тем же идентификатором панели. Для переноса установки с rootless Docker используй бэкап базы.

Чтобы применить настройки, отредактируй копию рабочего YAML и запусти `sudo setup-3x-ui --config /path/to/config.yml`. Сохрани текущий режим HTTPS и порты. При смене пути XHTTP или SNI укажи тег существующего инбаунда. Для добавления CDN добавь запись в список `cdn`.

## Проверка подключений

Команда `test` показывает результаты прямых запросов и запросов через VPN отдельно. Она принимает ссылку VLESS, URL подписки или файл со списком ссылок, в том числе в base64. Укажи путь до Xray через `--xray` либо используй `--download-xray`, чтобы временно скачать официальный бинарник для Linux.

```sh
setup-3x-ui test --config /path/to/config.yml --xray /usr/local/bin/xray
setup-3x-ui test --link-file /path/to/private-links.txt --download-xray --json
```

Журнал установки и обновления образов хранится в `logs/setup.log`.

## Тестирование

Из каталога проекта:

```sh
python3 -m unittest discover -s tests
./setup-3x-ui validate --config config.example.yml
sh -n install.sh && sh -n setup-3x-ui
```

[Полный прогон в Docker и покрытие](docs/testing.md) · [Структура проекта](docs/project-layout.md) · [Будущие фичи](features/README.md) · [История изменений](CHANGELOG.md)

## Лицензия

Код установщика распространяется по [лицензии MIT](LICENSE).

Конфиг `xray-default.json` взят из 3x-ui v3.11.0 и распространяется по GPLv3. [Источник](setup3ui/assets/xray-default.NOTICE) · [Лицензия](setup3ui/assets/xray-default.LICENSE)

В образ Caddy входят Caddy и модуль Layer 4 под Apache 2.0. Тексты их лицензий находятся в `/usr/share/licenses/caddy/` внутри образа.
