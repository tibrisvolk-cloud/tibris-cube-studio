"""Short, guided order flow and a common payment chooser."""

import json
import time

from .ordering import total, summary, method_amount, discounted
from .catalog import CATEGORIES
from .store import RuleError


class OrderUI:
    def draft(self, uid, step=None):
        dialog = self.s.dialog(uid)
        if not dialog or dialog[0] != "order" or (step and dialog[1].get("step") != step):
            raise RuleError("Этот шаг уже пройден. Откройте текущий заказ или начните новый 🐾")
        return dialog[1]

    def methods(self):
        methods = []
        for row in self.s.rows("SELECT key,value FROM settings WHERE key LIKE 'method:%' ORDER BY key"):
            config = json.loads(row["value"])
            provider = config.get("provider", "manual")
            if provider == "cryptopay" and not self.cfg.crypto_pay_token:
                continue
            if provider == "yoomoney" and (not self.cfg.yoomoney_token or not self.cfg.yoomoney_wallet):
                continue
            try:
                method_amount(100, config)
            except RuleError:
                continue
            methods.append((row["key"][7:], config))
        return methods

    def amounts(self, data):
        return total(data["product"], data["options"], data["background_price"], data.get("background_stars"))

    async def begin_order(self, context, uid, slug):
        if self.s.setting("commissions_open", "1") != "1":
            raise RuleError("Сейчас я заканчиваю предыдущие заказы. Напишите мне — обсудим вашу идею 🐾")
        product = self.s.one("SELECT * FROM art_products WHERE slug=? AND active=1", (slug,))
        if (
            not product
            or product["category"] not in CATEGORIES
            or slug in {"light_logo", "light_background", "mc_4d"}
        ):
            raise RuleError("Этот формат больше не доступен. Выберите другой в каталоге.")
        slots = self.s.free_slots(slug, uid)
        if not slots:
            await self.tell(
                context,
                uid,
                "Все места на этот формат пока заняты 🐾\nМожно выбрать другой формат или написать мне.",
                [[("💬 Написать Tibris", "contact")], [("← К форматам", f"cat:{product['category']}")]],
            )
            return
        self.s.hold_slot(uid, slots[0]["id"], slug)
        data = {
            "slug": slug,
            "product": product,
            "slot_id": slots[0]["id"],
            "refs": [],
            "options": {"characters": 1, "background": False},
            "background_price": int(self.s.setting("background_rub", "400")),
            "background_stars": int(self.s.setting("background_stars", "0")) or None,
        }
        data["options"]["extra_percent"] = int(self.s.setting("extra_character_percent", "100"))
        if product["category"] == "minecraft":
            data["step"] = "brief"
            self.s.save_dialog(uid, "order", data)
            await self.ask_brief(context, uid, data)
        else:
            data["step"] = "bg"
            self.s.save_dialog(uid, "order", data)
            await self.tell(
                context,
                uid,
                f"🎨 {product['title']} · {product['rub']} ₽\n\n{product['description']}\n\nДобавим подробный фон?",
                [
                    [(f"🌄 Да · +{data['background_price']} ₽", "build_bg:1")],
                    [("Без подробного фона · +0 ₽", "build_bg:0")],
                    [("← К форматам", f"cat:{product['category']}")],
                ],
                "product:" + slug,
            )

    async def ask_brief(self, context, uid, data):
        text = summary(data["product"], data["options"], self.amounts(data))
        text += "\n\n📝 Расскажите, что хотите: персонаж, поза, настроение и пожелания. Если есть желаемый срок, тоже напишите."
        if data["slug"] == "light_chibi":
            text += "\nДля чиби можно попросить версию без фона."
        await self.tell(context, uid, text, [[("Отменить заказ", "studio")]])

    async def coupon_choice(self, context, uid):
        data = self.draft(uid)
        coupons = self.s.rows(
            "SELECT * FROM coupons WHERE user_id=? AND state='available' ORDER BY id", (uid,)
        )
        if not coupons:
            data.update(coupon_id=None, discount=0, step="payment")
            self.s.save_dialog(uid, "order", data)
            await self.draft_payment(context, uid)
            return
        data["step"] = "coupon"
        self.s.save_dialog(uid, "order", data)
        rows = [[(f"Скидка −{c['percent']}%", f"coupon:{c['id']}")] for c in coupons[:20]]
        rows += [[("Продолжить без скидки", "coupon:0")], [("Отменить", "studio")]]
        await self.tell(context, uid, "🎁 У вас есть купон. Применим его к этому заказу?", rows)

    async def draft_payment(self, context, uid):
        data = self.draft(uid, "payment")
        amounts = self.amounts(data)
        percent = data.get("discount", 0)
        text = summary(data["product"], data["options"], amounts, percent)
        rows = []
        if percent == 100:
            rows.append([("🎁 Заказать по купону · 0 ₽", "build_pay:bonus")])
        else:
            if amounts["stars"]:
                rows.append([(f"⭐ Stars · {discounted(amounts['stars'], percent)}", "build_pay:stars")])
            for key, config in self.methods():
                amount = method_amount(discounted(amounts["rub"], percent), config)
                rows.append(
                    [(f"{config.get('label', key)} · {amount} {config['currency']}", f"build_pay:{key}")]
                )
            if amounts["points"] and not percent:
                rows.append([(f"🎲 Баллы Cube · {amounts['points']}", "build_pay:points")])
            if not rows:
                rows.append([("💳 В рублях · обсудить с Tibris", "build_pay:rub")])
        rows += [[("Отменить заказ", "studio")]]
        await self.tell(
            context,
            uid,
            text
            + "\n\nКак вам удобнее оплатить?\nЯ сначала посмотрю заявку и подтвержу срок. Сейчас деньги не списываются.",
            rows,
        )

    async def payment_menu(self, context, uid, oid):
        order = self.s.order(oid, uid)
        if order["status"] != "quoted":
            raise RuleError("Сначала дождитесь согласования заказа 🐾")
        if order["points"] or order["discount"] == 100:
            await self.tell(
                context,
                uid,
                "Заказ согласован. Подтвердим оплату бонусом?",
                [[("🎁 Подтвердить", f"redeem:{oid}")], [("← Заказ", f"order:{oid}")]],
            )
            return
        invoice = self.s.auto_invoice(oid)
        if (
            order["proof"]
            or order["checkout_until"] > time.time()
            or (invoice and invoice["state"] in {"creating", "uncertain", "pending"})
        ):
            rows = [[("← Заказ", f"order:{oid}")]]
            if order["payment_method"]:
                rows.insert(0, [("💳 Продолжить оплату", f"external:{oid}")])
            await self.tell(
                context,
                uid,
                "🕒 Оплата уже началась или проверяется. Продолжите по текущему счёту — он закреплён за заказом.",
                rows,
            )
            return
        rows = []
        if order["stars"]:
            rows.append([(f"⭐ Stars · {order['stars']}", f"paypick:{oid}:stars")])
        for key, config in self.methods():
            rows.append(
                [
                    (
                        f"{config.get('label', key)} · {method_amount(order['rub'], config)} {config['currency']}",
                        f"paypick:{oid}:{key}",
                    )
                ]
            )
        # Existing offers keep their saved requisites even if the method was disabled later.
        if order["payment_method"]:
            rows.append([("💳 Продолжить выбранную оплату", f"external:{oid}")])
        if not rows:
            rows.append([("💬 Уточнить оплату у Tibris", "contact")])
        rows.append([("← Заказ", f"order:{oid}")])
        await self.tell(
            context,
            uid,
            f"💳 Заказ #{oid} · {order['rub']} ₽\nВыберите удобный способ. Скидка уже учтена.",
            rows,
        )

    async def order_callback(self, context, uid, action, parts):
        arg = parts[1] if len(parts) > 1 else None
        if action in {"build_bg", "build_second", "build_third"}:
            step = {"build_bg": "bg", "build_second": "second", "build_third": "third"}[action]
            data = self.draft(uid, step)
            if arg not in {"0", "1"}:
                raise RuleError("Выберите один из вариантов кнопками.")
            if action == "build_bg":
                data["options"]["background"] = arg == "1"
                data["step"] = "second"
                self.s.save_dialog(uid, "order", data)
                await self.tell(
                    context,
                    uid,
                    f"🐾 Нужен второй персонаж?\nСейчас: {self.amounts(data)['rub']} ₽",
                    [
                        [(f"Да · +{self.amounts(data)['extra_rub']} ₽", "build_second:1")],
                        [("Нет, один персонаж", "build_second:0")],
                        [("Отменить", "studio")],
                    ],
                )
            elif action == "build_second" and arg == "1":
                data["options"]["characters"] = 2
                data["step"] = "third"
                self.s.save_dialog(uid, "order", data)
                await self.tell(
                    context,
                    uid,
                    f"🐾 Добавим третьего персонажа?\nСейчас: {self.amounts(data)['rub']} ₽",
                    [
                        [(f"Да · +{self.amounts(data)['extra_rub']} ₽", "build_third:1")],
                        [("Нет, оставим двоих", "build_third:0")],
                        [("Отменить", "studio")],
                    ],
                )
            else:
                if action == "build_third":
                    data["options"]["characters"] = 3 if arg == "1" else 2
                data["step"] = "brief"
                self.s.save_dialog(uid, "order", data)
                await self.ask_brief(context, uid, data)
        elif action == "build_pay":
            data = self.draft(uid, "payment")
            amounts = self.amounts(data)
            allowed = {key for key, _ in self.methods()}
            if data.get("discount") == 100:
                allowed = {"bonus"}
            else:
                if amounts["stars"]:
                    allowed.add("stars")
                if amounts["points"] and not data.get("discount"):
                    allowed.add("points")
                if not allowed:
                    allowed.add("rub")
            if arg not in allowed:
                raise RuleError("Этот способ сейчас недоступен. Откройте выбор оплаты снова.")
            data.update(method=arg, step="confirm")
            self.s.save_dialog(uid, "order", data)
            await self.tell(
                context,
                uid,
                summary(data["product"], data["options"], amounts, data.get("discount", 0))
                + f"\n\nОписание: {data['brief'][:400]}\nРеференсов: {len(data['refs'])}\nОплата: {arg}\n\nВсё верно? Отправьте заявку — я подтвержу её и срок 🐾",
                [
                    [("🐾 Отправить заявку", "build_submit")],
                    [("← Другой способ оплаты", "build_payback")],
                    [("Отменить", "studio")],
                ],
            )
        elif action == "build_payback":
            data = self.draft(uid, "confirm")
            data["step"] = "payment"
            self.s.save_dialog(uid, "order", data)
            await self.draft_payment(context, uid)
        elif action == "build_submit":
            data = self.draft(uid, "confirm")
            # create_order validates the held slot and coupon in the same transaction.
            oid = self.s.create_order(
                uid,
                data["slug"],
                data["brief"],
                data["refs"],
                data.get("coupon_id"),
                data["slot_id"],
                data["options"],
                self.amounts(data),
                data["method"],
            )
            self.s.clear_dialog(uid)
            await self.notify(
                context,
                self.cfg.admin_id,
                f"🐾 Новая заявка #{oid}",
                [[("📦 Посмотреть и принять", f"order:{oid}")]],
            )
            await self.tell(
                context,
                uid,
                f"Заявка #{oid} отправлена 🐾\nЯ посмотрю вашу идею и подтвержу срок. Следить за заказом можно в профиле.",
                [[("📦 Мой заказ", f"order:{oid}")], [("🏠 Главная", "home")]],
            )
        elif action == "paymenu":
            await self.payment_menu(context, uid, int(arg))
        elif action == "paypick":
            method = parts[2]
            available = dict(self.methods())
            if method != "stars" and method not in available:
                raise RuleError("Этот способ сейчас недоступен.")
            self.s.choose_payment(uid, int(arg), method, available.get(method))
            # Continue through the existing invoice/API/manual-proof handlers.
            return "invoice" if method == "stars" else "external", arg
        else:
            return False
        return True
