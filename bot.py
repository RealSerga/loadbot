import asyncio
import importlib
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from typing import Optional

import psutil
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter


def _load_settings_module() -> tuple[ModuleType, str]:
    for module_name in ("settings_prod", "settings"):
        try:
            return importlib.import_module(module_name), module_name
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
    raise SystemExit("Не найден файл настроек: setting_prod.py или setting.py")


settings, SETTINGS_MODULE_NAME = _load_settings_module()


@dataclass
class Config:
    token: str
    channel_id: str
    interface: str
    update_interval: float
    message_id: Optional[int]


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _pick_interface() -> str:
    """Return the first active non-loopback interface or any available one."""
    stats = psutil.net_if_stats()
    if not stats:
        raise SystemExit("Нет сетевых интерфейсов, psutil ничего не вернул")

    for name, info in stats.items():
        if name.lower().startswith("lo"):
            continue
        if info.isup:
            return name
    # fallback: just return the first key
    return next(iter(stats))


def load_config() -> Config:
    token = _env("BOT_TOKEN", settings.BOT_TOKEN)
    if not token:
        raise SystemExit(
            f"Заполните BOT_TOKEN в {SETTINGS_MODULE_NAME}.py или переменных окружения"
        )

    channel_id = _env("CHANNEL_ID", settings.CHANNEL_ID)
    if not channel_id:
        raise SystemExit(
            f"Заполните CHANNEL_ID в {SETTINGS_MODULE_NAME}.py или переменных окружения"
        )

    interface = _env("INTERFACE", settings.INTERFACE or None) or _pick_interface()
    interval_raw = _env("UPDATE_INTERVAL", str(settings.UPDATE_INTERVAL or "5"))
    try:
        update_interval = float(interval_raw)
        if update_interval <= 0:
            raise ValueError
    except ValueError:
        raise SystemExit("UPDATE_INTERVAL должен быть положительным числом секунд")

    message_id_raw = _env(
        "MESSAGE_ID",
        str(settings.MESSAGE_ID) if settings.MESSAGE_ID is not None else None,
    )
    message_id = int(message_id_raw) if message_id_raw else None

    return Config(
        token=token,
        channel_id=channel_id,
        interface=interface,
        update_interval=update_interval,
        message_id=message_id,
    )


def _humanize_bytes_per_sec(value: float) -> str:
    """Convert bytes/sec to human-readable bits/sec (Kb/s, Mb/s, ...)."""
    units = ["b/s", "Kb/s", "Mb/s", "Gb/s", "Tb/s"]
    value_bits = float(value) * 8
    for unit in units:
        if abs(value_bits) < 1024 or unit == units[-1]:
            return f"{value_bits:,.2f} {unit}"
        value_bits /= 1024
    return f"{value_bits:,.2f} Tb/s"


def _humanize_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(value)
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{value:,.2f} {unit}"
        value /= 1024
    return f"{value:,.2f} TB"


def _snapshot(interface: str) -> psutil._common.snetio:
    counters = psutil.net_io_counters(pernic=True)
    if interface not in counters:
        raise SystemExit(f"Интерфейс {interface} не найден. Доступные: {', '.join(counters.keys())}")
    return counters[interface]


def _render(
    interface: str,
    upload_bps: float,
    download_bps: float,
    total_sent: float,
    total_recv: float,
    cpu_percent: float,
    update_interval: float,
) -> str:
    now = datetime.now().strftime("%H:%M:%S")
    return (
        f"⏱ <b>{now}</b>\n"
        f"🧠 CPU: <code>{cpu_percent:.1f}%</code>\n"
        f"↗️ Upload: <code>{_humanize_bytes_per_sec(upload_bps)}</code>\n"
        f"↘️ Download: <code>{_humanize_bytes_per_sec(download_bps)}</code>\n"
        f"Σ Трафик: <code>{_humanize_bytes(total_sent)} ↑</code> | "
        f"<code>{_humanize_bytes(total_recv)} ↓</code>"
    )


async def updater(bot: Bot, config: Config) -> None:
    prev_snapshot = _snapshot(config.interface)
    prev_time = time.monotonic()
    psutil.cpu_percent(interval=None)
    sample_interval = min(1.0, config.update_interval)
    next_update_at = prev_time + config.update_interval
    accum_sent = 0.0
    accum_recv = 0.0
    accum_time = 0.0

    initial_text = _render(
        config.interface,
        upload_bps=0,
        download_bps=0,
        total_sent=prev_snapshot.bytes_sent,
        total_recv=prev_snapshot.bytes_recv,
        cpu_percent=psutil.cpu_percent(interval=None),
        update_interval=config.update_interval,
    )

    last_text = initial_text
    if config.message_id:
        message_id = config.message_id
        logging.info("Редактирую существующее сообщение %s в %s", message_id, config.channel_id)
        try:
            await bot.edit_message_text(
                chat_id=config.channel_id,
                message_id=message_id,
                text=initial_text,
                parse_mode="HTML",
            )
        except TelegramBadRequest as exc:
            logging.error(
                "Не получилось обновить MESSAGE_ID=%s (%s). Создаю новое сообщение.",
                config.message_id,
                exc,
            )
            sent = await bot.send_message(
                chat_id=config.channel_id,
                text=initial_text,
                parse_mode="HTML",
                disable_notification=True,
            )
            message_id = sent.message_id
    else:
        sent = await bot.send_message(
            chat_id=config.channel_id,
            text=initial_text,
            parse_mode="HTML",
            disable_notification=True,
        )
        message_id = sent.message_id
        logging.info("Создал сообщение %s в %s", message_id, config.channel_id)

    while True:
        await asyncio.sleep(sample_interval)
        now_snapshot = _snapshot(config.interface)
        now_time = time.monotonic()
        interval = max(now_time - prev_time, 1e-3)

        accum_sent += now_snapshot.bytes_sent - prev_snapshot.bytes_sent
        accum_recv += now_snapshot.bytes_recv - prev_snapshot.bytes_recv
        accum_time += interval
        prev_snapshot = now_snapshot
        prev_time = now_time

        if now_time < next_update_at:
            continue

        effective_interval = max(accum_time, 1e-3)
        upload_bps = accum_sent / effective_interval
        download_bps = accum_recv / effective_interval

        text = _render(
            config.interface,
            upload_bps=upload_bps,
            download_bps=download_bps,
            total_sent=now_snapshot.bytes_sent,
            total_recv=now_snapshot.bytes_recv,
            cpu_percent=psutil.cpu_percent(interval=None),
            update_interval=config.update_interval,
        )

        if text == last_text:
            accum_sent = 0.0
            accum_recv = 0.0
            accum_time = 0.0
            next_update_at = now_time + config.update_interval
            continue

        try:
            await bot.edit_message_text(
                chat_id=config.channel_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
            )
        except TelegramRetryAfter as exc:
            logging.warning("Получил ограничение Telegram. Жду %s секунд", exc.retry_after)
            await asyncio.sleep(exc.retry_after)
        except TelegramBadRequest as exc:
            logging.error("Не получилось обновить сообщение: %s", exc)
        else:
            last_text = text

        accum_sent = 0.0
        accum_recv = 0.0
        accum_time = 0.0
        next_update_at = now_time + config.update_interval


async def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
    )
    logging.info(
        "Запуск бота. Канал: %s | Интервал: %ss | Интерфейс: %s",
        config.channel_id,
        config.update_interval,
        config.interface,
    )
    async with Bot(token=config.token) as bot:
        await updater(bot, config)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
