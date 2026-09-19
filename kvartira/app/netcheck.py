"""Разбор причины, по которой не удалось связаться с сервисом.

«Не удалось связаться» — бесполезная фраза: непонятно, что чинить.
Здесь связь проверяется по шагам, и человек получает конкретный ответ:
не находится адрес, не пускает брандмауэр, мешает антивирус или
сервис просто не отвечает.
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlparse

from .errors import get_logger

log = get_logger()

STEP_TIMEOUT = 8.0


@dataclass
class Diagnosis:
    message: str      # что случилось
    hint: str         # что делать
    technical: str    # короткая строка для разработчика


def _host_and_port(base_url: str) -> tuple[str, int]:
    parsed = urlparse(base_url if "//" in base_url else f"https://{base_url}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname or "", port


def diagnose(base_url: str, error: Exception | None = None) -> Diagnosis:
    """Проверяет связь по шагам: имя → соединение → шифрование."""
    technical = f"{type(error).__name__}: {error}"[:200] if error else ""
    host, port = _host_and_port(base_url)

    if not host:
        return Diagnosis(
            "В настройках указан неверный адрес сервиса.",
            "Откройте файл .env и проверьте строки PLAN_BASE_URL и IMAGE_BASE_URL. "
            "Для AITunnel адрес такой: https://api.aitunnel.ru/v1",
            technical or f"адрес не разобран: {base_url!r}",
        )

    # Шаг 1. Находится ли имя сервиса в сети.
    try:
        socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return Diagnosis(
            "Компьютер не смог найти сервис в сети.",
            "Проверьте, что интернет работает. Если работает — адрес сервиса "
            "могут блокировать антивирус, брандмауэр или провайдер. "
            "Попробуйте открыть сайт сервиса в браузере: если он тоже не "
            "открывается, дело точно не в программе.",
            f"DNS {host}: {type(exc).__name__}: {exc}"[:200],
        )

    # Шаг 2. Устанавливается ли соединение вообще.
    try:
        with socket.create_connection((host, port), timeout=STEP_TIMEOUT):
            pass
    except socket.timeout as exc:
        return Diagnosis(
            "Сервис не отвечает.",
            "Соединение открывается, но ответа нет. Обычно это временно — "
            "подождите пару минут и нажмите «Проверить доступ» ещё раз. "
            "Если не проходит и дальше — проверьте, не блокирует ли программу "
            "антивирус или брандмауэр.",
            f"TCP {host}:{port}: время вышло ({exc})"[:200],
        )
    except OSError as exc:
        return Diagnosis(
            "Соединение с сервисом не устанавливается.",
            "Чаще всего это антивирус или брандмауэр: они не выпускают "
            "программу в интернет. Добавьте её в исключения или временно "
            "отключите защиту и попробуйте снова. Если включён VPN — "
            "выключите его.",
            f"TCP {host}:{port}: {type(exc).__name__}: {exc}"[:200],
        )

    # Шаг 3. Договариваются ли стороны о шифровании.
    if port == 443:
        try:
            context = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=STEP_TIMEOUT) as raw:
                with context.wrap_socket(raw, server_hostname=host):
                    pass
        except ssl.SSLCertVerificationError as exc:
            return Diagnosis(
                "Защищённое соединение отклонено: сертификат сервиса не признан.",
                "Почти всегда виноват антивирус, который проверяет защищённый "
                "трафик — Kaspersky, ESET, Dr.Web и подобные. В его настройках "
                "найдите «проверка защищённых соединений» или «проверка HTTPS» "
                "и отключите её либо добавьте программу в исключения.",
                f"TLS {host}: {type(exc).__name__}: {exc}"[:200],
            )
        except OSError as exc:
            return Diagnosis(
                "Не удалось установить защищённое соединение.",
                "Обычно мешает антивирус или корпоративная сеть. Попробуйте "
                "отключить проверку HTTPS в антивирусе, выключить VPN или "
                "подключиться к другой сети — например, раздать интернет с телефона.",
                f"TLS {host}: {type(exc).__name__}: {exc}"[:200],
            )

    # Связь есть, а запрос всё равно не прошёл.
    return Diagnosis(
        "Сервис доступен, но запрос до него не дошёл.",
        "Проверьте адрес сервиса в файле .env — возможно, в нём опечатка. "
        "Для AITunnel адрес такой: https://api.aitunnel.ru/v1",
        technical or f"{host}:{port} отвечает, но запрос не выполнен",
    )
