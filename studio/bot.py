import asyncio
import html
import json
import logging
import shlex
import time
from pathlib import Path

import httpx

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup, LabeledPrice, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from .catalog import CATEGORIES, STATUS
from .autopay import AutoPay
from .config import Config
from .store import RuleError, Store, positive

log = logging.getLogger(__name__)
ASSETS = Path(__file__).resolve().parents[1] / "assets"
PAGE_SIZE = 8


def esc(value):
    return html.escape(str(value or ""))


def kb(rows):
    return Markup([[Button(label, callback_data=data) for label, data in row] for row in rows])


HOME = [("🏠 Главная", "home")]


class Bot:
    def __init__(self, config, store=None):
        self.cfg = config
        self.s = store or Store(config.db_path)
        self.auto = AutoPay(self.s, config)
        self.poll_task = None

    def admin(self, uid):
        if uid != self.cfg.admin_id:
            raise RuleError("Этот раздел доступен только Tibris.")

    def media_of(self, msg):
        if msg.photo:
            return {"kind": "photo", "file_id": msg.photo[-1].file_id}
        if msg.document:
            if msg.document.file_size and msg.document.file_size > 20 * 1024 * 1024:
                raise RuleError("Отправьте файл размером до 20 МБ.")
            return {
                "kind": "document",
                "file_id": msg.document.file_id,
                "name": msg.document.file_name or "",
                "mime": msg.document.mime_type or "",
            }
        return None

    async def send_file(self, context, uid, media, caption=None):
        if media["kind"] == "photo":
            return await context.bot.send_photo(uid, media["file_id"], caption=caption)
        return await context.bot.send_document(uid, media["file_id"], caption=caption)

    async def tell(self, context, uid, text, rows=None, image=None, media=None):
        markup = kb(rows) if rows else None
        selected = media or (self.s.one("SELECT * FROM media WHERE key=?", (image,)) if image else None)
        # Keep long descriptions out of Telegram's 1024-character photo caption limit.
        if selected:
            try:
                await self.send_file(context, uid, selected)
            except TelegramError:
                log.warning("Could not display configured media for %s", image)
        elif image:
            path = ASSETS / f"{image}.png"
            if not path.exists() and image in {"cube", "riddles", "shop", "about", "studio"}:
                path = ASSETS / "main.png"
            if path.exists():
                with path.open("rb") as photo:
                    await context.bot.send_photo(uid, photo)
        for i in range(0, len(text), 3900):
            # Text pages use plain text for safe splitting and literal user/admin content.
            await context.bot.send_message(
                uid, text[i : i + 3900], reply_markup=markup if i + 3900 >= len(text) else None
            )

    async def notify(self, context, uid, text):
        try:
            await self.tell(context, uid, text)
            return True
        except TelegramError:
            log.warning("Notification failed for user %s", uid)
            return False

    async def home(self, context, uid):
        name = self.s.setting("artist_name", self.cfg.artist_name)
        text = self.s.setting(
            "text:home",
            f"🐾 {name} × Digital Cube\n\n"
            "Привет! Здесь живут мои персонажи, ваши идеи и немного кубической магии.\n\n"
            "🎨 Студия — обсудим арт, стикер или новый образ вашего OC.\n"
            "🎮 Digital Cube — загадки, задания и баллы для магазина и творческих бонусов.",
        )
        rows = [
            [("🎨 Студия Tibris", "studio"), ("🎮 Digital Cube", "cube")],
            [("👤 Мой профиль", "profile"), ("🐾 Обо мне", "about")],
            [("💬 Написать Tibris", "contact")],
        ]
        if uid == self.cfg.admin_id:
            rows.append([("⚙️ Управление", "admin")])
        await self.tell(context, uid, text, rows, "main")

    async def start(self, update, context):
        u = update.effective_user
        self.s.user(u.id, u.username)
        self.s.clear_dialog(u.id)
        await self.home(context, u.id)

    async def cancel(self, update, context):
        self.s.clear_dialog(update.effective_user.id)
        await update.effective_message.reply_text("Диалог отменён. /start — в главное меню.")

    async def contact(self, context, uid):
        self.s.save_dialog(uid, "contact", {})
        username = self.s.setting("artist_username", self.cfg.artist_username)
        suffix = f"\nМожно написать напрямую: @{username}" if username else ""
        await self.tell(
            context,
            uid,
            "💬 Напишите сообщение или приложите файл — я передам его Tibris.\n"
            "Для ответа по заказу укажите его номер. /cancel — отмена." + suffix,
            [[("🏠 Главная", "home")]],
        )

    async def list_page(self, context, uid, title, entries, page, prefix, back="cube"):
        page = max(0, int(page))
        rows = [
            [(label[:60], callback)] for label, callback in entries[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        ]
        navigation = []
        if page:
            navigation.append(("←", f"{prefix}:{page - 1}"))
        if len(entries) > (page + 1) * PAGE_SIZE:
            navigation.append(("→", f"{prefix}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([("🔙 Назад", back)])
        await self.tell(context, uid, title if entries else title + "\nПока здесь пусто.", rows)

    async def order_card(self, context, uid, oid):
        o = self.s.order(oid, None if uid == self.cfg.admin_id else uid)
        text = f"Заказ #{oid} · {o['title']}\n{STATUS[o['status']]}\nСлот: {o['slot_label'] or 'назначается'}\n\n{o['brief']}"
        rows = []
        if o["status"] == "quoted":
            text += f"\n\nСогласованная цена: {o['rub']} ₽"
            if o["stars"]:
                text += f" / {o['stars']} ⭐"
            if o["points"]:
                text += f"\nАрт за баллы: {o['points']} pts"
            if o["discount"]:
                text += f"\nСкидка {o['discount']}% уже учтена."
            text += f"\nУсловия Tibris: {o['note']}"
            if o["payment_method"]:
                text += f"\nВнешний расчёт: {o['external_amount']} {o['external_currency']} · {o['payment_method']}"
                if o["proof"]:
                    text += "\n🕒 Подтверждение отправлено. Tibris проверяет поступление."
            if uid == o["user_id"]:
                if o["payment_method"]:
                    rows.append([("💳 Реквизиты и подтверждение", f"external:{oid}")])
                elif o["points"] or o["discount"] == 100:
                    rows.append([("✅ Подтвердить заказ за баллы / бонус", f"redeem:{oid}")])
                elif o["stars"]:
                    rows.append([("⭐ Оплатить Stars", f"invoice:{oid}")])
        if o["status"] in {"requested", "quoted"}:
            rows.append([("Отменить заявку", f"cancelorder:{oid}")])
        if o["status"] == "delivered" and o["result"]:
            rows.append([("📥 Получить файл ещё раз", f"result:{oid}")])
        if uid == self.cfg.admin_id:
            rows.extend(
                [
                    [("📎 Референсы", f"refs:{oid}")],
                    [("📝 Предложить цену", f"quoteui:{oid}")],
                    [("🎨 В работу", f"working:{oid}"), ("📦 Выдать", f"deliverui:{oid}")],
                ]
            )
            text += f"\n\nКлиент: {o['user_id']}\nSKU: {o['slug']}"
            if o["payment_method"] and o["status"] == "quoted":
                text += f"\nДля подтверждения: /markpaid {oid} номер_операции"
                if o["proof"]:
                    rows.append([("🧾 Подтверждение клиента", f"proof:{oid}")])
        rows.extend(
            [
                [("💬 Обсудить", "contact")],
                [("🔙 Заказы", "adminorders:0" if uid == self.cfg.admin_id else "orders:0")],
                HOME,
            ]
        )
        await self.tell(context, uid, text, rows)

    async def coupon_choice(self, context, uid):
        d = self.s.dialog(uid)
        if not d or d[0] != "order":
            raise RuleError("Заявка истекла. Начните заново.")
        data = d[1]
        data["step"] = "coupon"
        self.s.save_dialog(uid, "order", data)
        coupons = self.s.rows(
            "SELECT * FROM coupons WHERE user_id=? AND state='available' ORDER BY id", (uid,)
        )
        rows = [[(f"Купон #{c['id']} · −{c['percent']}%", f"coupon:{c['id']}")] for c in coupons[:20]]
        rows.extend([[("Без купона", "coupon:0")], [("Отменить", "home")]])
        await self.tell(
            context,
            uid,
            "Применим бонус Digital Cube? Купон закрепится за этой заявкой; "
            "при отмене до оплаты он вернётся. Арт целиком за pts оформляется без купона.",
            rows,
        )

    async def callback(self, update, context):
        q = update.callback_query
        await q.answer()
        uid = q.from_user.id
        self.s.user(uid, q.from_user.username)
        parts = q.data.split(":")
        action = parts[0]
        arg = parts[1] if len(parts) > 1 else None
        if action in {
            "home",
            "studio",
            "cube",
            "about",
            "profile",
            "cat",
            "riddles",
            "tasks",
            "shop",
            "orders",
            "bonuses",
            "admin",
            "adminorders",
        }:
            self.s.clear_dialog(uid)
        if action == "home":
            await self.home(context, uid)
        elif action == "studio":
            rows = [[(name, f"cat:{key}")] for key, (name, _) in CATEGORIES.items()]
            rows += [
                [("📦 Мои заказы", "orders:0"), ("🎁 Бонусы Cube", "bonuses:0")],
                [("🎟 Промокод", "promo"), ("📜 Как заказать", "terms")],
                HOME,
            ]
            await self.tell(
                context,
                uid,
                "🎨 Студия Tibris\n\nРасскажите о вашем OC — вместе найдём формат, "
                "в котором он заиграет. Сначала обсуждаем идею, цену и срок; потом оплата и работа.",
                rows,
                "studio",
            )
        elif action == "cat":
            if arg not in CATEGORIES:
                raise RuleError("Раздел не найден.")
            name, description = CATEGORIES[arg]
            products = self.s.rows("SELECT * FROM art_products WHERE category=? AND active=1", (arg,))
            text = self.s.setting("text:" + arg, name + "\n\n" + description) + "\n\n"
            rows = []
            for p in products:
                price = ("от " if p["price_from"] else "") + f"{p['rub']} ₽"
                if p["stars"]:
                    price += f" / {p['stars']} ⭐"
                if p["points"]:
                    price += f" / {p['points']} pts"
                text += f"• {p['title']} — {price}\n"
                text += f"  Свободных мест: {len(self.s.free_slots(p['slug'], uid))}\n"
                rows.append([(p["title"], f"product:{p['slug']}")])
            text += "\nИтоговую стоимость и условия я подтвержу после просмотра вашей идеи."
            rows += [[("🖼 Примеры работ", f"portfolio:{arg}")], [("🔙 Студия", "studio")]]
            await self.tell(context, uid, text, rows, arg)
        elif action == "product":
            p = self.s.one("SELECT * FROM art_products WHERE slug=? AND active=1", (arg,))
            if not p:
                raise RuleError("Формат не найден.")
            text = f"{p['title']}\n\n{p['description']}\n\nБазовая цена: {'от ' if p['price_from'] else ''}{p['rub']} ₽"
            if p["stars"]:
                text += f" / {p['stars']} ⭐"
            if p["points"]:
                text += f"\nМожно заказать за {p['points']} pts после согласования."
            text += (
                "\n\nПодготовьте описание и референсы. Стоимость сложного фона, дополнительных персонажей "
            )
            text += "и срок согласуем до оплаты. Способ оплаты и реквизиты обсудим после заявки."
            text += f"\nСвободных слотов: {len(self.s.free_slots(arg, uid))}."
            await self.tell(
                context,
                uid,
                text,
                [[("🐾 Выбрать слот", f"neworder:{arg}")], [("🔙 К форматам", f"cat:{p['category']}")]],
                "product:" + arg,
            )
        elif action == "neworder":
            if self.s.setting("commissions_open", "1") != "1":
                raise RuleError("Приём новых заявок временно закрыт. Можно написать Tibris.")
            p = self.s.one("SELECT * FROM art_products WHERE slug=? AND active=1", (arg,))
            if not p:
                raise RuleError("Формат недоступен.")
            await self.slot_choice(context, uid, arg)
        elif action == "chooseslot":
            slug = parts[2]
            if self.s.setting("commissions_open", "1") != "1":
                raise RuleError("Приём новых заявок закрыт.")
            selected = self.s.hold_slot(uid, int(arg), slug)
            previous = self.s.dialog(uid)
            if (
                previous
                and previous[0] == "order"
                and previous[1].get("slug") == slug
                and previous[1].get("brief")
            ):
                data = previous[1]
                data["slot_id"] = int(arg)
                self.s.save_dialog(uid, "order", data)
                await self.coupon_choice(context, uid)
                return
            self.s.save_dialog(uid, "order", {"slug": slug, "slot_id": int(arg), "step": "brief", "refs": []})
            await self.tell(
                context,
                uid,
                f"Место «{selected['title']}» зарезервировано на 30 минут; "
                "каждый шаг заявки продлевает резерв.",
            )
            await self.tell(
                context,
                uid,
                "📝 Расскажите об идее: персонаж, поза или эмоция, фон, формат, "
                "срок и пожелания. До 3500 символов. Затем прикрепим референсы. /cancel — отмена.",
            )
        elif action == "refs_done":
            d = self.s.dialog(uid)
            if not d or d[0] != "order" or d[1].get("step") != "refs":
                raise RuleError("Заявка истекла. Начните заново.")
            await self.coupon_choice(context, uid)
        elif action == "coupon":
            d = self.s.dialog(uid)
            if not d or d[0] != "order" or d[1].get("step") != "coupon":
                raise RuleError("Заявка уже отправлена или истекла.")
            data = d[1]
            try:
                oid = self.s.create_order(
                    uid, data["slug"], data["brief"], data["refs"], int(arg) or None, data["slot_id"]
                )
            except RuleError as e:
                if "слот" in str(e).lower() or "мест" in str(e).lower():
                    await self.slot_choice(context, uid, data["slug"])
                raise
            self.s.clear_dialog(uid)
            await self.notify(
                context,
                self.cfg.admin_id,
                f"🐾 Новая заявка #{oid} от @{q.from_user.username or uid}.\n"
                f"{data['brief']}\n/admin — открыть очередь и референсы.",
            )
            await self.tell(
                context,
                uid,
                f"Заявка #{oid} у Tibris 🐾\nЯ посмотрю референсы и пришлю цену и условия.",
                [[("📦 Открыть заявку", f"order:{oid}")], HOME],
            )
        elif action in {"orders", "adminorders"}:
            if action == "adminorders":
                self.admin(uid)
                orders = self.s.rows(
                    "SELECT o.*,p.title FROM orders o JOIN art_products p ON o.slug=p.slug ORDER BY o.id DESC"
                )
            else:
                orders = self.s.rows(
                    "SELECT o.*,p.title FROM orders o JOIN art_products p ON o.slug=p.slug WHERE user_id=? ORDER BY o.id DESC",
                    (uid,),
                )
            await self.list_page(
                context,
                uid,
                "📦 Заказы",
                [(f"#{o['id']} {o['title']} · {STATUS[o['status']]}", f"order:{o['id']}") for o in orders],
                arg,
                action,
                "home",
            )
        elif action == "order":
            await self.order_card(context, uid, int(arg))
        elif action == "cancelorder":
            self.s.order(int(arg), None if uid == self.cfg.admin_id else uid)
            await self.tell(
                context,
                uid,
                f"Отменить заявку #{arg}? Купон вернётся, если оплата ещё не началась.",
                [[("Да, отменить", f"cancelconfirm:{arg}"), ("Оставить", f"order:{arg}")]],
            )
        elif action == "cancelconfirm":
            o = self.s.order(int(arg), None if uid == self.cfg.admin_id else uid)
            self.s.cancel_order(uid, int(arg), uid == self.cfg.admin_id)
            await self.notify(
                context,
                o["user_id"] if uid == self.cfg.admin_id else self.cfg.admin_id,
                f"Заявка #{arg} отменена.",
            )
            await self.order_card(context, uid, int(arg))
        elif action == "invoice":
            o = self.s.order(int(arg), uid)
            if o["status"] != "quoted" or not o["stars"] or o["points"]:
                raise RuleError("Счёт недоступен.")
            await context.bot.send_invoice(
                uid,
                f"Tibris · заказ #{arg}",
                f"{o['title']} · условия согласованы в заявке #{arg}",
                o["invoice"],
                "XTR",
                [LabeledPrice("Заказ", o["stars"])],
                provider_token="",
                start_parameter=f"order-{arg}",
            )
        elif action == "external":
            o = self.s.order(int(arg), uid)
            if o["status"] != "quoted" or not o["payment_method"]:
                raise RuleError("Внешняя оплата недоступна.")
            if o["gateway"] != "manual":
                invoice = await self.auto.prepare(int(arg), f"https://t.me/{context.bot.username}")
                text = f"Заказ #{arg}: {o['external_amount']} {o['external_currency']}\n"
                if invoice["gross"]:
                    text += f"К оплате по карте с комиссией ЮMoney: {invoice['gross']} RUB.\n"
                text += "Оплата подтвердится автоматически после проверки статуса у провайдера.\n" + o["note"]
                markup = Markup(
                    [
                        [Button("💳 Открыть платёжную ссылку", url=invoice["url"])],
                        [Button("✅ Проверить сейчас", callback_data=f"autocheck:{arg}")],
                        [Button("🔙 Заказ", callback_data=f"order:{arg}")],
                    ]
                )
                await context.bot.send_message(uid, text, reply_markup=markup)
                return
            await self.tell(
                context,
                uid,
                f"💳 Заказ #{arg}\nСпособ: {o['payment_method']}\n"
                f"Сумма с учётом скидки: {o['external_amount']} {o['external_currency']}\n\n"
                f"{o['external_instructions']}\n\n{o['note']}\n\n"
                "После оплаты пришлите подтверждение. Заказ будет оплачен после проверки поступления Tibris.",
                [[("🧾 Отправить подтверждение", f"proofupload:{arg}")], [("🔙 Заказ", f"order:{arg}")]],
                "payment:" + o["payment_method"],
            )
        elif action == "proofupload":
            o = self.s.order(int(arg), uid)
            if o["status"] != "quoted" or not o["payment_method"] or o["gateway"] != "manual":
                raise RuleError("Заказ недоступен.")
            self.s.save_dialog(uid, "proof", {"oid": int(arg)})
            await self.tell(
                context,
                uid,
                "Пришлите номер операции, чек фото/файлом или хеш транзакции. "
                "Скройте лишние персональные данные. Tibris проверит фактическое поступление.",
            )
        elif action == "autocheck":
            o = self.s.order(int(arg), None if uid == self.cfg.admin_id else uid)
            fresh = await self.auto.check(int(arg))
            if fresh == "review":
                await self.review_notice(context, int(arg))
            elif fresh is True:
                await self.payment_notice(context, int(arg))
            elif self.s.order(int(arg))["status"] == "quoted":
                await self.tell(
                    context, uid, "Поступление ещё не подтверждено. Проверка повторяется автоматически."
                )
            await self.order_card(context, uid, int(arg))
        elif action == "proof":
            self.admin(uid)
            o = self.s.order(int(arg))
            if not o["proof"]:
                raise RuleError("Подтверждение ещё не прислано.")
            proof = json.loads(o["proof"])
            if proof.get("file_id"):
                await self.send_file(context, uid, proof, f"Подтверждение клиента по заказу #{arg}")
            await self.tell(
                context,
                uid,
                proof.get("text")
                or "Проверьте поступление в банке / кабинете сервиса / кошельке. "
                f"После проверки: /markpaid {arg} номер_операции",
            )
        elif action == "redeem":
            self.s.redeem_art(uid, int(arg))
            await self.notify(context, self.cfg.admin_id, f"Заказ #{arg} подтверждён за баллы / купон.")
            await self.order_card(context, uid, int(arg))
        elif action == "refs":
            self.admin(uid)
            o = self.s.order(int(arg))
            for media in json.loads(o["refs"]):
                await self.send_file(context, uid, media)
            if not json.loads(o["refs"]):
                await self.tell(context, uid, "Референсов пока нет.")
        elif action == "result":
            o = self.s.order(int(arg), None if uid == self.cfg.admin_id else uid)
            if o["status"] != "delivered" or not o["result"]:
                raise RuleError("Результат ещё не выдан.")
            await self.send_file(context, uid, json.loads(o["result"]), f"Ваш заказ #{arg} 🐾")
        elif action in {"quoteui", "deliverui", "working"}:
            self.admin(uid)
            o = self.s.order(int(arg))
            if action == "working":
                self.s.working(int(arg))
                await self.notify(context, o["user_id"], f"🎨 Tibris начал работу над заказом #{arg}.")
                await self.order_card(context, uid, int(arg))
            elif action == "quoteui":
                self.s.save_dialog(uid, "quote", {"oid": int(arg)})
                await self.tell(
                    context,
                    uid,
                    "Введите: рубли | Stars | условия и срок\n"
                    "Скидка купона применится автоматически. Для заказа за pts: /quotepts ID условия.",
                )
            else:
                if o["status"] not in {"paid", "working"}:
                    raise RuleError("Сначала дождитесь оплаты.")
                self.s.save_dialog(uid, "deliver", {"oid": int(arg)})
                await self.tell(
                    context,
                    uid,
                    "Отправьте готовый арт файлом или изображением. "
                    "Файл сохранит исходное качество. /cancel — отмена.",
                )
        elif action == "cube":
            await self.tell(
                context,
                uid,
                "🎮 Digital Cube\n\nРешайте, исследуйте и собирайте баллы. "
                "Потратить их можно в магазине или обменять на творческие бонусы Tibris.",
                [
                    [("💎 Баланс", "balance"), ("🧩 Загадки", "riddles:0")],
                    [("🔍 Задания", "tasks:0"), ("🏪 Магазин", "shop:0")],
                    [("🎁 Бонусы для артов", "bonuses:0"), ("🎒 Склад", "inventory:0")],
                    HOME,
                ],
                "cube",
            )
        elif action == "balance":
            await self.tell(
                context,
                uid,
                f"💎 Ваш баланс: {self.s.balance(uid)} pts\nБаллы — бонусы Digital Cube. "
                "Они не являются деньгами и не выводятся.",
                [[("🎁 Потратить на бонусы", "bonuses:0")], [("🔙 Cube", "cube")]],
            )
        elif action == "profile":
            orders = self.s.rows(
                "SELECT status,COUNT(*) n FROM orders WHERE user_id=? GROUP BY status", (uid,)
            )
            coupons = self.s.one(
                "SELECT COUNT(*) n FROM coupons WHERE user_id=? AND state='available'", (uid,)
            )["n"]
            purchases = self.s.one("SELECT COUNT(*) n FROM purchases WHERE user_id=?", (uid,))["n"]
            username = self.s.setting("artist_username", self.cfg.artist_username)
            rows = [
                [("📦 Все заказы", "orders:0"), ("🎒 Покупки Cube", "inventory:0")],
                [("🎁 Бонусы", "bonuses:0"), ("🎟 Промокод", "promo")],
                [("💬 Написать Tibris", "contact")],
                HOME,
            ]
            markup = kb(rows)
            if username:
                markup = Markup(
                    [[Button("🐾 Чат с Tibris", url=f"https://t.me/{username}")]]
                    + list(markup.inline_keyboard)
                )
            await context.bot.send_message(
                uid,
                f"👤 Ваш профиль\n@{q.from_user.username or 'без username'}\nID: {uid}\n"
                f"💎 Баланс Cube: {self.s.balance(uid)} pts\n🎟 Доступных купонов: {coupons}\n"
                f"🎒 Покупок Cube: {purchases}\n\n"
                + ("\n".join(f"{STATUS[o['status']]}: {o['n']}" for o in orders) or "Заявок пока нет."),
                reply_markup=markup,
            )
        elif action == "promo":
            self.s.save_dialog(uid, "promo", {})
            await self.tell(
                context, uid, "Введите текстовый промокод. Регистр букв не важен. /cancel — отмена."
            )
        elif action == "riddles":
            await self.list_page(
                context,
                uid,
                "🧩 Загадки · 3 попытки за 24 часа",
                [(r["text"][:45], f"riddle:{r['id']}") for r in self.s.riddles(uid)],
                arg,
                "riddles",
            )
        elif action == "riddle":
            r = self.s.one("SELECT * FROM riddles WHERE id=? AND is_active=1", (int(arg),))
            if not r:
                raise RuleError("Загадка недоступна.")
            self.s.save_dialog(uid, "answer", {"rid": int(arg)})
            await self.tell(
                context,
                uid,
                f"🧩 {r['text']}\n\nНаграда: {r['points_reward']} pts. Введите ответ.",
                [[("🔙 Загадки", "riddles:0")]],
                media={"file_id": r["image"], "kind": r["image_kind"]} if r["image"] else None,
            )
        elif action == "tasks":
            await self.list_page(
                context,
                uid,
                "🔍 Задания",
                [
                    (
                        f"{s['description'] or s['channel_username']} · {s['points_reward']} pts",
                        f"task:{s['id']}",
                    )
                    for s in self.s.tasks(uid)
                ],
                arg,
                "tasks",
            )
        elif action == "task":
            t = self.s.one("SELECT * FROM subscriptions WHERE id=? AND is_active=1", (int(arg),))
            if not t:
                raise RuleError("Задание недоступно.")
            text = f"🔍 {t['description'] or 'Задание'}\nНаграда: {t['points_reward']} pts"
            url = (
                t["channel_username"]
                if t["task_type"] != "subscription"
                else f"https://t.me/{t['channel_username'].lstrip('@')}"
            )
            rows = []
            if t["task_type"] == "external":
                text += f"\nТаймер: {t['delay_seconds']} сек. Нажмите «Начать», затем перейдите по ссылке. "
                text += "Бот проверяет время ожидания, а не факт посещения сайта."
                rows.append([("⏱ Начать", f"taskstart:{arg}"), ("✅ Проверить", f"taskcheck:{arg}")])
            elif t["task_type"] == "code":
                rows.append([("🔑 Ввести код", f"code:{arg}")])
            else:
                rows.append([("✅ Проверить подписку", f"taskcheck:{arg}")])
            markup = kb(rows + [[("🔙 Задания", "tasks:0")]])
            if url.startswith(("https://", "http://")):
                markup = Markup([[Button("🔗 Перейти", url=url)]] + list(markup.inline_keyboard))
            await context.bot.send_message(uid, text, reply_markup=markup)
        elif action == "taskstart":
            t = self.s.start_task(uid, int(arg))
            await self.tell(
                context,
                uid,
                f"Таймер запущен и сохранён. Через {t['delay_seconds']} сек. нажмите «Проверить».",
                [[("✅ Проверить", f"taskcheck:{arg}"), ("🔙 Задание", f"task:{arg}")]],
            )
        elif action == "code":
            t = self.s.one(
                "SELECT id FROM subscriptions WHERE id=? AND task_type='code' AND is_active=1", (int(arg),)
            )
            if not t:
                raise RuleError("Задание недоступно.")
            self.s.save_dialog(uid, "code", {"tid": int(arg)})
            await self.tell(context, uid, "Введите секретный код. /cancel — отмена.")
        elif action == "taskcheck":
            t = self.s.one("SELECT * FROM subscriptions WHERE id=? AND is_active=1", (int(arg),))
            if not t or t["task_type"] == "code":
                raise RuleError("Для этого задания нужно ввести код.")
            evidence = None
            if t["task_type"] == "subscription":
                try:
                    member = await context.bot.get_chat_member(t["channel_username"], uid)
                    evidence = member.status in {"member", "administrator", "creator"} or (
                        member.status == "restricted" and member.is_member
                    )
                except TelegramError:
                    raise RuleError(
                        "Не удалось проверить подписку. Бот должен быть администратором канала."
                    ) from None
            reward = self.s.complete_task(uid, int(arg), t["task_type"], evidence)
            await self.tell(
                context, uid, f"✅ Задание выполнено! +{reward} pts", [[("🔍 Ещё задания", "tasks:0")]]
            )
        elif action == "shop":
            items = self.s.rows("SELECT * FROM shop_items WHERE is_active=1 ORDER BY id")
            await self.list_page(
                context,
                uid,
                "🏪 Магазин Digital Cube",
                [(f"{i['name']} · {i['price']} pts", f"item:{i['id']}") for i in items],
                arg,
                "shop",
            )
        elif action == "item":
            i = self.s.one("SELECT * FROM shop_items WHERE id=? AND is_active=1", (int(arg),))
            if not i:
                raise RuleError("Товар недоступен.")
            await self.tell(
                context,
                uid,
                f"🎁 {i['name']}\n{i['description']}\n\nЦена: {i['price']} pts\n"
                f"Общий лимит: {i['total_limit'] if i['total_limit'] is not None else 'без лимита'}\n"
                f"На пользователя: {i['user_limit'] if i['user_limit'] is not None else 'без лимита'}\n"
                f"Выдача: {'вручную' if i['manual_delivery'] else 'автоматически'}",
                [[("Купить за баллы", f"buyconfirm:{arg}")], [("🔙 Магазин", "shop:0")]],
                media={"file_id": i["image"], "kind": i["image_kind"]} if i["image"] else None,
            )
        elif action == "buyconfirm":
            i = self.s.one("SELECT name,price FROM shop_items WHERE id=? AND is_active=1", (int(arg),))
            if not i:
                raise RuleError("Товар недоступен.")
            await self.tell(
                context,
                uid,
                f"Купить «{i['name']}» за {i['price']} pts?",
                [[("Да, купить", f"buy:{arg}"), ("Назад", f"item:{arg}")]],
            )
        elif action == "buy":
            pid, item = self.s.buy_item(uid, int(arg))
            content = "Tibris выдаст товар вручную." if item["manual_delivery"] else item["content"]
            await self.tell(
                context,
                uid,
                f"✅ Покупка #{pid}\n{content}\nБаланс: {self.s.balance(uid)} pts",
                [[("🎒 Склад", "inventory:0")]],
            )
            await self.notify(
                context,
                self.cfg.admin_id,
                f"Покупка Cube #{pid}: {item['name']}, клиент {uid}. /pending — ручная выдача.",
            )
        elif action == "inventory":
            purchases = self.s.rows(
                "SELECT p.*,i.name FROM purchases p JOIN shop_items i ON p.item_id=i.id WHERE user_id=? ORDER BY p.id DESC",
                (uid,),
            )
            await self.list_page(
                context,
                uid,
                "🎒 Склад",
                [
                    (
                        f"#{p['id']} {p['name']} · {'выдан' if p['delivered'] else 'ожидает'}",
                        f"purchase:{p['id']}",
                    )
                    for p in purchases
                ],
                arg,
                "inventory",
            )
        elif action == "purchase":
            p = self.s.one(
                "SELECT p.*,i.name FROM purchases p JOIN shop_items i ON i.id=p.item_id WHERE p.id=? AND user_id=?",
                (int(arg), uid),
            )
            if not p:
                raise RuleError("Покупка не найдена.")
            await self.tell(
                context,
                uid,
                f"{p['name']}\n{p['content'] if p['delivered'] else 'Ожидает ручной выдачи.'}",
                [[("🔙 Склад", "inventory:0")]],
            )
        elif action == "bonuses":
            rewards = self.s.rows("SELECT * FROM rewards WHERE active=1 ORDER BY id")
            coupons = self.s.rows("SELECT * FROM coupons WHERE user_id=? AND state='available'", (uid,))
            text = f"🎁 Бонусы Tibris\nБаланс: {self.s.balance(uid)} pts. Купонов доступно: {len(coupons)}.\n"
            text += "Скидки задаёт Tibris. Купон действует на один согласованный заказ и не суммируется.\n\n"
            text += "Арты целиком за pts появятся в каталоге, когда Tibris включит их."
            await self.list_page(
                context,
                uid,
                text,
                [
                    (f"−{r['percent']}% · {r['name']} · {r['price']} pts", f"rewardconfirm:{r['id']}")
                    for r in rewards
                ],
                arg,
                "bonuses",
                "studio",
            )
        elif action == "rewardconfirm":
            r = self.s.one("SELECT * FROM rewards WHERE id=? AND active=1", (int(arg),))
            if not r:
                raise RuleError("Бонус недоступен.")
            await self.tell(
                context,
                uid,
                f"Получить «{r['name']}» (−{r['percent']}%) за {r['price']} pts?",
                [[("Обменять", f"rewardbuy:{arg}"), ("Назад", "bonuses:0")]],
            )
        elif action == "rewardbuy":
            cid = self.s.buy_reward(uid, int(arg))
            await self.tell(
                context,
                uid,
                f"Купон #{cid} готов! Выберите его при оформлении заявки.",
                [[("🎨 Студия", "studio")]],
            )
        elif action in {"contact", "support"}:
            await self.contact(context, uid)
        elif action == "about":
            name = self.s.setting("artist_name", self.cfg.artist_name)
            text = self.s.setting(
                "text:about",
                f"🐾 Привет, я {name}!\n\nРисую персонажей, эмоции и маленькие истории, "
                "а ещё помогаю вашим OC поселиться в Minecraft.\nDigital Cube — игровой уголок этой студии: "
                "здесь активность превращается в творческие бонусы.\n\nРасскажите, что хотите придумать вместе.",
            )
            await self.tell(
                context, uid, text, [[("💬 Написать", "contact"), ("🎨 Каталог", "studio")], HOME], "about"
            )
        elif action == "terms":
            await self.tell(
                context,
                uid,
                self.s.setting(
                    "text:terms",
                    "📜 Как заказать\n\n1. Выберите формат и отправьте "
                    "идею с референсами.\n2. Tibris согласует цену, срок, этапы и правки.\n3. После согласования "
                    "оплатите согласованным способом или подтвердите разрешённый арт за pts.\n4. Готовый файл придёт в этот бот.\n\n"
                    "Не оплачивайте заказ до согласования. Дополнительный персонаж Classic: +100% базовой цены. "
                    "Сложный фон — по договорённости. Вопросы и возвраты: /paysupport. "
                    "До оплаты уточните права использования и число правок.",
                ),
                [[("🎨 Каталог", "studio")], HOME],
            )
        elif action == "portfolio":
            if arg not in CATEGORIES:
                raise RuleError("Раздел не найден.")
            media = self.s.rows(
                "SELECT * FROM media WHERE key LIKE ? ORDER BY key LIMIT 10", (f"portfolio:{arg}:%",)
            )
            for m in media:
                await self.send_file(context, uid, m)
            url = self.s.setting("portfolio_url", self.cfg.portfolio_url)
            await self.tell(
                context,
                uid,
                ("🖼 Работы Tibris" if media else "Tibris скоро добавит сюда свои работы.")
                + (f"\nПортфолио: {url}" if url else ""),
                [[("🔙 Каталог", f"cat:{arg}")]],
            )
        elif action == "admin":
            self.admin(uid)
            await self.tell(
                context,
                uid,
                "⚙️ Студия под вашим управлением\n/adminhelp — команды и форматы.\n"
                "Изображения можно присылать фото или файлами. Цены Stars и бонусы задаёте вы.",
                [
                    [("📦 Очередь заказов", "adminorders:0")],
                    [("🗓 Слоты позиций", "adminslots:0"), ("🖼 Добавить YCH", "addych")],
                    [("🧩 Новая загадка", "addriddle"), ("🎁 Новый товар", "additem")],
                    [("🖼 Обложки и фото", "imagehelp"), ("📊 Статистика", "stats")],
                    [("📣 Рассылка", "broadcast")],
                    HOME,
                ],
            )
        elif action == "adminslots":
            self.admin(uid)
            products = self.s.rows("SELECT slug,title,category FROM art_products ORDER BY category,slug")
            await self.list_page(
                context,
                uid,
                "🗓 Выберите позицию для настройки слотов",
                [(f"{p['category']} · {p['title']}", f"slotadmin:{p['slug']}") for p in products],
                arg,
                "adminslots",
                "admin",
            )
        elif action == "slotadmin":
            self.admin(uid)
            slots = self.s.rows(
                "SELECT s.*,o.id oid,o.status FROM slots s LEFT JOIN orders o ON o.slot_id=s.id "
                "AND o.status IN ('requested','quoted','paid','working','delivered') WHERE s.slug=? ORDER BY s.id",
                (arg,),
            )
            text = (
                "Слоты позиции "
                + arg
                + "\n\n"
                + "\n".join(
                    f"#{s['id']} {s['title']}: "
                    + (
                        f"заказ #{s['oid']} · {STATUS[s['status']]}"
                        if s["oid"]
                        else "открыт"
                        if s["active"]
                        else "закрыт"
                    )
                    for s in slots
                )
            )
            rows = [
                [
                    (
                        f"{'Закрыть' if s['active'] else 'Открыть'} #{s['id']}",
                        f"slottoggle:{s['id']}:{0 if s['active'] else 1}",
                    )
                ]
                for s in slots[:30]
            ]
            rows += [[("＋ Новый слот", f"slotnew:{arg}")], [("🔙 Позиции", "adminslots:0")]]
            await self.tell(context, uid, text + "\nЗакрытие слота не отменяет уже созданный заказ.", rows)
        elif action == "slottoggle":
            self.admin(uid)
            self.s.set_slot(int(arg), bool(int(parts[2])))
            await self.tell(context, uid, "Слот обновлён.", [[("🗓 Слоты", "adminslots:0")]])
        elif action == "slotnew":
            self.admin(uid)
            self.s.save_dialog(uid, "slotnew", {"slug": arg})
            await self.tell(
                context, uid, "Напишите название слота: например «Июнь · место 1» или «На этой неделе»."
            )
        elif action in {"addriddle", "additem", "addych", "imagehelp", "stats", "broadcast"}:
            self.admin(uid)
            if action in {"addriddle", "additem", "addych"}:
                await self.begin_admin_dialog(context, uid, action)
            elif action == "imagehelp":
                await self.tell(
                    context,
                    uid,
                    "/image main · studio · cube · classic · light · minecraft · about · riddles · shop\n"
                    "/image product:classic_head\n/image riddle:ID\n/image item:ID\n"
                    "После команды отправьте изображение. /portfolio classic — добавить пример работы.",
                )
            elif action == "stats":
                await self.statistics(context, uid)
            else:
                self.s.save_dialog(uid, "broadcast", {})
                await self.tell(
                    context,
                    uid,
                    "Пришлите текст, фото, файл или другое сообщение для рассылки. "
                    "Сначала появится подтверждение. /cancel — отмена.",
                )
        elif action == "broadcastsend":
            self.admin(uid)
            d = self.s.dialog(uid)
            if not d or d[0] != "broadcast_confirm":
                raise RuleError("Рассылка уже запущена или истекла.")
            self.s.clear_dialog(uid)
            context.application.create_task(self.broadcast(context, d[1]))
            await self.tell(context, uid, "Рассылка запущена. Итог придёт отдельным сообщением.")
        else:
            raise RuleError("Кнопка устарела. Отправьте /start.")

    async def begin_admin_dialog(self, context, uid, kind):
        self.s.save_dialog(uid, kind, {"step": "fields"})
        if kind == "addriddle":
            text = "Введите через | : текст загадки | ответ | награда pts | общий лимит или -\nЗатем пришлите изображение. /cancel — отмена."
        elif kind == "addych":
            text = "Введите через | : название YCH | цена ₽ | число мест | описание и условия\nЗатем загрузите изображение YCH. /cancel — отмена."
        else:
            text = "Введите через | : название | цена pts | общий лимит или - | лимит на пользователя или - | manual или auto | описание | содержимое\nЗатем пришлите изображение. /cancel — отмена."
        await self.tell(context, uid, text)

    async def on_message(self, update, context):
        uid = update.effective_user.id
        self.s.user(uid, update.effective_user.username)
        msg = update.effective_message
        d = self.s.dialog(uid)
        if not d:
            await msg.reply_text(
                "Выберите действие через /start. Сообщение Tibris можно отправить через /contact."
            )
            return
        kind, data = d
        text = (msg.text or "").strip()
        # All media and text use this single router. Broadcast always takes precedence.
        if kind == "broadcast":
            self.admin(uid)
            self.s.save_dialog(uid, "broadcast_confirm", {"chat": msg.chat_id, "message": msg.message_id})
            await self.tell(
                context,
                uid,
                "Отправить это сообщение всем пользователям?",
                [[("📣 Отправить", "broadcastsend"), ("Отменить", "home")]],
            )
        elif kind == "proof":
            proof = self.media_of(msg) or {}
            if text:
                proof["text"] = text[:3500]
            elif msg.caption:
                proof["text"] = msg.caption[:3500]
            if not proof:
                raise RuleError("Пришлите номер операции или чек файлом/фото.")
            self.s.payment_proof(uid, data["oid"], proof)
            self.s.clear_dialog(uid)
            await self.notify(
                context,
                self.cfg.admin_id,
                f"🧾 Клиент отправил подтверждение по заказу #{data['oid']}. "
                "Проверьте его через очередь в /start → Управление.",
            )
            await msg.reply_text("Подтверждение передано. Tibris проверит поступление и обновит статус.")
        elif kind == "contact":
            if uid == self.cfg.admin_id:
                raise RuleError("Для ответа клиенту используйте /reply ID текст или /send ID и файл.")
            await context.bot.send_message(
                self.cfg.admin_id,
                f"💬 Сообщение от @{update.effective_user.username or uid}\n"
                f"ID: {uid}. Ответ: /reply {uid} текст; файл: /send {uid}.",
            )
            await context.bot.copy_message(self.cfg.admin_id, msg.chat_id, msg.message_id)
            self.s.clear_dialog(uid)
            await msg.reply_text("Передал сообщение Tibris 🐾")
        elif kind == "send":
            self.admin(uid)
            await context.bot.copy_message(data["uid"], msg.chat_id, msg.message_id)
            self.s.clear_dialog(uid)
            await msg.reply_text("Сообщение отправлено.")
        elif kind in {"image", "portfolio"}:
            self.admin(uid)
            media = self.media_of(msg)
            if not media or (media["kind"] == "document" and not media.get("mime", "").startswith("image/")):
                raise RuleError("Пришлите изображение фото или файлом PNG/JPEG/WebP.")
            key = data["key"]
            if key.startswith(("riddle:", "item:")):
                table = "riddles" if key.startswith("riddle:") else "shop_items"
                with self.s.db(True) as c:
                    if (
                        c.execute(
                            f"UPDATE {table} SET image=?,image_kind=? WHERE id=?",
                            (media["file_id"], media["kind"], int(key.split(":")[1])),
                        ).rowcount
                        != 1
                    ):
                        raise RuleError("Запись не найдена.")
            else:
                self.s.set_media(key, media["file_id"], media["kind"])
            self.s.clear_dialog(uid)
            await msg.reply_text("Изображение сохранено. Оно появится в выбранном разделе.")
        elif kind == "text":
            self.admin(uid)
            if not text or len(text) > 3500:
                raise RuleError("Пришлите текст до 3500 символов.")
            self.s.set_setting("text:" + data["key"], text)
            self.s.clear_dialog(uid)
            await msg.reply_text("Текст обновлён.")
        elif kind == "quote":
            self.admin(uid)
            fields = text.split("|", 2)
            if len(fields) != 3 or not fields[2].strip():
                raise RuleError("Формат: рубли | Stars | условия и срок")
            o = self.s.quote(data["oid"], positive(fields[0]), positive(fields[1]), fields[2].strip())
            self.s.clear_dialog(uid)
            await self.order_card(context, o["user_id"], o["id"])
            await msg.reply_text("Предложение отправлено клиенту.")
        elif kind == "deliver":
            self.admin(uid)
            media = self.media_of(msg)
            if not media:
                raise RuleError("Отправьте готовый результат файлом или фото.")
            o = self.s.order(data["oid"])
            if o["status"] not in {"paid", "working"}:
                raise RuleError("Заказ недоступен для выдачи.")
            # Record delivered only after Telegram accepted the file. On failure dialog remains retryable.
            await self.send_file(
                context,
                o["user_id"],
                media,
                f"🐾 Ваш заказ #{o['id']} готов. Спасибо за доверие! /paysupport — вопросы.",
            )
            self.s.delivered(o["id"], media)
            self.s.clear_dialog(uid)
            await msg.reply_text("Результат отправлен, заказ закрыт. Клиент может скачать его снова.")
        elif kind == "order":
            if data["step"] == "brief":
                if not text or len(text) > 3500:
                    raise RuleError("Пришлите описание текстом до 3500 символов.")
                data.update(brief=text, step="refs")
                self.s.save_dialog(uid, kind, data)
                await self.tell(
                    context,
                    uid,
                    "Прикрепите до 10 референсов: фото или файлы. Можно отправить по одному "
                    "или альбомом. Когда закончите, нажмите кнопку ниже.",
                    [[("Готово / без референсов", "refs_done")]],
                )
            elif data["step"] == "refs":
                media = self.media_of(msg)
                if not media:
                    raise RuleError("Пришлите фото/файл или нажмите «Готово».")
                if len(data["refs"]) >= 10:
                    raise RuleError("Уже прикреплено 10 файлов. Нажмите «Готово».")
                data["refs"].append(media)
                self.s.save_dialog(uid, kind, data)
                await self.tell(
                    context, uid, f"Референсов: {len(data['refs'])}/10", [[("Готово", "refs_done")]]
                )
            else:
                raise RuleError("Выберите купон кнопкой под заявкой.")
        elif kind == "promo":
            if not text:
                raise RuleError("Промокод нужно отправить текстом.")
            cid, percent = self.s.claim_promo(uid, text)
            self.s.clear_dialog(uid)
            await self.tell(
                context,
                uid,
                f"🎟 Промокод активирован! Купон #{cid}: скидка {percent}% на один заказ. "
                "Выберите его при оформлении заявки.",
                [[("🎨 Каталог", "studio")]],
            )
        elif kind == "slotnew":
            self.admin(uid)
            if not text or len(text) > 100:
                raise RuleError("Название: до 100 символов.")
            sid = self.s.add_slot(data["slug"], text)
            self.s.clear_dialog(uid)
            await msg.reply_text(f"Слот #{sid} создан.")
        elif kind in {"answer", "code"}:
            if not text:
                raise RuleError("Отправьте ответ текстом.")
            if kind == "code":
                reward = self.s.complete_task(uid, data["tid"], "code", text)
                self.s.clear_dialog(uid)
                await msg.reply_text(f"✅ Верно! +{reward} pts")
            else:
                correct, reward, remaining = self.s.answer(uid, data["rid"], text)
                if correct or remaining == 0:
                    self.s.clear_dialog(uid)
                await msg.reply_text(
                    f"🎉 Правильно! +{reward} pts"
                    if correct
                    else f"Ответ не подошёл. Осталось попыток: {remaining}/3."
                )
        elif kind in {"addriddle", "additem", "addych"}:
            self.admin(uid)
            if data["step"] == "fields":
                fields = [v.strip() for v in text.split("|")]
                if kind == "addriddle":
                    if len(fields) != 4 or not fields[0] or not fields[1]:
                        raise RuleError("Текст | ответ | награда | лимит или -")
                    data["fields"] = [
                        fields[0],
                        fields[1],
                        positive(fields[2]),
                        None if fields[3] == "-" else positive(fields[3]),
                    ]
                elif kind == "addych":
                    if len(fields) != 4 or not fields[0] or not fields[3]:
                        raise RuleError("Название | цена ₽ | число мест | описание")
                    data["fields"] = [fields[0], positive(fields[1]), positive(fields[2]), fields[3]]
                else:
                    if len(fields) != 7 or not fields[0] or fields[4] not in {"manual", "auto"}:
                        raise RuleError(
                            "Название | цена | общий лимит или - | лимит пользователя или - | manual/auto | описание | содержимое"
                        )
                    if fields[4] == "auto" and not fields[6]:
                        raise RuleError("Для автоматической выдачи укажите содержимое.")
                    data["fields"] = [
                        fields[0],
                        positive(fields[1]),
                        fields[5],
                        None if fields[2] == "-" else positive(fields[2]),
                        None if fields[3] == "-" else positive(fields[3]),
                        fields[4] == "manual",
                        fields[6],
                    ]
                data["step"] = "image"
                self.s.save_dialog(uid, kind, data)
                await msg.reply_text("Теперь пришлите изображение фото/файлом или напишите «нет».")
            else:
                media = self.media_of(msg)
                if kind == "addych" and not media:
                    raise RuleError("Для YCH обязательно прикрепите своё изображение.")
                if not media and text.casefold() not in {"нет", "skip", "-"}:
                    raise RuleError("Пришлите изображение или напишите «нет».")
                if media and media["kind"] == "document" and not media.get("mime", "").startswith("image/"):
                    raise RuleError("Для карточки нужен файл изображения.")
                image, media_kind = (media["file_id"], media["kind"]) if media else ("", "photo")
                if kind == "addriddle":
                    oid = self.s.add_riddle(*data["fields"], image=image, kind=media_kind)
                elif kind == "addych":
                    oid = self.s.add_ych(*data["fields"], media)
                else:
                    oid = self.s.add_item(*data["fields"], image=image, kind=media_kind)
                self.s.clear_dialog(uid)
                await msg.reply_text(f"Создано: #{oid}.")
        else:
            raise RuleError("Используйте кнопки диалога или /cancel.")

    async def broadcast(self, context, data):
        users = self.s.rows("SELECT user_id FROM users")
        delivered = 0
        for user in users:
            for attempt in range(3):
                try:
                    await context.bot.copy_message(user["user_id"], data["chat"], data["message"])
                    delivered += 1
                    break
                except RetryAfter as e:
                    delay = (
                        e.retry_after.total_seconds()
                        if hasattr(e.retry_after, "total_seconds")
                        else e.retry_after
                    )
                    await asyncio.sleep(float(delay) + 0.1)
                except TelegramError:
                    break
            await asyncio.sleep(0.05)
        await self.notify(context, self.cfg.admin_id, f"Рассылка завершена: {delivered}/{len(users)}.")

    async def statistics(self, context, uid):
        u = self.s.one("SELECT COUNT(*) n,COALESCE(SUM(points),0) p FROM users")
        orders = self.s.rows("SELECT status,COUNT(*) n FROM orders GROUP BY status")
        await self.tell(
            context,
            uid,
            f"👥 Пользователей: {u['n']}\n💎 Баллов на счетах: {u['p']}\n\n"
            + "\n".join(f"{STATUS[o['status']]}: {o['n']}" for o in orders),
        )

    async def admin_command(self, update, context):
        uid = update.effective_user.id
        self.admin(uid)
        command = update.message.text.split()[0].split("@")[0].lstrip("/")
        args = shlex.split(update.message.text, posix=True)[1:]
        if command in {"admin", "adminhelp"}:
            await self.tell(context, uid, ADMIN_HELP)
        elif command in {"addriddle", "additem", "addych"}:
            await self.begin_admin_dialog(context, uid, command)
        elif command == "addslot":
            sid = self.s.add_slot(args[0], " ".join(args[1:]))
            await update.message.reply_text(f"Слот #{sid} создан.")
        elif command in {"openslot", "closeslot"}:
            self.s.set_slot(int(args[0]), command == "openslot")
            await update.message.reply_text("Слот обновлён.")
        elif command == "slots":
            slots = self.s.rows(
                "SELECT id,slug,title,active FROM slots" + (" WHERE slug=?" if args else ""),
                (args[0],) if args else (),
            )
            await self.tell(
                context,
                uid,
                "\n".join(
                    f"#{s['id']} {s['slug']} · {s['title']} · {'открыт' if s['active'] else 'закрыт'}"
                    for s in slots
                )
                or "Слотов нет.",
            )
        elif command == "addpromo":
            self.s.add_promo(
                args[0], args[1], None if args[2] == "-" else args[2], None if args[3] == "-" else args[3]
            )
            await update.message.reply_text(
                "Промокод создан: одна активация на пользователя, лимит и срок учтены."
            )
        elif command == "removepromo":
            with self.s.db(True) as c:
                c.execute("UPDATE promos SET active=0 WHERE code=?", (args[0].strip().casefold(),))
            await update.message.reply_text(
                "Промокод закрыт для новых активаций. Уже полученные купоны сохраняются."
            )
        elif command == "promos":
            promos = self.s.rows("SELECT code,percent,total_limit,active FROM promos")
            await self.tell(context, uid, "\n".join(str(p) for p in promos) or "Промокодов нет.")
        elif command == "image":
            key = args[0]
            if key not in {
                "main",
                "studio",
                "cube",
                "classic",
                "light",
                "minecraft",
                "about",
                "riddles",
                "shop",
            }:
                if key.startswith("product:"):
                    if not self.s.one("SELECT slug FROM art_products WHERE slug=?", (key.split(":", 1)[1],)):
                        raise RuleError("SKU не найден.")
                elif key.startswith("payment:"):
                    if not self.s.setting("method:" + key.split(":", 1)[1]):
                        raise RuleError("Сначала настройте способ через /method.")
                elif not (key.startswith(("riddle:", "item:")) and key.split(":", 1)[1].isdigit()):
                    raise RuleError("Неизвестный раздел изображения. /adminhelp")
            self.s.save_dialog(uid, "image", {"key": key})
            await update.message.reply_text("Отправьте изображение фото или файлом. /cancel — отмена.")
        elif command == "portfolio":
            if args[0] not in CATEGORIES:
                raise RuleError("Категория: classic, light или minecraft.")
            self.s.save_dialog(uid, "portfolio", {"key": f"portfolio:{args[0]}:{time.time_ns()}"})
            await update.message.reply_text(
                "Отправьте свою работу изображением. Она появится в примерах этого раздела."
            )
        elif command == "text":
            if args[0] not in {"home", "about", "terms", "classic", "light", "minecraft"}:
                raise RuleError("Раздел: home/about/terms/classic/light/minecraft")
            self.s.save_dialog(uid, "text", {"key": args[0]})
            await update.message.reply_text("Отправьте новый текст раздела.")
        elif command == "set":
            if args[0] not in {"artist_name", "artist_username", "portfolio_url", "commissions_open"}:
                raise RuleError(
                    "Настройка: artist_name, artist_username, portfolio_url, commissions_open (1/0)."
                )
            self.s.set_setting(
                args[0],
                " ".join(args[1:]).lstrip("@") if args[0] == "artist_username" else " ".join(args[1:]),
            )
            await update.message.reply_text("Настройка сохранена.")
        elif command == "method":
            method, currency = args[0].lower(), args[1].upper()
            if (
                not method.replace("_", "").isalnum()
                or len(method) > 24
                or currency not in {"RUB", "USD", "EUR", "GRAM", "TON", "USDT"}
            ):
                raise RuleError("Название: до 24 букв/цифр; валюта: RUB USD EUR GRAM TON USDT.")
            instructions = " ".join(args[2:])
            if not instructions or len(instructions) > 2500:
                raise RuleError(
                    "Укажите реквизиты или ссылку, до 2500 символов. Для крипты обязательно укажите сеть."
                )
            self.s.set_setting(
                "method:" + method,
                json.dumps({"currency": currency, "instructions": instructions}, ensure_ascii=False),
            )
            await update.message.reply_text(
                "Способ настроен. /offer ID способ сумма базовая_цена_руб условия — предложение клиенту."
            )
        elif command == "methods":
            methods = self.s.rows("SELECT key,value FROM settings WHERE key LIKE 'method:%'")
            await self.tell(
                context,
                uid,
                "\n".join(f"{m['key'][7:]} · {json.loads(m['value'])['currency']}" for m in methods)
                or "Способы ещё не настроены.",
            )
        elif command == "methodauto":
            method, currency, provider = args[0].lower(), args[1].upper(), args[2].lower()
            if not method.replace("_", "").isalnum() or len(method) > 24:
                raise RuleError("Название: до 24 букв/цифр.")
            if not (
                (provider == "cryptopay" and currency in {"TON", "GRAM", "USDT"})
                or (provider == "yoomoney" and currency == "RUB")
            ):
                raise RuleError("cryptopay: TON/GRAM/USDT; yoomoney: RUB.")
            self.s.set_setting(
                "method:" + method,
                json.dumps(
                    {
                        "currency": currency,
                        "provider": provider,
                        "instructions": "Оплата подтверждается по API.",
                    }
                ),
            )
            await update.message.reply_text(
                "Способ подготовлен. Для работы добавьте ключи провайдера в .env и перезапустите бота."
            )
        elif command == "autopaystatus":
            rows = self.s.rows(
                "SELECT order_id,provider,state,remote_id FROM auto_invoices ORDER BY id DESC LIMIT 40"
            )
            await self.tell(
                context,
                uid,
                "\n".join(
                    f"#{r['order_id']} {r['provider']} · {r['state']} · {r['remote_id'] or 'создание не подтверждено'}"
                    for r in rows
                )
                or "Автоматических счетов нет.",
            )
            reviews = self.s.rows("SELECT * FROM auto_review WHERE resolved=0")
            if reviews:
                await self.tell(
                    context,
                    uid,
                    "Требуют ручной проверки / возврата:\n"
                    + "\n".join(
                        f"Заказ #{r['order_id']}: {r['provider']} · операция {r['reference']}"
                        for r in reviews
                    ),
                )
        elif command == "voidinvoice":
            oid = int(args[0])
            self.s.void_auto(oid)
            self.s.cancel_order(uid, oid, admin=True)
            o = self.s.order(oid)
            await self.notify(
                context,
                o["user_id"],
                f"Заявка #{oid} отменена. Не используйте старую платёжную ссылку. /paysupport — если уже переводили деньги.",
            )
            await update.message.reply_text(
                "Заявка отменена, слот освобождён. Позднее поступление по старой ссылке будет помечено для ручного возврата."
            )
        elif command == "methodoff":
            with self.s.db(True) as c:
                c.execute("DELETE FROM settings WHERE key=?", ("method:" + args[0],))
            await update.message.reply_text(
                "Способ убран из настроек для новых предложений. Существующие предложения сохраняются."
            )
        elif command == "offer":
            o = self.s.external_offer(int(args[0]), args[1], args[2], args[3], " ".join(args[4:]))
            await self.order_card(context, o["user_id"], o["id"])
            await update.message.reply_text("Предложение внешнего расчёта отправлено.")
        elif command == "markpaid":
            self.s.confirm_external(int(args[0]), " ".join(args[1:]))
            o = self.s.order(int(args[0]))
            await self.notify(
                context,
                o["user_id"],
                f"✅ Tibris подтвердил поступление по заказу #{o['id']}. Заказ оплачен.",
            )
            await update.message.reply_text(
                "Оплата отмечена. Это ручная запись после вашей проверки поступления."
            )
        elif command == "refundexternal":
            self.s.refund_external(int(args[0]), " ".join(args[1:]))
            o = self.s.order(int(args[0]))
            await self.notify(
                context,
                o["user_id"],
                f"Tibris отметил внешний возврат по заказу #{o['id']}. /paysupport — вопросы.",
            )
            await update.message.reply_text(
                "Записан уже выполненный вами внешний возврат. Бот не переводит деньги."
            )
        elif command == "price":
            rub = positive(args[1])
            stars = None if args[2] == "-" else positive(args[2])
            with self.s.db(True) as c:
                if (
                    c.execute(
                        "UPDATE art_products SET rub=?,stars=? WHERE slug=?", (rub, stars, args[0])
                    ).rowcount
                    != 1
                ):
                    raise RuleError("SKU не найден.")
            await update.message.reply_text("Прайс обновлён. Существующие предложения клиентам не меняются.")
        elif command == "artpoints":
            points = None if args[1] == "off" else positive(args[1])
            with self.s.db(True) as c:
                if (
                    c.execute("UPDATE art_products SET points=? WHERE slug=?", (points, args[0])).rowcount
                    != 1
                ):
                    raise RuleError("SKU не найден.")
            await update.message.reply_text("Доступность арта за pts обновлена.")
        elif command == "reward":
            rid = self.s.reward(" ".join(args[2:]), args[0], args[1])
            await update.message.reply_text(f"Бонус #{rid} создан.")
        elif command in {"quote", "quotepts"}:
            oid = int(args[0])
            if command == "quote":
                o = self.s.quote(
                    oid, args[1], args[2], " ".join(args[3:]) or "Срок и правки по согласованию."
                )
            else:
                o = self.s.order(oid)
                p = self.s.one("SELECT * FROM art_products WHERE slug=?", (o["slug"],))
                if not p["points"]:
                    raise RuleError("Включите формат через /artpoints SKU цена.")
                o = self.s.quote(oid, p["rub"], None, " ".join(args[1:]), p["points"])
            await self.order_card(context, o["user_id"], oid)
            await update.message.reply_text("Предложение отправлено.")
        elif command == "orders":
            orders = self.s.rows("SELECT id,user_id,status,slug FROM orders ORDER BY id DESC LIMIT 40")
            await self.tell(
                context,
                uid,
                "\n".join(f"#{o['id']} {o['slug']} {STATUS[o['status']]} · {o['user_id']}" for o in orders)
                or "Заказов нет.",
            )
        elif command == "deliverart":
            o = self.s.order(int(args[0]))
            if o["status"] not in {"paid", "working"}:
                raise RuleError("Заказ ещё не оплачен или уже закрыт.")
            self.s.save_dialog(uid, "deliver", {"oid": o["id"]})
            await update.message.reply_text("Отправьте готовый арт файлом или фото.")
        elif command == "refund":
            o = self.s.order(int(args[0]))
            if o["status"] not in {"paid", "working", "delivered"}:
                raise RuleError("Заказ уже возвращён или не оплачен.")
            if o["payment_method"]:
                raise RuleError(
                    "Возврат выполняется во внешнем сервисе вручную. Затем /refundexternal ID номер_возврата."
                )
            if o["charge"]:
                await context.bot.refund_star_payment(o["user_id"], o["charge"])
                self.s.mark_refund(o["charge"])
            else:
                self.s.refund_points(o["id"])
            await self.notify(context, o["user_id"], f"Возврат по заказу #{o['id']} выполнен.")
            await update.message.reply_text("Возврат выполнен.")
        elif command == "refundcharge":
            p = self.s.one("SELECT * FROM payments WHERE charge=? AND state='refund_required'", (args[0],))
            if not p:
                raise RuleError("Нет такого платежа, ожидающего возврата.")
            await context.bot.refund_star_payment(p["user_id"], p["charge"])
            self.s.mark_refund(p["charge"])
            await update.message.reply_text("Платёж возвращён.")
        elif command in {"addpoints", "removepoints", "setpoints"}:
            target, balance = self.s.adjust(
                args[0],
                int(args[1]),
                {"addpoints": "add", "removepoints": "remove", "setpoints": "set"}[command],
                " ".join(args[2:]) or "Администратор",
            )
            ok = await self.notify(
                context, target, f"Tibris изменил ваш баланс: {balance} pts.\n{' '.join(args[2:])}"
            )
            await update.message.reply_text(
                "Баланс изменён."
                + (" Уведомление отправлено." if ok else " Пользователь недоступен для уведомления.")
            )
        elif command in {"addsub", "addext", "addcode"}:
            # Explicit separator format removes ambiguous optional integer arguments.
            fields = [v.strip() for v in update.message.text.split(" ", 1)[1].split("|")]
            if command == "addsub" and len(fields) == 4:
                tid = self.s.add_task(
                    fields[0], fields[3], fields[1], "subscription", None if fields[2] == "-" else fields[2]
                )
            elif command == "addext" and len(fields) == 5:
                tid = self.s.add_task(
                    fields[0],
                    fields[4],
                    fields[1],
                    "external",
                    None if fields[2] == "-" else fields[2],
                    fields[3],
                )
            elif command == "addcode" and len(fields) == 5:
                tid = self.s.add_task(
                    fields[0],
                    fields[4],
                    fields[1],
                    "code",
                    None if fields[2] == "-" else fields[2],
                    code=fields[3],
                )
            else:
                raise RuleError("Неверный формат. Смотрите /adminhelp; поля разделяются |")
            await update.message.reply_text(f"Задание #{tid} создано.")
        elif command in {"removeriddle", "removesub", "removeitem", "removereward"}:
            table = {
                "removeriddle": "riddles",
                "removesub": "subscriptions",
                "removeitem": "shop_items",
                "removereward": "rewards",
            }[command]
            self.s.deactivate(table, int(args[0]))
            await update.message.reply_text("Запись деактивирована.")
        elif command in {"listriddles", "listsubs", "listitems", "users", "earnings"}:
            sql = {
                "listriddles": "SELECT id,text,points_reward,total_limit FROM riddles WHERE is_active=1",
                "listsubs": "SELECT id,description,task_type,points_reward,total_limit FROM subscriptions WHERE is_active=1",
                "listitems": "SELECT id,name,price,manual_delivery FROM shop_items WHERE is_active=1",
                "users": "SELECT user_id,username,points FROM users ORDER BY points DESC",
                "earnings": "SELECT reason,SUM(amount) amount FROM ledger WHERE amount>0 GROUP BY reason",
            }[command]
            rows = self.s.rows(sql)
            await self.tell(
                context,
                uid,
                "\n".join(" · ".join(str(v) for v in r.values()) for r in rows) or "Нет записей.",
            )
        elif command == "stats":
            await self.statistics(context, uid)
        elif command == "pending":
            rows = self.s.rows(
                "SELECT p.id,p.user_id,i.name FROM purchases p JOIN shop_items i ON i.id=p.item_id WHERE p.delivered=0"
            )
            await self.tell(
                context,
                uid,
                "\n".join(f"#{p['id']} {p['name']} · {p['user_id']}" for p in rows)
                or "Невыданных товаров нет.",
            )
        elif command == "deliver":
            pid = int(args[0])
            p = self.s.one("SELECT * FROM purchases WHERE id=?", (pid,))
            if not p or p["delivered"]:
                raise RuleError("Покупка не найдена или уже выдана.")
            content = " ".join(args[1:]) or p["content"]
            await self.tell(context, p["user_id"], f"Товар Cube #{pid} выдан!\n{content}")
            with self.s.db(True) as c:
                c.execute("UPDATE purchases SET delivered=1,content=? WHERE id=?", (content, pid))
            await update.message.reply_text("Товар выдан.")
        elif command == "reply":
            await self.tell(context, int(args[0]), "🐾 Tibris:\n" + " ".join(args[1:]))
            await update.message.reply_text("Ответ отправлен.")
        elif command == "send":
            self.s.save_dialog(uid, "send", {"uid": int(args[0])})
            await update.message.reply_text("Отправьте сообщение или файл для этого клиента.")
        elif command == "broadcast":
            self.s.save_dialog(uid, "broadcast", {})
            await update.message.reply_text("Пришлите сообщение для рассылки. Затем подтвердите отправку.")

    async def precheckout(self, update, context):
        q = update.pre_checkout_query
        try:
            self.s.precheckout(q.from_user.id, q.invoice_payload, q.total_amount, q.currency)
        except RuleError as e:
            await q.answer(ok=False, error_message=str(e))
        else:
            await q.answer(ok=True)

    async def payment(self, update, context):
        p = update.message.successful_payment
        uid = update.effective_user.id
        oid, fresh, state = self.s.paid(
            uid, p.invoice_payload, p.total_amount, p.currency, p.telegram_payment_charge_id
        )
        if not fresh:
            return
        if state == "refund_required":
            try:
                await context.bot.refund_star_payment(uid, p.telegram_payment_charge_id)
                self.s.mark_refund(p.telegram_payment_charge_id)
                await update.message.reply_text(
                    "Счёт изменился или заказ уже оплачен. Этот платёж возвращён."
                )
            except TelegramError:
                await self.notify(
                    context,
                    self.cfg.admin_id,
                    f"Требуется возврат платежа: /refundcharge {p.telegram_payment_charge_id}",
                )
                await update.message.reply_text(
                    "Платёж требует проверки и возврата. Tibris уведомлён; /paysupport."
                )
            return
        await update.message.reply_text(f"⭐ Оплата заказа #{oid} подтверждена! Tibris уведомлён.")
        await self.notify(
            context, self.cfg.admin_id, f"⭐ Заказ #{oid} оплачен: {p.total_amount} Stars. /admin — очередь."
        )

    async def privacy(self, update, context):
        await update.message.reply_text(
            "Бот хранит ваш Telegram ID, username, баланс, выполнение заданий, покупки, "
            "ТЗ, референсы и данные платежей (идентификатор и сумму, без банковской карты). "
            "Они нужны для заказов и доступны Tibris. Не присылайте пароли или документы личности. "
            "Для удаления данных или вопросов используйте /contact. Состояние диалога действует 30 минут."
        )

    async def refund_event(self, update, context):
        payment = update.message.refunded_payment
        if self.s.one("SELECT charge FROM payments WHERE charge=?", (payment.telegram_payment_charge_id,)):
            self.s.mark_refund(payment.telegram_payment_charge_id)

    async def slot_choice(self, context, uid, slug):
        slots = self.s.free_slots(slug, uid)
        rows = [[(s["title"], f"chooseslot:{s['id']}:{slug}")] for s in slots[:30]]
        rows += [[("💬 Написать Tibris", "contact")], [("🔙 Формат", f"product:{slug}")]]
        await self.tell(
            context,
            uid,
            "🗓 Выберите свободный слот. Черновик резервирует его на 30 минут. "
            "После отправки заявки место закрепляется за заказом."
            if slots
            else "Слоты этой позиции разобраны. Напишите Tibris или выберите другой формат.",
            rows,
        )

    async def promo_command(self, update, context):
        uid = update.effective_user.id
        self.s.user(uid, update.effective_user.username)
        if context.args:
            cid, percent = self.s.claim_promo(uid, context.args[0])
            await update.message.reply_text(f"Купон #{cid}: −{percent}%. Выберите его при оформлении заявки.")
        else:
            self.s.save_dialog(uid, "promo", {})
            await update.message.reply_text("Отправьте текстовый промокод.")

    async def payment_notice(self, context, oid):
        o = self.s.order(oid)
        await self.notify(
            context, o["user_id"], f"✅ Оплата заказа #{oid} подтверждена автоматически. Tibris уведомлён."
        )
        await self.notify(
            context,
            self.cfg.admin_id,
            f"💳 Заказ #{oid} оплачен через {o['payment_method']}. Откройте очередь в /start.",
        )

    async def poll_payments(self, app):
        context = type("PaymentContext", (), {"bot": app.bot})()
        while True:
            rows = self.s.rows(
                "SELECT order_id FROM auto_invoices WHERE state IN ('pending','watching') ORDER BY id LIMIT 1000"
            )
            for row in rows:
                try:
                    result = await self.auto.check(row["order_id"])
                    if result == "review":
                        await self.review_notice(context, row["order_id"])
                    elif result is True:
                        await self.payment_notice(context, row["order_id"])
                except (httpx.HTTPError, RuleError, ValueError, KeyError):
                    log.warning("Automatic payment check deferred for order %s", row["order_id"])
                await asyncio.sleep(0.2)
            await asyncio.sleep(30)

    async def review_notice(self, context, oid):
        o = self.s.order(oid)
        await self.notify(
            context,
            o["user_id"],
            f"Поступление по отменённому/изменённому заказу #{oid} передано Tibris для проверки и возврата. /paysupport.",
        )
        await self.notify(
            context,
            self.cfg.admin_id,
            f"⚠️ Поздний платёж по заказу #{oid}. Заказ не возобновлён, слот не занят. /autopaystatus — операция для проверки и возврата.",
        )

    async def post_init(self, app):
        if self.cfg.crypto_pay_token or self.cfg.yoomoney_token:
            self.poll_task = asyncio.create_task(self.poll_payments(app))

    async def post_stop(self, app):
        if self.poll_task:
            self.poll_task.cancel()
            try:
                await self.poll_task
            except asyncio.CancelledError:
                pass

    async def guarded(self, handler, update, context):
        if getattr(update, "effective_chat", None) and update.effective_chat.type != "private":
            await update.effective_message.reply_text(
                "Для заказов и управления напишите боту в личные сообщения."
            )
            return
        try:
            await handler(update, context)
        except RuleError as e:
            await update.effective_message.reply_text(str(e))
        except (IndexError, ValueError):
            await update.effective_message.reply_text(
                "Проверьте формат команды и числа. /adminhelp — помощь; /cancel — отмена."
            )
        except httpx.HTTPError:
            await update.effective_message.reply_text(
                "Провайдер временно недоступен. Не оплачивайте повторно; повторите проверку позже."
            )

    async def error(self, update, context):
        # Do not log Update objects or Bot API URLs (they may contain private briefs/tokens).
        log.error("Handler failed: %s", type(context.error).__name__)
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "Не удалось выполнить действие. Попробуйте ещё раз; /paysupport — связь."
                )
            except TelegramError:
                pass

    def application(self):
        app = (
            Application.builder()
            .token(self.cfg.token)
            .concurrent_updates(False)
            .post_init(self.post_init)
            .post_stop(self.post_stop)
            .build()
        )

        def wrap(handler):
            async def wrapped(update, context):
                await self.guarded(handler, update, context)

            return wrapped

        async def contact_command(update, context):
            self.s.user(update.effective_user.id, update.effective_user.username)
            await self.contact(context, update.effective_user.id)

        app.add_handler(PreCheckoutQueryHandler(self.precheckout))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, self.payment))
        app.add_handler(MessageHandler(filters.StatusUpdate.REFUNDED_PAYMENT, self.refund_event))
        app.add_handler(CommandHandler("start", wrap(self.start)))
        app.add_handler(CommandHandler("cancel", wrap(self.cancel)))
        app.add_handler(CommandHandler(["contact", "paysupport"], wrap(contact_command)))
        app.add_handler(CommandHandler("privacy", self.privacy))
        app.add_handler(CommandHandler("promo", wrap(self.promo_command)))
        commands = [
            "admin",
            "adminhelp",
            "addriddle",
            "additem",
            "image",
            "portfolio",
            "text",
            "set",
            "price",
            "artpoints",
            "reward",
            "quote",
            "quotepts",
            "orders",
            "deliverart",
            "refund",
            "refundcharge",
            "addpoints",
            "removepoints",
            "setpoints",
            "addsub",
            "addext",
            "addcode",
            "removeriddle",
            "removesub",
            "removeitem",
            "removereward",
            "listriddles",
            "listsubs",
            "listitems",
            "users",
            "earnings",
            "stats",
            "pending",
            "deliver",
            "reply",
            "send",
            "broadcast",
        ]
        commands += ["method", "methods", "methodoff", "offer", "markpaid", "refundexternal"]
        commands += ["methodauto", "autopaystatus"]
        commands += ["voidinvoice"]
        commands += [
            "addslot",
            "slots",
            "openslot",
            "closeslot",
            "addych",
            "addpromo",
            "removepromo",
            "promos",
        ]
        app.add_handler(CommandHandler(commands, wrap(self.admin_command)))
        app.add_handler(CallbackQueryHandler(wrap(self.callback)))
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, wrap(self.on_message)))
        app.add_error_handler(self.error)
        return app


