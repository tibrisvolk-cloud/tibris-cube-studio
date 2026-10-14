"""Prices for the button-based commission builder. No guessed exchange rates."""

import math
from decimal import Decimal, InvalidOperation, ROUND_UP

from .store import RuleError


def total(product, options, background_price=400, background_stars=None):
    characters = int(options.get("characters", 1))
    background = bool(options.get("background", False))
    extra_percent = int(options.get("extra_percent", 100))
    if not 1 <= extra_percent <= 500:
        raise RuleError("Доплата за персонажа: от 1 до 500% базовой цены.")
    if characters not in {1, 2, 3}:
        raise RuleError("Можно выбрать от одного до трёх персонажей.")
    if product["category"] == "minecraft" and (characters != 1 or background):
        raise RuleError("Для Minecraft фон и дополнительные персонажи недоступны.")
    extra_rub = math.ceil(product["rub"] * extra_percent / 100)
    rub = product["rub"] + extra_rub * (characters - 1) + (background_price if background else 0)
    stars = product["stars"]
    if stars:
        stars = stars + math.ceil(stars * extra_percent / 100) * (characters - 1)
        if background:
            stars = stars + background_stars if background_stars else None
    points = product["points"] if characters == 1 and not background else None
    return {
        "rub": rub,
        "stars": stars,
        "points": points,
        "extra_rub": extra_rub,
        "background_rub": background_price if background else 0,
    }


def discounted(amount, percent):
    return math.ceil(amount * (100 - percent) / 100) if amount is not None else None


def method_amount(rub, config):
    if config["currency"] == "RUB":
        return str(rub)
    try:
        rate = Decimal(str(config.get("rate", "0")).replace(",", "."))
    except InvalidOperation:
        raise RuleError("Tibris ещё не настроил курс этого способа оплаты.") from None
    if not rate.is_finite() or rate <= 0:
        raise RuleError("Tibris ещё не настроил курс этого способа оплаты.")
    quantum = Decimal("0.01") if config["currency"] in {"USD", "EUR"} else Decimal("0.000001")
    return str((Decimal(rub) / rate).quantize(quantum, rounding=ROUND_UP))


def summary(product, options, amounts, percent=0):
    lines = [f"🐾 Ваш заказ · {product['title']}", f"Формат: {product['rub']} ₽"]
    characters = int(options.get("characters", 1))
    if characters > 1:
        lines.append(f"Ещё персонажей: {characters - 1} · +{amounts['extra_rub'] * (characters - 1)} ₽")
    if product["category"] != "minecraft":
        lines.append(
            f"Подробный фон: +{amounts['background_rub']} ₽"
            if options.get("background")
            else "Без подробного фона · +0 ₽"
        )
    if percent:
        lines.append(f"Купон: −{percent}%")
    lines.append(f"\nИтого: {discounted(amounts['rub'], percent)} ₽")
    if amounts["stars"]:
        lines.append(f"или {discounted(amounts['stars'], percent)} ⭐")
    return "\n".join(lines)
