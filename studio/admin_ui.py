"""Administrator menus and single-question forms; commands remain optional shortcuts."""

import json
import re
import time
from decimal import Decimal, InvalidOperation

from .catalog import CATEGORIES, STATUS
from .store import RuleError, positive


BACK = [[("← Управление", "admin")]]
SECTIONS = {
    "main": "Главная",
    "studio": "Студия",
    "cube": "Digital Cube",
    "classic": "Classic",
    "light": "Light",
    "minecraft": "Minecraft",
    "about": "Обо мне",
    "riddles": "Загадки",
    "shop": "Магазин",
}
TEXTS = {
    "home": "Главная",
    "about": "Обо мне",
    "terms": "Как заказать",
    "classic": "Classic",
    "light": "Light",
    "minecraft": "Minecraft",
}
TABLES = {
    "riddles": ("Загадки", "text", "is_active"),
    "subscriptions": ("Задания", "description", "is_active"),
    "shop_items": ("Товары", "name", "is_active"),
    "rewards": ("Бонусы", "name", "active"),
}


def field(key, prompt, kind="text", choices=None):
    return {"key": key, "prompt": prompt, "kind": kind, "choices": choices}


class AdminUI:
    async def admin_queue(self, context, uid, page=0, state="active"):
        self.admin(uid)
        self.s.clear_dialog(uid)
        if state == "active":
            condition, args = "o.status IN ('requested','quoted','paid','working')", ()
        elif state == "all":
            condition, args = "1=1", ()
        elif state in STATUS:
            condition, args = "o.status=?", (state,)
        else:
            raise RuleError("Раздел заказов не найден.")
        orders = self.s.rows(
            "SELECT o.*,p.title FROM orders o JOIN art_products p ON p.slug=o.slug WHERE "
            + condition
            + " ORDER BY o.id DESC",
            args,
        )
        page = max(0, int(page))
        rows = [
            [("📝 Новые", "adminorders:0:requested"), ("💳 Ждут оплаты", "adminorders:0:quoted")],
            [("✅ Оплачены", "adminorders:0:paid"), ("🎨 В работе", "adminorders:0:working")],
            [("Активные", "adminorders:0:active"), ("Все и архив", "adminorders:0:all")],
        ]
        rows += [
            [(f"#{o['id']} · {o['title']} · {STATUS[o['status']]}", f"order:{o['id']}")]
            for o in orders[page * 8 : (page + 1) * 8]
        ]
        navigation = []
        if page:
            navigation.append(("←", f"adminorders:{page - 1}:{state}"))
        if len(orders) > (page + 1) * 8:
            navigation.append(("→", f"adminorders:{page + 1}:{state}"))
        if navigation:
            rows.append(navigation)
        await self.tell(
            context,
            uid,
            f"📦 Заказы · {len(orders)}\nВыберите этап или откройте заказ."
            if orders
            else "📦 Здесь пока нет заказов. Выберите другой этап.",
            rows + BACK,
        )

    async def admin_home(self, context, uid):
        self.admin(uid)
        self.s.clear_dialog(uid)
        count = self.s.one(
            "SELECT COUNT(*) n FROM orders WHERE status IN ('requested','quoted','paid','working')"
        )["n"]
        opened = self.s.setting("commissions_open", "1") == "1"
        await self.tell(
            context,
            uid,
            f"⚙️ Привет, Tibris!\nАктивных заказов: {count}.\nПриём заявок {'открыт 🟢' if opened else 'закрыт 🔴'}.\n\nВыберите, что хотите настроить.",
            [
                [("📦 Заказы", "adminorders:0"), ("🗓 Слоты", "adminslots:0")],
                [("🎨 Каталог и цены", "aui:catalog"), ("💳 Оплата", "aui:payments")],
                [("🖼 Картинки и тексты", "aui:content"), ("🎟 Промокоды", "aui:promos")],
                [("🎲 Digital Cube", "aui:cube"), ("👥 Клиенты", "aui:users")],
                [("📣 Рассылка", "broadcast"), ("📊 Статистика", "stats")],
                [("🐾 Мои настройки", "aui:settings")],
                [("🏠 Главная", "home")],
            ],
        )

    async def start_form(self, context, uid, purpose, fields, meta=None):
        self.admin(uid)
        data = {
            "purpose": purpose,
            "fields": fields,
            "index": 0,
            "values": {},
            "meta": meta or {},
            "nonce": str(time.time_ns())[-12:],
        }
        self.s.save_dialog(uid, "adminform", data)
        await self.form_question(context, uid, data)

    async def form_question(self, context, uid, data):
        f = data["fields"][data["index"]]
        choices = f.get("choices") or []
        rows = [[(label, f"av:{data['nonce']}:{data['index']}:{i}")] for i, (label, _) in enumerate(choices)]
        if f["kind"] in {"optional_int", "image"}:
            rows.append([("Пропустить", f"av:{data['nonce']}:{data['index']}:skip")])
        rows += [[("Отменить", "admin")]]
        await self.tell(
            context, uid, f"{f['prompt']}\n\nШаг {data['index'] + 1} из {len(data['fields'])}", rows
        )

    async def form_value(self, context, uid, value, msg=None):
        self.admin(uid)
        dialog = self.s.dialog(uid)
        if not dialog or dialog[0] != "adminform":
            raise RuleError("Настройка уже сохранена или отменена.")
        data = dialog[1]
        f = data["fields"][data["index"]]
        if f["kind"] == "image":
            value = self.media_of(msg) if msg else None
            if value and value["kind"] == "document" and not value.get("mime", "").startswith("image/"):
                raise RuleError("Пришлите изображение PNG/JPEG/WebP.")
            if msg and not value:
                raise RuleError("Пришлите картинку или нажмите «Пропустить».")
        elif f.get("choices"):
            if value not in [v for _, v in f["choices"]]:
                raise RuleError("Выберите вариант кнопкой ниже.")
        elif f["kind"] in {"int", "optional_int"}:
            value = positive(value) if value is not None else None
            if value is None and f["kind"] == "int":
                raise RuleError("Пришлите число больше нуля.")
        elif f["kind"] == "percent":
            value = positive(value)
            if value > 100:
                raise RuleError("Скидка: от 1 до 100%.")
        elif f["kind"] == "rate":
            try:
                number = Decimal(str(value).replace(",", "."))
            except InvalidOperation:
                raise RuleError("Пришлите положительное число, например 95,50.") from None
            if not number.is_finite() or number <= 0:
                raise RuleError("Курс должен быть больше нуля.")
            value = str(number)
        else:
            value = str(value or "").strip()
            if not value or len(value) > 2500:
                raise RuleError("Пришлите текст от 1 до 2500 символов.")
        if f["key"] == "percent" and value > 100:
            raise RuleError("Скидка: от 1 до 100%.")
        if f["key"] == "username":
            value = value.lstrip("@")
            if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
                raise RuleError("Введите Telegram username, например @TibrisArtist.")
        data["values"][f["key"]] = value
        data["index"] += 1
        if data["index"] < len(data["fields"]):
            self.s.save_dialog(uid, "adminform", data)
            await self.form_question(context, uid, data)
        else:
            await self.finish_form(context, uid, data)
            self.s.clear_dialog(uid)

    async def finish_form(self, context, uid, data):
        values, meta, purpose = data["values"], data["meta"], data["purpose"]
        if purpose == "price":
            with self.s.db(True) as c:
                c.execute(
                    "UPDATE art_products SET rub=?,stars=?,points=? WHERE slug=?",
                    (values["rub"], values["stars"], values["points"], meta["slug"]),
                )
        elif purpose == "description":
            with self.s.db(True) as c:
                c.execute(
                    "UPDATE art_products SET description=? WHERE slug=?", (values["text"], meta["slug"])
                )
        elif purpose == "background":
            self.s.set_setting("background_rub", str(values["rub"]))
            self.s.set_setting("background_stars", str(values["stars"] or 0))
        elif purpose == "characters":
            if values["extra_percent"] > 500:
                raise RuleError("Доплата: от 1 до 500% базовой цены.")
            self.s.set_setting("extra_character_percent", str(values["extra_percent"]))
        elif purpose in {"image", "portfolio"}:
            image = values["image"]
            if image:
                self.s.set_media(meta["key"], image["file_id"], image["kind"])
        elif purpose == "text":
            self.s.set_setting("text:" + meta["key"], values["text"])
        elif purpose == "setting":
            self.s.set_setting(meta["key"], values.get("username", values.get("text")))
        elif purpose == "promo":
            self.s.add_promo(values["code"], values["percent"], values["limit"], values["days"])
        elif purpose == "reward":
            self.s.reward(values["name"], values["price"], values["percent"])
        elif purpose == "method":
            self.s.set_setting(
                "method:" + meta["key"],
                json.dumps(
                    {
                        "label": values["label"],
                        "currency": meta["currency"],
                        "rate": values.get("rate", "1"),
                        "provider": values["provider"],
                        "instructions": values["instructions"],
                    },
                    ensure_ascii=False,
                ),
            )
        elif purpose == "riddle":
            image = values["image"] or {}
            self.s.add_riddle(
                values["text"],
                values["answer"],
                values["reward"],
                values["limit"],
                image.get("file_id", ""),
                image.get("kind", "photo"),
            )
        elif purpose == "item":
            image = values["image"] or {}
            self.s.add_item(
                values["name"],
                values["price"],
                values["description"],
                values["limit"],
                values["user_limit"],
                values["manual"],
                values["content"],
                image.get("file_id", ""),
                image.get("kind", "photo"),
            )
        elif purpose == "task":
            self.s.add_task(
                values["link"],
                values["description"],
                values["reward"],
                meta["type"],
                values["limit"],
                values.get("delay", 0),
                values.get("code", ""),
            )
        elif purpose == "quote":
            order = self.s.order(meta["oid"])
            if meta.get("accept"):
                rub = order["base_rub"] or order["rub"]
                stars = order["base_stars"]
            else:
                rub, stars = values["rub"], values["stars"]
            points = None
            if order["preferred_method"] == "points":
                product = self.s.one("SELECT points FROM art_products WHERE slug=?", (order["slug"],))
                options = json.loads(order["options"])
                if options.get("characters", 1) != 1 or options.get("background"):
                    raise RuleError("Арт с дополнениями нельзя оплатить базовым количеством баллов.")
                points = product["points"]
                if not points:
                    raise RuleError("Цена в баллах больше не установлена. Измените предложение.")
            o = self.s.quote(meta["oid"], rub, stars, values["note"], points)
            await self.order_card(context, o["user_id"], o["id"])
        elif purpose == "markpaid":
            self.s.confirm_external(meta["oid"], values["reference"])
            await self.payment_notice(context, meta["oid"])
        elif purpose == "refundexternal":
            self.s.refund_external(meta["oid"], values["reference"])
            await self.notify(
                context,
                self.s.order(meta["oid"])["user_id"],
                f"Возврат по заказу #{meta['oid']} отмечен Tibris.",
            )
        elif purpose == "points":
            target, balance = self.s.adjust(meta["uid"], values["amount"], meta["mode"])
            await self.notify(context, target, f"🐾 Tibris обновил ваш баланс: {balance} баллов Cube.")
        elif purpose == "cubedeliver":
            purchase = self.s.one("SELECT * FROM purchases WHERE id=? AND delivered=0", (meta["pid"],))
            if not purchase:
                raise RuleError("Эта покупка уже выдана.")
            await self.tell(
                context, purchase["user_id"], f"🎁 Ваш товар Cube #{meta['pid']}\n{values['text']}"
            )
            with self.s.db(True) as c:
                c.execute(
                    "UPDATE purchases SET delivered=1,content=? WHERE id=?", (values["text"], meta["pid"])
                )
        await self.tell(
            context,
            uid,
            "Готово 🐾 Изменения сохранены.",
            [[("← К заказу", f"order:{meta['oid']}")]] if "oid" in meta else BACK,
        )

    async def admin_callback(self, context, uid, parts):
        self.admin(uid)
        action = parts[0]
        if action == "av":
            dialog = self.s.dialog(uid)
            if (
                not dialog
                or dialog[0] != "adminform"
                or dialog[1]["nonce"] != parts[1]
                or dialog[1]["index"] != int(parts[2])
            ):
                raise RuleError("Этот шаг уже пройден. Используйте последние кнопки.")
            f = dialog[1]["fields"][dialog[1]["index"]]
            if parts[3] == "skip":
                if f["kind"] not in {"image", "optional_int"}:
                    raise RuleError("Этот шаг нужно заполнить.")
                value = None
            else:
                value = f["choices"][int(parts[3])][1]
            await self.form_value(context, uid, value)
            return
        self.s.clear_dialog(uid)
        page = parts[1]
        arg = parts[2] if len(parts) > 2 else None
        if page == "catalog":
            products = self.s.rows(
                "SELECT * FROM art_products WHERE category IN ('classic','light','minecraft') AND slug NOT IN ('light_logo','light_background','mc_4d') ORDER BY category,rub"
            )
            rows = [
                [(f"{'🟢' if p['active'] else '🔴'} {p['title']} · {p['rub']} ₽", f"aui:product:{p['slug']}")]
                for p in products
            ]
            await self.tell(
                context,
                uid,
                "🎨 Каталог\nВыберите формат: здесь можно менять цену, описание, картинку и доступность.",
                rows
                + [
                    [("🌄 Цена подробного фона", "aui:background")],
                    [("🐾 Доплата за персонажа", "aui:characters")],
                ]
                + BACK,
            )
        elif page == "product":
            p = self.s.one(
                "SELECT * FROM art_products WHERE slug=? AND category IN ('classic','light','minecraft')",
                (arg,),
            )
            if not p or p["slug"] in {"light_logo", "light_background", "mc_4d"}:
                raise RuleError("Формат больше не используется.")
            await self.tell(
                context,
                uid,
                f"🎨 {p['title']}\n{p['rub']} ₽ · {p['stars'] or '—'} ⭐ · {p['points'] or '—'} баллов\n\n{p['description']}",
                [
                    [("💰 Изменить цены", f"aui:price:{arg}"), ("📝 Описание", f"aui:description:{arg}")],
                    [("🖼 Картинка", f"aui:cover:product:{arg}"), ("🗓 Слоты", f"slotadmin:{arg}")],
                    [("Скрыть формат" if p["active"] else "Показать формат", f"aui:toggleproduct:{arg}")],
                    [("← Каталог", "aui:catalog")],
                ],
            )
        elif page == "toggleproduct":
            with self.s.db(True) as c:
                c.execute(
                    "UPDATE art_products SET active=1-active WHERE slug=? AND category IN ('classic','light','minecraft') AND slug NOT IN ('light_logo','light_background','mc_4d')",
                    (arg,),
                )
            await self.admin_callback(context, uid, ["aui", "product", arg])
        elif page == "price":
            await self.start_form(
                context,
                uid,
                "price",
                [
                    field("rub", "Новая цена в рублях?", "int"),
                    field(
                        "stars",
                        "Цена в Stars? Пропустите, чтобы отключить Stars для этого формата.",
                        "optional_int",
                    ),
                    field(
                        "points",
                        "Цена в баллах Cube? Пропустите, если формат не продаётся за баллы.",
                        "optional_int",
                    ),
                ],
                {"slug": arg},
            )
        elif page == "description":
            await self.start_form(
                context,
                uid,
                "description",
                [field("text", "Пришлите новое описание формата.")],
                {"slug": arg},
            )
        elif page == "background":
            await self.start_form(
                context,
                uid,
                "background",
                [
                    field(
                        "rub",
                        "Сколько стоит подробный фон? Сейчас: "
                        + self.s.setting("background_rub", "400")
                        + " ₽",
                        "int",
                    ),
                    field(
                        "stars",
                        "Доплата за фон в Stars? Пропустите, чтобы не предлагать Stars для заказов с фоном.",
                        "optional_int",
                    ),
                ],
            )
        elif page == "characters":
            await self.start_form(
                context,
                uid,
                "characters",
                [
                    field(
                        "extra_percent",
                        "Доплата за второго и третьего персонажа, в процентах базовой цены? Сейчас: "
                        + self.s.setting("extra_character_percent", "100")
                        + "%. Максимум 500%.",
                        "int",
                    )
                ],
            )
        elif page == "content":
            await self.tell(
                context,
                uid,
                "🖼 Оформление\nКартинки отправляйте прямо сюда: фото или файлом.",
                [
                    [("🖼 Обложки разделов", "aui:covers"), ("📝 Тексты", "aui:texts")],
                    [("🎨 Примеры работ", "aui:portfolio")],
                ]
                + BACK,
            )
        elif page == "covers":
            await self.tell(
                context,
                uid,
                "Выберите раздел для картинки.",
                [[(name, f"aui:cover:{key}")] for key, name in SECTIONS.items()] + BACK,
            )
        elif page == "cover":
            key = ":".join(parts[2:])
            await self.start_form(
                context,
                uid,
                "image",
                [field("image", "Пришлите картинку для выбранного раздела.", "image")],
                {"key": key},
            )
        elif page == "texts":
            await self.tell(
                context,
                uid,
                "Какой текст изменим?",
                [[(name, f"aui:text:{key}")] for key, name in TEXTS.items()] + BACK,
            )
        elif page == "text":
            await self.start_form(
                context, uid, "text", [field("text", "Пришлите новый текст раздела.")], {"key": arg}
            )
        elif page == "portfolio":
            await self.tell(
                context,
                uid,
                "В какой раздел добавить пример работы?",
                [[(name, f"aui:portfolioadd:{key}")] for key, (name, _) in CATEGORIES.items()]
                + [[("🗑 Удалить пример", "aui:portfoliolist")]]
                + BACK,
            )
        elif page == "portfolioadd":
            if arg not in CATEGORIES:
                raise RuleError("Раздел не найден.")
            await self.start_form(
                context,
                uid,
                "portfolio",
                [field("image", "Пришлите свою работу фото или файлом.", "image")],
                {"key": f"portfolio:{arg}:{time.time_ns()}"},
            )
        elif page == "portfoliolist":
            media = self.s.rows("SELECT key FROM media WHERE key LIKE 'portfolio:%' ORDER BY key")
            await self.list_page(
                context,
                uid,
                "Выберите пример для просмотра и удаления.",
                [(m["key"], f"aui:portfoliosee:{m['key'][10:]}") for m in media],
                int(arg or 0),
                "aui:portfoliolist",
                "aui:content",
            )
        elif page == "portfoliosee":
            key = "portfolio:" + ":".join(parts[2:])
            image = self.s.one("SELECT * FROM media WHERE key=?", (key,))
            await self.tell(
                context,
                uid,
                "Удалить этот пример из портфолио?",
                [[("🗑 Удалить", "aui:portfoliodelete:" + key[10:])]] + BACK,
                media=image,
            )
        elif page == "portfoliodelete":
            with self.s.db(True) as c:
                c.execute("DELETE FROM media WHERE key=?", ("portfolio:" + ":".join(parts[2:]),))
            await self.tell(context, uid, "Пример удалён.", BACK)
        elif page == "settings":
            await self.tell(
                context,
                uid,
                "🐾 Ваш профиль и приём заявок",
                [
                    [("Имя художника", "aui:setting:artist_name")],
                    [("Telegram для связи", "aui:setting:artist_username")],
                    [("Ссылка на портфолио", "aui:setting:portfolio_url")],
                    [("Открыть / закрыть приём", "aui:commissions")],
                ]
                + BACK,
            )
        elif page == "setting":
            if arg not in {"artist_name", "artist_username", "portfolio_url"}:
                raise RuleError("Настройка не найдена.")
            await self.start_form(
                context,
                uid,
                "setting",
                [field("username" if arg == "artist_username" else "text", "Пришлите новое значение.")],
                {"key": arg},
            )
        elif page == "commissions":
            self.s.set_setting(
                "commissions_open", "0" if self.s.setting("commissions_open", "1") == "1" else "1"
            )
            await self.admin_home(context, uid)
        elif page == "promos":
            promos = self.s.rows(
                "SELECT p.rowid pid,p.*,COUNT(c.user_id) used FROM promos p LEFT JOIN promo_claims c ON c.code=p.code GROUP BY p.code"
            )
            await self.tell(
                context,
                uid,
                "🎟 Промокоды\n"
                + (
                    "\n".join(
                        f"{p['code']} · −{p['percent']}% · активаций {p['used']}/{p['total_limit'] or '∞'}"
                        for p in promos
                    )
                    or "Пока нет кодов."
                ),
                [[("＋ Создать промокод", "aui:promonew")]]
                + [[(f"Закрыть {p['code']}", f"aui:promooff:{p['pid']}")] for p in promos if p["active"]]
                + BACK,
            )
        elif page == "promonew":
            await self.start_form(
                context,
                uid,
                "promo",
                [
                    field("code", "Текст промокода? Например TIBRIS10."),
                    field("percent", "Скидка в процентах?", "percent"),
                    field("limit", "Общий лимит активаций? Пропустить — без лимита.", "optional_int"),
                    field("days", "Срок действия в днях? Пропустить — бессрочно.", "optional_int"),
                ],
            )
        elif page == "promooff":
            with self.s.db(True) as c:
                c.execute("UPDATE promos SET active=0 WHERE rowid=?", (int(arg),))
            await self.tell(context, uid, "Промокод закрыт. Выданные купоны сохраняются.", BACK)
        elif page == "payments":
            methods = self.s.rows("SELECT key,value FROM settings WHERE key LIKE 'method:%'")
            await self.tell(
                context,
                uid,
                "💳 Способы оплаты\nКлиенты видят только подключённые способы с заданным курсом. Ключи автоматической оплаты задаются в переменных окружения хостинга.",
                [
                    [(json.loads(m["value"]).get("label", m["key"][7:]), f"aui:method:{m['key'][7:]}")]
                    for m in methods
                ]
                + [[("＋ Добавить способ", "aui:methodnew")], [("🔎 Автоматические счета", "aui:autostatus")]]
                + BACK,
            )
        elif page == "methodnew":
            await self.tell(
                context,
                uid,
                "Какой способ добавим?",
                [
                    [("СБП · рубли", "aui:methodsetup:sbp:RUB")],
                    [("ЮMoney · рубли", "aui:methodsetup:yoomoney:RUB")],
                    [("Hipolink · доллары", "aui:methodsetup:hipolink:USD")],
                    [("TON", "aui:methodsetup:ton:TON"), ("USDT", "aui:methodsetup:usdt:USDT")],
                ]
                + BACK,
            )
        elif page == "method":
            raw = self.s.setting("method:" + arg)
            if not raw:
                raise RuleError("Способ не найден.")
            m = json.loads(raw)
            await self.tell(
                context,
                uid,
                f"{m.get('label', arg)} · {m['currency']}\nПодтверждение: {m.get('provider', 'manual')}\nКурс: {m.get('rate', 'не задан')} ₽ за единицу\n\n{m['instructions']}",
                [
                    [("Изменить настройки", f"aui:methodsetup:{arg}:{m['currency']}")],
                    [("🖼 Картинка / QR", f"aui:cover:payment:{arg}")],
                    [("Отключить способ", f"aui:methodoff:{arg}")],
                ]
                + BACK,
            )
        elif page == "methodsetup":
            if arg not in {"sbp", "yoomoney", "hipolink", "ton", "usdt"}:
                raise RuleError("Способ не найден.")
            currency = parts[3]
            providers = [("Проверяю поступление сам", "manual")]
            if arg == "yoomoney":
                providers.append(("Автоматически · API ЮMoney", "yoomoney"))
            elif arg in {"ton", "usdt"}:
                providers.append(("Автоматически · Crypto Pay", "cryptopay"))
            fields = [
                field("label", "Название кнопки для клиента? Например «СБП» или «Карта РФ»."),
                field("provider", "Как подтверждать оплату?", choices=providers),
            ]
            if currency != "RUB":
                fields.append(
                    field(
                        "rate", f"Сколько рублей стоит 1 {currency}? Это ваш курс для расчёта цены.", "rate"
                    )
                )
            fields.append(
                field(
                    "instructions",
                    "Пришлите реквизиты / ссылку и инструкцию. Для криптовалюты укажите сеть. Для автоматического способа напишите краткую подсказку клиенту.",
                )
            )
            await self.start_form(context, uid, "method", fields, {"key": arg, "currency": currency})
        elif page == "methodoff":
            with self.s.db(True) as c:
                c.execute("DELETE FROM settings WHERE key=?", ("method:" + arg,))
            await self.tell(
                context, uid, "Способ отключён для новых оплат. Старые предложения сохранены.", BACK
            )
        elif page == "autostatus":
            invoices = self.s.rows(
                "SELECT order_id,provider,state FROM auto_invoices ORDER BY id DESC LIMIT 30"
            )
            await self.tell(
                context,
                uid,
                "Автоматические счета\n"
                + (
                    "\n".join(f"#{i['order_id']} · {i['provider']} · {i['state']}" for i in invoices)
                    or "Счетов пока нет."
                ),
                [[(f"Заказ #{i['order_id']}", f"order:{i['order_id']}")] for i in invoices] + BACK,
            )
        elif page == "cube":
            await self.tell(
                context,
                uid,
                "🎲 Digital Cube\nСоздавайте задания и награды — всё по одному вопросу за шаг.",
                [
                    [("🧩 Загадки", "aui:list:riddles:0"), ("📋 Задания", "aui:list:subscriptions:0")],
                    [("🎁 Магазин", "aui:list:shop_items:0"), ("🎟 Бонусы", "aui:list:rewards:0")],
                    [("📦 Выдать покупку", "aui:pending")],
                ]
                + BACK,
            )
        elif page == "list":
            if arg not in TABLES:
                raise RuleError("Раздел не найден.")
            name, label, active = TABLES[arg]
            records = self.s.rows(f"SELECT * FROM {arg} WHERE {active}=1 ORDER BY id DESC")
            entries = [(str(r[label])[:42], f"aui:record:{arg}:{r['id']}") for r in records]
            entries.insert(0, ("＋ Добавить", f"aui:new:{arg}"))
            await self.list_page(context, uid, name, entries, int(parts[3]), f"aui:list:{arg}", "aui:cube")
        elif page == "record":
            if arg not in TABLES:
                raise RuleError("Раздел не найден.")
            _, label, _ = TABLES[arg]
            record = self.s.one(f"SELECT * FROM {arg} WHERE id=?", (int(parts[3]),))
            if not record:
                raise RuleError("Запись не найдена.")
            rows = [[("🗑 Убрать из каталога", f"aui:remove:{arg}:{record['id']}")]]
            if arg in {"riddles", "shop_items"}:
                key = "riddle" if arg == "riddles" else "item"
                rows.insert(0, [("🖼 Изменить картинку", f"aui:legacycover:{key}:{record['id']}")])
            names = {
                "points_reward": "Награда, баллы",
                "price": "Цена, баллы",
                "percent": "Скидка, %",
                "total_limit": "Общий лимит",
                "user_limit": "Лимит на человека",
                "description": "Описание",
                "answer": "Ответ",
                "secret_code": "Код",
                "channel_username": "Канал / ссылка",
                "delay_seconds": "Ожидание, секунды",
            }
            await self.tell(
                context,
                uid,
                f"#{record['id']} · {record[label]}\n\n"
                + "\n".join(
                    f"{names[k]}: {v if v is not None else 'без лимита'}"
                    for k, v in record.items()
                    if k in names
                ),
                rows + BACK,
            )
        elif page == "legacycover":
            if arg not in {"riddle", "item"}:
                raise RuleError("Раздел не найден.")
            self.s.save_dialog(uid, "image", {"key": f"{arg}:{int(parts[3])}"})
            await self.tell(context, uid, "Пришлите картинку фото или файлом.", BACK)
        elif page == "remove":
            if arg not in TABLES:
                raise RuleError("Раздел не найден.")
            self.s.deactivate(arg, int(parts[3]))
            await self.tell(context, uid, "Запись скрыта. История сохранена.", BACK)
        elif page == "new":
            await self.new_cube_form(context, uid, arg)
        elif page == "tasktype":
            await self.new_task_form(context, uid, arg)
        elif page == "users":
            users = self.s.rows("SELECT * FROM users ORDER BY user_id")
            await self.list_page(
                context,
                uid,
                "👥 Клиенты",
                [
                    (f"@{u['username'] or u['user_id']} · {u['points']} баллов", f"aui:user:{u['user_id']}")
                    for u in users
                ],
                int(arg or 0),
                "aui:users",
                "admin",
            )
        elif page == "user":
            user = self.s.one("SELECT * FROM users WHERE user_id=?", (int(arg),))
            if not user:
                raise RuleError("Клиент не найден.")
            await self.tell(
                context,
                uid,
                f"👤 @{user['username'] or user['user_id']}\nБаланс: {user['points']} баллов Cube",
                [
                    [("💬 Написать", f"aui:reply:{arg}")],
                    [("＋ Начислить", f"aui:points:{arg}:add"), ("− Списать", f"aui:points:{arg}:remove")],
                    [("← Клиенты", "aui:users")],
                ],
            )
        elif page == "points":
            if parts[3] not in {"add", "remove"}:
                raise RuleError("Действие не найдено.")
            await self.start_form(
                context,
                uid,
                "points",
                [field("amount", "Сколько баллов?", "int")],
                {"uid": int(arg), "mode": parts[3]},
            )
        elif page == "reply":
            self.s.save_dialog(uid, "send", {"uid": int(arg)})
            await self.tell(context, uid, "Пришлите текст, фото или файл — я отправлю его клиенту.", BACK)
        elif page in {"accept", "quote"}:
            order = self.s.order(int(arg))
            fields = (
                []
                if page == "accept" and order["base_rub"]
                else [
                    field("rub", "Стоимость до скидки, в рублях?", "int"),
                    field("stars", "Стоимость до скидки в Stars? Пропустить — без Stars.", "optional_int"),
                ]
            )
            fields.append(
                field(
                    "note",
                    "Напишите срок и условия: например «Готовность до 25 октября, две правки на этапе скетча».",
                )
            )
            await self.start_form(
                context,
                uid,
                "quote",
                fields,
                {"oid": int(arg), "accept": page == "accept" and bool(order["base_rub"])},
            )
        elif page == "markpaid":
            await self.start_form(
                context,
                uid,
                "markpaid",
                [
                    field(
                        "reference",
                        "Сверьте поступление в банке/кошельке. Только после проверки пришлите уникальный номер операции.",
                    )
                ],
                {"oid": int(arg)},
            )
        elif page == "refund":
            order = self.s.order(int(arg))
            await self.tell(
                context,
                uid,
                f"Вернуть оплату по заказу #{arg}?\n"
                + (
                    "Сначала выполните возврат во внешнем сервисе; здесь можно записать его номер."
                    if order["payment_method"]
                    else "После подтверждения Stars или баллы вернутся клиенту."
                ),
                [[("Подтвердить возврат", f"aui:refundconfirm:{arg}")], [("← Заказ", f"order:{arg}")]],
            )
        elif page == "refundconfirm":
            order = self.s.order(int(arg))
            if order["status"] not in {"paid", "working", "delivered"}:
                raise RuleError("Заказ не оплачен или уже возвращён.")
            if order["payment_method"]:
                await self.start_form(
                    context,
                    uid,
                    "refundexternal",
                    [
                        field(
                            "reference",
                            "Номер уже выполненного внешнего возврата? Бот записывает его, деньги возвращаются вами в сервисе.",
                        )
                    ],
                    {"oid": int(arg)},
                )
            else:
                if order["charge"]:
                    await context.bot.refund_star_payment(order["user_id"], order["charge"])
                    self.s.mark_refund(order["charge"])
                else:
                    self.s.refund_points(int(arg))
                await self.notify(context, order["user_id"], f"Возврат по заказу #{arg} выполнен.")
                await self.order_card(context, uid, int(arg))
        elif page == "void":
            await self.tell(
                context,
                uid,
                f"Отменить заказ #{arg} с выставленным внешним счётом? Поздний платёж попадёт на ручную проверку.",
                [[("Да, отменить", f"aui:voidconfirm:{arg}")], [("← Заказ", f"order:{arg}")]],
            )
        elif page == "voidconfirm":
            self.s.void_auto(int(arg))
            self.s.cancel_order(uid, int(arg), admin=True)
            order = self.s.order(int(arg))
            await self.notify(
                context,
                order["user_id"],
                f"Заказ #{arg} отменён. Не оплачивайте старую ссылку. Если уже оплатили, напишите Tibris.",
            )
            await self.order_card(context, uid, int(arg))
        elif page == "pending":
            purchases = self.s.rows(
                "SELECT p.id,i.name FROM purchases p JOIN shop_items i ON i.id=p.item_id WHERE p.delivered=0"
            )
            await self.tell(
                context,
                uid,
                "📦 Покупки Cube для выдачи",
                [[(f"#{p['id']} · {p['name']}", f"aui:cubedeliver:{p['id']}")] for p in purchases] + BACK,
            )
        elif page == "cubedeliver":
            await self.start_form(
                context,
                uid,
                "cubedeliver",
                [field("text", "Пришлите содержимое или инструкцию для клиента.")],
                {"pid": int(arg)},
            )
        else:
            raise RuleError("Кнопка устарела. Откройте Управление.")

    async def new_cube_form(self, context, uid, table):
        if table == "riddles":
            await self.start_form(
                context,
                uid,
                "riddle",
                [
                    field("text", "Текст загадки?"),
                    field("answer", "Правильный ответ?"),
                    field("reward", "Награда в баллах?", "int"),
                    field(
                        "limit",
                        "Сколько пользователей могут решить? Пропустить — без лимита.",
                        "optional_int",
                    ),
                    field("image", "Картинка загадки?", "image"),
                ],
            )
        elif table == "shop_items":
            await self.start_form(
                context,
                uid,
                "item",
                [
                    field("name", "Название товара?"),
                    field("price", "Цена в баллах?", "int"),
                    field("description", "Описание для покупателя?"),
                    field("limit", "Общее число покупок?", "optional_int"),
                    field("user_limit", "Лимит на одного человека?", "optional_int"),
                    field(
                        "manual", "Как выдавать товар?", choices=[("Вручную", True), ("Автоматически", False)]
                    ),
                    field("content", "Содержимое для автоматической выдачи или инструкция для ручной?"),
                    field("image", "Картинка товара?", "image"),
                ],
            )
        elif table == "rewards":
            await self.start_form(
                context,
                uid,
                "reward",
                [
                    field("name", "Название бонуса?"),
                    field("price", "Стоимость в баллах?", "int"),
                    field("percent", "Размер скидки в процентах?", "percent"),
                ],
            )
        elif table == "subscriptions":
            await self.tell(
                context,
                uid,
                "Какое задание создаём?",
                [
                    [("Подписка на канал", "aui:tasktype:subscription")],
                    [("Посещение сайта", "aui:tasktype:external")],
                    [("Секретный код", "aui:tasktype:code")],
                ]
                + BACK,
            )
        else:
            raise RuleError("Раздел не найден.")

    async def new_task_form(self, context, uid, kind):
        if kind not in {"subscription", "external", "code"}:
            raise RuleError("Тип задания не найден.")
        fields = [
            field("link", "Username канала с @?" if kind == "subscription" else "Ссылка на сайт?"),
            field("description", "Описание задания?"),
            field("reward", "Награда в баллах?", "int"),
            field("limit", "Общий лимит выполнений?", "optional_int"),
        ]
        if kind == "external":
            fields.append(field("delay", "Сколько секунд ждать на сайте?", "int"))
        elif kind == "code":
            fields.append(field("code", "Какой секретный код должен ввести пользователь?"))
        await self.start_form(context, uid, "task", fields, {"type": kind})