ADMIN_HELP = """⚙️ Tibris · команды администратора
/start → Управление — меню и очередь
/addriddle — загадка с фото, ответом, наградой и лимитом
/addych — YCH с собственной картинкой и ограниченным числом мест
/addslot SKU название · /slots [SKU]
/openslot ID · /closeslot ID — уже принятые заказы сохраняются
/addpromo КОД процент общий_лимит_или_- срок_в_днях_или_-
/promos · /removepromo КОД
/additem — товар с фото, лимитами и выдачей
/image РАЗДЕЛ — затем отправьте фото или файл
Разделы: main studio cube classic light minecraft about riddles shop
/image product:classic_head · /image riddle:ID · /image item:ID
/portfolio classic|light|minecraft — затем своя работа
/text home|about|terms|classic|light|minecraft — новый текст
/set artist_name Имя · /set artist_username username
/set portfolio_url URL · /set commissions_open 1 или 0

Внешний расчёт (подтверждение поступления вручную):
/method sbp RUB реквизиты_или_ссылка
/method hipolink USD ссылка_на_оплату
/method gram GRAM адрес_и_сеть
/method usdt USDT адрес_и_сеть
/image payment:sbp — отправьте QR-код фото/файлом
/methods · /methodoff название
/methodauto rub RUB yoomoney — read-only API кошелька, без ЮKassa
/methodauto ton TON cryptopay · /methodauto usdt USDT cryptopay
/autopaystatus — счета и состояние проверки
/voidinvoice ID — отменить заявку с внешней ссылкой; поздний платёж уйдёт на ручную проверку
/offer ID способ сумма_в_его_валюте базовая_цена_руб условия
/markpaid ID номер_операции — только после проверки в выписке/кабинете
/refundexternal ID номер_возврата — только после реального возврата вручную

/price SKU рубли Stars (или - для неустановленной цены Stars)
/artpoints SKU цена_pts (или off)
/reward цена_pts процент название — новый скидочный купон
/removereward ID — скрыть бонус, купленные купоны сохраняются

/orders — последние заявки; очередь целиком в меню
/quote ID рубли Stars условия и срок — согласованное предложение
/quotepts ID условия и срок — арт за настроенную цену pts
/deliverart ID — затем готовый файл/фото
/refund ID — возврат Stars/pts; выполняйте по согласованию с клиентом
/refundcharge CHARGE_ID — повторить неудавшийся возврат лишнего платежа
/reply USER_ID текст · /send USER_ID — следующий файл/сообщение клиенту

Задания: поля разделяются символом |
/addsub @канал | баллы | общий_лимит или - | описание
/addext https://URL | баллы | лимит или - | задержка_сек | описание
/addcode https://URL | баллы | лимит или - | код | описание
/removeriddle ID · /removesub ID · /removeitem ID
/listriddles · /listsubs · /listitems
/pending · /deliver ID содержимое — ручная выдача Cube
/addpoints ID_или_@username число причина
/removepoints ID_или_@username число причина
/setpoints ID_или_@username число причина
/stats · /users · /earnings — журнал новых начислений
/broadcast — сообщение любого типа + подтверждение
/cancel — отменить текущий диалог

SKU Classic: classic_head classic_half classic_sticker classic_full
SKU Light: light_icon light_chibi light_logo light_background
SKU Minecraft: mc_skin mc_4d mc_totem mc_animal
"""


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = Config.from_env()
    except ValueError as e:
        raise SystemExit(str(e)) from None
    Bot(config).application().run_polling(allowed_updates=Update.ALL_TYPES)
