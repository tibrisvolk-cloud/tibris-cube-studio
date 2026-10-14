"""SQLite domain layer. Every balance/limit/payment mutation is one transaction."""

import hmac
import json
import math
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP
from pathlib import Path

from .catalog import CATALOG


class RuleError(ValueError):
    pass


def positive(value):
    value = int(value)
    if value <= 0:
        raise RuleError("Значение должно быть больше нуля.")
    return value


def timestamp(value):
    if not value:
        return 0
    try:
        return float(value)
    except (TypeError, ValueError):
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp()


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def db(self, write=False):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            if write:
                con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def init(self):
        with self.db() as c:
            c.execute("PRAGMA journal_mode=WAL")
        with self.db(True) as c:
            schemas = [
                "users(user_id INTEGER PRIMARY KEY, username TEXT, points INTEGER DEFAULT 0)",
                "riddles(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, answer TEXT NOT NULL, "
                "points_reward INTEGER DEFAULT 10, total_limit INTEGER, image TEXT DEFAULT '', is_active INTEGER DEFAULT 1)",
                "user_riddle_attempts(user_id INTEGER, riddle_id INTEGER, first_attempt_time TEXT, "
                "attempts_count INTEGER DEFAULT 0, solved INTEGER DEFAULT 0, PRIMARY KEY(user_id,riddle_id))",
                "subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT, channel_username TEXT NOT NULL, description TEXT, "
                "points_reward INTEGER DEFAULT 5, task_type TEXT DEFAULT 'subscription', delay_seconds INTEGER DEFAULT 0, "
                "secret_code TEXT DEFAULT '', total_limit INTEGER, is_active INTEGER DEFAULT 1)",
                "user_subscriptions(user_id INTEGER, subscription_id INTEGER, completed INTEGER DEFAULT 0, "
                "PRIMARY KEY(user_id,subscription_id))",
                "shop_items(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT DEFAULT '', "
                "price INTEGER NOT NULL, total_limit INTEGER, user_limit INTEGER, content TEXT DEFAULT '', "
                "manual_delivery INTEGER DEFAULT 0, image TEXT DEFAULT '', is_active INTEGER DEFAULT 1)",
                "purchases(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, item_id INTEGER NOT NULL, "
                "content TEXT DEFAULT '', delivered INTEGER DEFAULT 0, purchased_at TEXT DEFAULT CURRENT_TIMESTAMP)",
                "task_starts(user_id INTEGER, task_id INTEGER, started_at REAL NOT NULL, PRIMARY KEY(user_id,task_id))",
                "settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                "media(key TEXT PRIMARY KEY, file_id TEXT NOT NULL, kind TEXT DEFAULT 'photo')",
                "dialogs(user_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL, expires_at REAL NOT NULL)",
                "ledger(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, amount INTEGER NOT NULL, "
                "reason TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
                "art_products(slug TEXT PRIMARY KEY, category TEXT NOT NULL, title TEXT NOT NULL, rub INTEGER NOT NULL, "
                "price_from INTEGER DEFAULT 0, description TEXT NOT NULL, stars INTEGER, points INTEGER, active INTEGER DEFAULT 1)",
                "rewards(id INTEGER PRIMARY KEY, name TEXT NOT NULL, price INTEGER NOT NULL, percent INTEGER NOT NULL, active INTEGER DEFAULT 1)",
                "coupons(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, reward_id INTEGER NOT NULL, percent INTEGER NOT NULL, "
                "state TEXT DEFAULT 'available')",
                "orders(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, slug TEXT NOT NULL, brief TEXT NOT NULL, "
                "refs TEXT NOT NULL, status TEXT DEFAULT 'requested', coupon_id INTEGER UNIQUE, discount INTEGER DEFAULT 0, "
                "rub INTEGER, stars INTEGER, points INTEGER, note TEXT DEFAULT '', invoice TEXT UNIQUE, "
                "checkout_until REAL DEFAULT 0, charge TEXT UNIQUE, result TEXT DEFAULT '', "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)",
                "payments(charge TEXT PRIMARY KEY, order_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                "amount INTEGER NOT NULL, state TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
                "external_payments(order_id INTEGER PRIMARY KEY, method TEXT NOT NULL, reference TEXT NOT NULL, "
                "refunded INTEGER DEFAULT 0, refund_reference TEXT DEFAULT '', UNIQUE(method,reference))",
                "auto_invoices(id INTEGER PRIMARY KEY, order_id INTEGER UNIQUE NOT NULL, method TEXT NOT NULL, "
                "provider TEXT NOT NULL, nonce TEXT UNIQUE NOT NULL, remote_id TEXT, amount TEXT NOT NULL, "
                "currency TEXT NOT NULL, gross TEXT DEFAULT '', url TEXT DEFAULT '', state TEXT DEFAULT 'creating', "
                "created_at REAL NOT NULL, UNIQUE(provider,remote_id))",
                "slots(id INTEGER PRIMARY KEY, slug TEXT NOT NULL, title TEXT NOT NULL, active INTEGER DEFAULT 1)",
                "slot_holds(slot_id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL, expires_at REAL NOT NULL)",
                "ych(id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL)",
                "promos(code TEXT PRIMARY KEY, percent INTEGER NOT NULL, total_limit INTEGER, expires_at REAL, active INTEGER DEFAULT 1)",
                "promo_claims(code TEXT NOT NULL, user_id INTEGER NOT NULL, coupon_id INTEGER NOT NULL, PRIMARY KEY(code,user_id))",
                "auto_review(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, provider TEXT NOT NULL, reference TEXT NOT NULL, "
                "reason TEXT NOT NULL, resolved INTEGER DEFAULT 0, UNIQUE(provider,reference))",
            ]
            for schema in schemas:
                c.execute("CREATE TABLE IF NOT EXISTS " + schema)
            # Inspect each column separately: an existing column must never skip the next migration.
            migrations = {
                "riddles": {
                    "total_limit": "INTEGER",
                    "image": "TEXT DEFAULT ''",
                    "image_kind": "TEXT DEFAULT 'photo'",
                },
                "subscriptions": {
                    "total_limit": "INTEGER",
                    "task_type": "TEXT DEFAULT 'subscription'",
                    "delay_seconds": "INTEGER DEFAULT 0",
                    "secret_code": "TEXT DEFAULT ''",
                },
                "shop_items": {
                    "total_limit": "INTEGER",
                    "user_limit": "INTEGER",
                    "content": "TEXT DEFAULT ''",
                    "manual_delivery": "INTEGER DEFAULT 0",
                    "image": "TEXT DEFAULT ''",
                    "image_kind": "TEXT DEFAULT 'photo'",
                },
                "purchases": {"content": "TEXT DEFAULT ''", "delivered": "INTEGER DEFAULT 0"},
                "orders": {
                    "payment_method": "TEXT DEFAULT ''",
                    "external_currency": "TEXT DEFAULT ''",
                    "external_amount": "TEXT DEFAULT ''",
                    "external_instructions": "TEXT DEFAULT ''",
                    "proof": "TEXT DEFAULT ''",
                    "gateway": "TEXT DEFAULT 'manual'",
                    "slot_id": "INTEGER",
                    "slot_label": "TEXT DEFAULT ''",
                    "options": "TEXT DEFAULT '{}'",
                    "base_rub": "INTEGER",
                    "base_stars": "INTEGER",
                    "preferred_method": "TEXT DEFAULT ''",
                },
            }
            for table, columns in migrations.items():
                existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                for name, declaration in columns.items():
                    if name not in existing:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            for product in CATALOG:
                c.execute(
                    "INSERT OR IGNORE INTO art_products(slug,category,title,rub,price_from,description) "
                    "VALUES(?,?,?,?,?,?)",
                    product,
                )
            # Retire formats without deleting orders, payments, uploaded media or slots.
            c.execute(
                "UPDATE art_products SET active=0 WHERE category='ych' OR slug IN ('light_logo','light_background','mc_4d')"
            )
            if not c.execute("SELECT 1 FROM settings WHERE key='simple_catalog_v3'").fetchone():
                chibi = next(p for p in CATALOG if p[0] == "light_chibi")
                c.execute(
                    "UPDATE art_products SET title=?,description=? WHERE slug='light_chibi'",
                    (chibi[2], chibi[5]),
                )
                # Old unfinished wizards lack the pricing snapshot used by the new builder.
                # Accepted orders and their files are separate records and stay intact.
                for dialog in c.execute("SELECT user_id,data FROM dialogs WHERE kind='order'").fetchall():
                    if "product" not in json.loads(dialog["data"]):
                        c.execute("DELETE FROM dialogs WHERE user_id=?", (dialog["user_id"],))
                        c.execute("DELETE FROM slot_holds WHERE user_id=?", (dialog["user_id"],))
                c.execute("DELETE FROM dialogs WHERE kind IN ('addriddle','additem','addych')")
                c.execute("INSERT INTO settings VALUES('simple_catalog_v3','1')")
            if not c.execute("SELECT 1 FROM settings WHERE key='slots_initialized'").fetchone():
                for product in CATALOG:
                    for number in range(1, 4):
                        c.execute("INSERT INTO slots(slug,title) VALUES(?,?)", (product[0], f"Слот {number}"))
                c.execute("INSERT INTO settings VALUES('slots_initialized','1')")
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_order_slot ON orders(slot_id) "
                "WHERE status IN ('requested','quoted','paid','working','delivered')"
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id,id)")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_riddle_solved ON user_riddle_attempts(riddle_id,solved)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_completed ON user_subscriptions(subscription_id,completed)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_purchase_item ON purchases(item_id,user_id)")
            c.execute("PRAGMA user_version=3")

    def rows(self, sql, args=()):
        with self.db() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def user(self, uid, username=None):
        with self.db(True) as c:
            c.execute(
                "INSERT INTO users(user_id,username) VALUES(?,?) ON CONFLICT(user_id) "
                "DO UPDATE SET username=excluded.username",
                (uid, username),
            )

    def balance(self, uid):
        r = self.one("SELECT points FROM users WHERE user_id=?", (uid,))
        return r["points"] if r else 0

    def _points(self, c, uid, amount, reason):
        r = c.execute("SELECT points FROM users WHERE user_id=?", (uid,)).fetchone()
        if r is None:
            raise RuleError("Сначала отправьте /start.")
        if amount < 0 and r[0] + amount < 0:
            raise RuleError("Недостаточно баллов.")
        c.execute("UPDATE users SET points=points+? WHERE user_id=?", (amount, uid))
        c.execute("INSERT INTO ledger(user_id,amount,reason) VALUES(?,?,?)", (uid, amount, reason))

    def adjust(self, identifier, amount, mode="add", reason="Администратор"):
        with self.db(True) as c:
            if str(identifier).lstrip("-").isdigit():
                r = c.execute("SELECT * FROM users WHERE user_id=?", (int(identifier),)).fetchone()
            else:
                r = c.execute(
                    "SELECT * FROM users WHERE lower(username)=?", (identifier.lstrip("@").lower(),)
                ).fetchone()
            if not r:
                raise RuleError("Пользователь не найден.")
            amount = int(amount)
            if amount < 0:
                raise RuleError("Укажите неотрицательное количество.")
            delta = amount - r["points"] if mode == "set" else (-amount if mode == "remove" else amount)
            self._points(c, r["user_id"], delta, reason)
            return r["user_id"], r["points"] + delta

    def setting(self, key, fallback=""):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else fallback

    def set_setting(self, key, value):
        with self.db(True) as c:
            c.execute(
                "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def set_media(self, key, file_id, kind="photo"):
        with self.db(True) as c:
            c.execute(
                "INSERT INTO media VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "file_id=excluded.file_id,kind=excluded.kind",
                (key, file_id, kind),
            )

    def dialog(self, uid):
        r = self.one("SELECT * FROM dialogs WHERE user_id=? AND expires_at>?", (uid, time.time()))
        if not r:
            return None
        return r["kind"], json.loads(r["data"])

    def save_dialog(self, uid, kind, data):
        with self.db(True) as c:
            c.execute(
                "INSERT INTO dialogs VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "kind=excluded.kind,data=excluded.data,expires_at=excluded.expires_at",
                (uid, kind, json.dumps(data, ensure_ascii=False), time.time() + 1800),
            )
            if kind == "order" and data.get("slot_id"):
                c.execute(
                    "UPDATE slot_holds SET expires_at=? WHERE slot_id=? AND user_id=? AND expires_at>?",
                    (time.time() + 1800, data["slot_id"], uid, time.time()),
                )

    def clear_dialog(self, uid):
        with self.db(True) as c:
            c.execute("DELETE FROM dialogs WHERE user_id=?", (uid,))
            c.execute("DELETE FROM slot_holds WHERE user_id=?", (uid,))

    def add_riddle(self, text, answer, reward, limit=None, image="", kind="photo"):
        with self.db(True) as c:
            return c.execute(
                "INSERT INTO riddles(text,answer,points_reward,total_limit,image,image_kind) "
                "VALUES(?,?,?,?,?,?)",
                (
                    text,
                    answer.strip().casefold(),
                    positive(reward),
                    positive(limit) if limit else None,
                    image,
                    kind,
                ),
            ).lastrowid

    def riddles(self, uid):
        return self.rows(
            "SELECT * FROM riddles r WHERE is_active=1 AND NOT EXISTS "
            "(SELECT 1 FROM user_riddle_attempts a WHERE a.user_id=? AND a.riddle_id=r.id AND solved=1) "
            "AND (total_limit IS NULL OR (SELECT COUNT(*) FROM user_riddle_attempts a "
            "WHERE a.riddle_id=r.id AND solved=1)<total_limit)",
            (uid,),
        )

    def answer(self, uid, rid, answer, now=None):
        now = time.time() if now is None else now
        with self.db(True) as c:
            r = c.execute("SELECT * FROM riddles WHERE id=? AND is_active=1", (rid,)).fetchone()
            if not r:
                raise RuleError("Загадка недоступна.")
            a = c.execute(
                "SELECT * FROM user_riddle_attempts WHERE user_id=? AND riddle_id=?", (uid, rid)
            ).fetchone()
            if a and a["solved"]:
                raise RuleError("Вы уже решили эту загадку.")
            if r["total_limit"] is not None:
                count = c.execute(
                    "SELECT COUNT(*) FROM user_riddle_attempts WHERE riddle_id=? AND solved=1", (rid,)
                ).fetchone()[0]
                if count >= r["total_limit"]:
                    raise RuleError("Общий лимит загадки исчерпан.")
            started = timestamp(a["first_attempt_time"]) if a else now
            count = a["attempts_count"] if a else 0
            if now - started >= 86400:
                count, started = 0, now
            if count >= 3:
                raise RuleError(
                    f"Попытки исчерпаны. До обновления: {math.ceil((86400 - now + started) / 3600)} ч."
                )
            solved = answer.strip().casefold() == r["answer"].strip().casefold()
            c.execute(
                "INSERT INTO user_riddle_attempts VALUES(?,?,?,?,?) ON CONFLICT(user_id,riddle_id) "
                "DO UPDATE SET first_attempt_time=excluded.first_attempt_time,attempts_count=excluded.attempts_count,"
                "solved=excluded.solved",
                (uid, rid, str(started), count + 1, int(solved)),
            )
            if solved:
                self._points(c, uid, r["points_reward"], f"riddle:{rid}")
            return solved, r["points_reward"], 3 - count - 1

    def add_task(self, url, description, reward, kind, limit=None, delay=0, code=""):
        if kind not in {"subscription", "external", "code"}:
            raise RuleError("Неизвестный тип задания.")
        if kind == "code" and not code.strip():
            raise RuleError("Укажите секретный код.")
        if int(delay) < 0:
            raise RuleError("Задержка не может быть отрицательной.")
        with self.db(True) as c:
            return c.execute(
                "INSERT INTO subscriptions(channel_username,description,points_reward,task_type,"
                "delay_seconds,secret_code,total_limit) VALUES(?,?,?,?,?,?,?)",
                (
                    url,
                    description,
                    positive(reward),
                    kind,
                    int(delay),
                    code.strip(),
                    positive(limit) if limit else None,
                ),
            ).lastrowid

    def tasks(self, uid):
        return self.rows(
            "SELECT s.* FROM subscriptions s WHERE is_active=1 AND NOT EXISTS "
            "(SELECT 1 FROM user_subscriptions u WHERE user_id=? AND subscription_id=s.id AND completed=1) "
            "AND (total_limit IS NULL OR (SELECT COUNT(*) FROM user_subscriptions "
            "WHERE subscription_id=s.id AND completed=1)<total_limit)",
            (uid,),
        )

    def start_task(self, uid, tid):
        with self.db(True) as c:
            r = c.execute(
                "SELECT * FROM subscriptions WHERE id=? AND is_active=1 AND task_type='external'", (tid,)
            ).fetchone()
            if not r:
                raise RuleError("Задание недоступно.")
            c.execute("INSERT OR IGNORE INTO task_starts VALUES(?,?,?)", (uid, tid, time.time()))
            return dict(r)

    def complete_task(self, uid, tid, kind, evidence=None, now=None):
        now = time.time() if now is None else now
        with self.db(True) as c:
            r = c.execute("SELECT * FROM subscriptions WHERE id=? AND is_active=1", (tid,)).fetchone()
            if not r or r["task_type"] != kind:
                raise RuleError("Задание недоступно.")
            done = c.execute(
                "SELECT completed FROM user_subscriptions WHERE user_id=? AND subscription_id=?", (uid, tid)
            ).fetchone()
            if done and done[0]:
                raise RuleError("Вы уже получили награду за это задание.")
            count = c.execute(
                "SELECT COUNT(*) FROM user_subscriptions WHERE subscription_id=? AND completed=1", (tid,)
            ).fetchone()[0]
            if r["total_limit"] is not None and count >= r["total_limit"]:
                raise RuleError("Общий лимит задания исчерпан.")
            if kind == "code" and not hmac.compare_digest(
                str(evidence).strip().casefold().encode(), r["secret_code"].strip().casefold().encode()
            ):
                raise RuleError("Код не подошёл. Попробуйте ещё раз или /cancel.")
            if kind == "subscription" and evidence is not True:
                raise RuleError("Подписка пока не подтверждена.")
            if kind == "external":
                start = c.execute(
                    "SELECT started_at FROM task_starts WHERE user_id=? AND task_id=?", (uid, tid)
                ).fetchone()
                if not start:
                    raise RuleError("Сначала нажмите «Начать».")
                remaining = r["delay_seconds"] - (now - start[0])
                if remaining > 0:
                    raise RuleError(f"Подождите ещё {math.ceil(remaining)} сек.")
            c.execute(
                "INSERT INTO user_subscriptions VALUES(?,?,1) ON CONFLICT(user_id,subscription_id) DO UPDATE SET completed=1",
                (uid, tid),
            )
            self._points(c, uid, r["points_reward"], f"task:{tid}")
            return r["points_reward"]

    def add_item(
        self,
        name,
        price,
        description="",
        total=None,
        per_user=None,
        manual=True,
        content="",
        image="",
        kind="photo",
    ):
        with self.db(True) as c:
            return c.execute(
                "INSERT INTO shop_items(name,description,price,total_limit,user_limit,manual_delivery,content,image,image_kind) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    name,
                    description,
                    positive(price),
                    positive(total) if total else None,
                    positive(per_user) if per_user else None,
                    int(manual),
                    content,
                    image,
                    kind,
                ),
            ).lastrowid

    def buy_item(self, uid, iid):
        with self.db(True) as c:
            r = c.execute("SELECT * FROM shop_items WHERE id=? AND is_active=1", (iid,)).fetchone()
            if not r:
                raise RuleError("Товар недоступен.")
            counts = c.execute(
                "SELECT COUNT(*),COALESCE(SUM(user_id=?),0) FROM purchases WHERE item_id=?", (uid, iid)
            ).fetchone()
            if r["total_limit"] is not None and counts[0] >= r["total_limit"]:
                raise RuleError("Товар закончился.")
            if r["user_limit"] is not None and counts[1] >= r["user_limit"]:
                raise RuleError("Лимит покупок исчерпан.")
            self._points(c, uid, -positive(r["price"]), f"shop:{iid}")
            pid = c.execute(
                "INSERT INTO purchases(user_id,item_id,content,delivered) VALUES(?,?,?,?)",
                (uid, iid, r["content"], int(not r["manual_delivery"])),
            ).lastrowid
            return pid, dict(r)

    def reward(self, name, price, percent):
        percent = positive(percent)
        if percent > 100:
            raise RuleError("Скидка не может превышать 100%.")
        with self.db(True) as c:
            return c.execute(
                "INSERT INTO rewards(name,price,percent) VALUES(?,?,?)", (name, positive(price), percent)
            ).lastrowid

    def buy_reward(self, uid, rid):
        with self.db(True) as c:
            r = c.execute("SELECT * FROM rewards WHERE id=? AND active=1", (rid,)).fetchone()
            if not r:
                raise RuleError("Бонус недоступен.")
            self._points(c, uid, -r["price"], f"reward:{rid}")
            return c.execute(
                "INSERT INTO coupons(user_id,reward_id,percent) VALUES(?,?,?)", (uid, rid, r["percent"])
            ).lastrowid

    def create_order(
        self, uid, slug, brief, refs, coupon=None, slot=None, options=None, amounts=None, method=""
    ):
        if not brief.strip() or len(brief) > 3500 or len(refs) > 10:
            raise RuleError("ТЗ: до 3500 символов; референсы: до 10 файлов.")
        with self.db(True) as c:
            if not c.execute("SELECT 1 FROM art_products WHERE slug=? AND active=1", (slug,)).fetchone():
                raise RuleError("Этот формат пока недоступен.")
            if (
                c.execute(
                    "SELECT COUNT(*) FROM orders WHERE user_id=? AND status IN ('requested','quoted')", (uid,)
                ).fetchone()[0]
                >= 5
            ):
                raise RuleError("У вас уже пять заявок на согласовании.")
            c.execute("DELETE FROM slot_holds WHERE expires_at<=?", (time.time(),))
            if slot is not None:
                hold = c.execute(
                    "SELECT 1 FROM slot_holds WHERE slot_id=? AND user_id=?", (slot, uid)
                ).fetchone()
                if not hold:
                    raise RuleError(
                        "Резерв слота истёк. Выберите свободный слот ещё раз; описание и референсы сохранены."
                    )
            selected = c.execute(
                "SELECT * FROM slots s WHERE slug=? AND active=1 "
                "AND (? IS NULL OR id=?) AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.slot_id=s.id "
                "AND o.status IN ('requested','quoted','paid','working','delivered')) "
                "AND NOT EXISTS (SELECT 1 FROM slot_holds h WHERE h.slot_id=s.id AND h.user_id!=?) ORDER BY id LIMIT 1",
                (slug, slot, slot, uid),
            ).fetchone()
            if not selected:
                raise RuleError("Свободных слотов для этой позиции пока нет.")
            discount = 0
            if coupon:
                r = c.execute(
                    "SELECT * FROM coupons WHERE id=? AND user_id=? AND state='available'", (coupon, uid)
                ).fetchone()
                if not r:
                    raise RuleError("Купон уже использован или недоступен.")
                discount = r["percent"]
                c.execute("UPDATE coupons SET state='reserved' WHERE id=?", (coupon,))
            oid = c.execute(
                "INSERT INTO orders(user_id,slug,brief,refs,coupon_id,discount,slot_id,slot_label,options,base_rub,base_stars,preferred_method) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uid,
                    slug,
                    brief,
                    json.dumps(refs),
                    coupon,
                    discount,
                    selected["id"],
                    selected["title"],
                    json.dumps(options or {}),
                    (amounts or {}).get("rub"),
                    (amounts or {}).get("stars"),
                    method,
                ),
            ).lastrowid
            c.execute("DELETE FROM slot_holds WHERE user_id=?", (uid,))
            self._sync_ych(c, slug)
            return oid

    def order(self, oid, uid=None):
        r = self.one(
            "SELECT o.*,p.title,p.category FROM orders o JOIN art_products p ON p.slug=o.slug WHERE o.id=?",
            (oid,),
        )
        if not r or (uid is not None and r["user_id"] != uid):
            raise RuleError("Заказ не найден.")
        return r

    def quote(self, oid, rub, stars, note, points=None):
        with self.db(True) as c:
            self._invoice_guard(c, oid)
            r = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if (
                not r
                or r["status"] not in {"requested", "quoted"}
                or r["checkout_until"] > time.time()
                or r["proof"]
            ):
                raise RuleError("Заказ нельзя переоценить сейчас.")
            rub = positive(rub)
            stars = positive(stars) if stars is not None else None
            if points is not None:
                points = positive(points)
                if r["coupon_id"]:
                    raise RuleError(
                        "Купон действует на денежный заказ. Для арта за баллы создайте заявку без купона."
                    )
                p = c.execute("SELECT points FROM art_products WHERE slug=?", (r["slug"],)).fetchone()
                if not p or not p[0] or points != p[0]:
                    raise RuleError("Сначала включите этот формат через /artpoints; цена должна совпадать.")
            factor = (100 - r["discount"]) / 100
            rub = math.ceil(rub * factor)
            stars = math.ceil(stars * factor) if stars else None
            # A 100% loyalty discount becomes a free redemption, never a zero-Star invoice.
            invoice = secrets.token_urlsafe(24)
            c.execute(
                "UPDATE orders SET rub=?,stars=?,points=?,note=?,invoice=?,status='quoted',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (rub, stars, points, note, invoice, oid),
            )
            c.execute(
                "UPDATE orders SET payment_method='',external_currency='',external_amount='',external_instructions='',proof='',gateway='manual' WHERE id=?",
                (oid,),
            )
        return self.order(oid)

    def choose_payment(self, uid, oid, method, config=None):
        """Switch a quoted order before any invoice/receipt is pending, with atomic guards."""
        from .ordering import method_amount

        with self.db(True) as c:
            self._invoice_guard(c, oid)
            o = c.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, uid)).fetchone()
            if (
                not o
                or o["status"] != "quoted"
                or o["checkout_until"] > time.time()
                or o["proof"]
                or o["points"]
                or o["discount"] == 100
            ):
                raise RuleError("Способ оплаты сейчас нельзя изменить. Откройте актуальный заказ.")
            if method == "stars":
                if not o["stars"]:
                    raise RuleError("Цена в Stars ещё не установлена.")
                c.execute(
                    "UPDATE orders SET payment_method='',external_currency='',external_amount='',external_instructions='',gateway='manual',invoice=? WHERE id=?",
                    (secrets.token_urlsafe(24), oid),
                )
            else:
                if not config or not config.get("instructions"):
                    raise RuleError("Способ оплаты ещё не настроен.")
                amount = method_amount(o["rub"], config)
                c.execute(
                    "UPDATE orders SET payment_method=?,external_currency=?,external_amount=?,external_instructions=?,gateway=?,invoice=NULL WHERE id=?",
                    (
                        method,
                        config["currency"],
                        amount,
                        config["instructions"],
                        config.get("provider", "manual"),
                        oid,
                    ),
                )
            c.execute(
                "UPDATE orders SET preferred_method=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (method, oid)
            )
        return self.order(oid)

    def precheckout(self, uid, invoice, amount, currency):
        with self.db(True) as c:
            r = c.execute("SELECT * FROM orders WHERE invoice=?", (invoice,)).fetchone()
            if (
                not r
                or r["user_id"] != uid
                or r["status"] != "quoted"
                or currency != "XTR"
                or not r["stars"]
                or amount != r["stars"]
                or r["points"]
                or r["payment_method"]
            ):
                raise RuleError("Счёт недоступен или изменился. Откройте актуальный заказ.")
            c.execute("UPDATE orders SET checkout_until=? WHERE id=?", (time.time() + 120, r["id"]))

    def paid(self, uid, invoice, amount, currency, charge):
        with self.db(True) as c:
            old = c.execute("SELECT * FROM payments WHERE charge=?", (charge,)).fetchone()
            if old:
                return old["order_id"], False, old["state"]
            r = c.execute("SELECT * FROM orders WHERE invoice=?", (invoice,)).fetchone()
            valid = bool(
                r
                and r["user_id"] == uid
                and r["status"] == "quoted"
                and currency == "XTR"
                and r["stars"] == amount
                and amount > 0
                and not r["points"]
                and not r["payment_method"]
            )
            oid = r["id"] if r else 0
            state = "paid" if valid else "refund_required"
            c.execute(
                "INSERT INTO payments(charge,order_id,user_id,amount,state) VALUES(?,?,?,?,?)",
                (charge, oid, uid, amount, state),
            )
            if valid:
                c.execute(
                    "UPDATE orders SET status='paid',charge=?,checkout_until=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (charge, oid),
                )
                if r["coupon_id"]:
                    c.execute("UPDATE coupons SET state='used' WHERE id=?", (r["coupon_id"],))
            return oid, True, state

    def redeem_art(self, uid, oid):
        with self.db(True) as c:
            r = c.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, uid)).fetchone()
            if not r or r["status"] != "quoted":
                raise RuleError("Заказ недоступен.")
            if r["payment_method"]:
                raise RuleError("Этот заказ ожидает внешнюю оплату.")
            if r["points"]:
                self._points(c, uid, -r["points"], f"art:{oid}")
            elif r["discount"] != 100:
                raise RuleError("Этот заказ ожидает оплату Stars.")
            c.execute("UPDATE orders SET status='paid',updated_at=CURRENT_TIMESTAMP WHERE id=?", (oid,))
            if r["coupon_id"]:
                c.execute("UPDATE coupons SET state='used' WHERE id=?", (r["coupon_id"],))

    def cancel_order(self, uid, oid, admin=False):
        with self.db(True) as c:
            self._invoice_guard(c, oid)
            r = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if not r or (not admin and r["user_id"] != uid):
                raise RuleError("Заказ не найден.")
            if r["status"] not in {"requested", "quoted"} or r["checkout_until"] > time.time():
                raise RuleError("Заказ уже оплачен или оплата выполняется. Обратитесь в /paysupport.")
            if r["proof"] and not admin:
                raise RuleError("Tibris уже проверяет внешний платёж. Напишите в /paysupport для отмены.")
            c.execute(
                "UPDATE orders SET status='cancelled',coupon_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (oid,),
            )
            if r["coupon_id"]:
                c.execute("UPDATE coupons SET state='available' WHERE id=?", (r["coupon_id"],))
            self._sync_ych(c, r["slug"])

    def working(self, oid):
        with self.db(True) as c:
            if (
                c.execute(
                    "UPDATE orders SET status='working',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='paid'",
                    (oid,),
                ).rowcount
                != 1
            ):
                raise RuleError("В работу можно взять только оплаченный заказ.")

    def delivered(self, oid, result):
        with self.db(True) as c:
            if (
                c.execute(
                    "UPDATE orders SET status='delivered',result=?,updated_at=CURRENT_TIMESTAMP WHERE id=? "
                    "AND status IN ('paid','working')",
                    (json.dumps(result), oid),
                ).rowcount
                != 1
            ):
                raise RuleError("Заказ ещё не оплачен или уже закрыт.")
            o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            is_ych = c.execute("SELECT 1 FROM ych WHERE slug=?", (o["slug"],)).fetchone()
            if is_ych:
                c.execute("UPDATE slots SET active=0 WHERE id=?", (o["slot_id"],))
            else:
                c.execute("UPDATE orders SET slot_id=NULL WHERE id=?", (oid,))
            self._sync_ych(c, o["slug"])

    def mark_refund(self, charge):
        with self.db(True) as c:
            p = c.execute("SELECT * FROM payments WHERE charge=?", (charge,)).fetchone()
            if not p:
                raise RuleError("Платёж не найден.")
            if p["state"] == "refunded":
                return
            c.execute("UPDATE payments SET state='refunded' WHERE charge=?", (charge,))
            r = c.execute("SELECT * FROM orders WHERE id=? AND charge=?", (p["order_id"], charge)).fetchone()
            if r:
                c.execute(
                    "UPDATE orders SET status='refunded',coupon_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (r["id"],),
                )
                if r["coupon_id"]:
                    c.execute("UPDATE coupons SET state='available' WHERE id=?", (r["coupon_id"],))
                self._sync_ych(c, r["slug"])

    def refund_points(self, oid):
        with self.db(True) as c:
            r = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if (
                not r
                or r["status"] not in {"paid", "working", "delivered"}
                or r["charge"]
                or r["payment_method"]
            ):
                raise RuleError("Заказ нельзя вернуть этим способом.")
            if r["points"]:
                self._points(c, r["user_id"], r["points"], f"refund_art:{oid}")
            c.execute(
                "UPDATE orders SET status='refunded',coupon_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (oid,),
            )
            if r["coupon_id"]:
                c.execute("UPDATE coupons SET state='available' WHERE id=?", (r["coupon_id"],))
            self._sync_ych(c, r["slug"])

    def deactivate(self, table, oid):
        flags = {
            "riddles": "is_active",
            "subscriptions": "is_active",
            "shop_items": "is_active",
            "rewards": "active",
        }
        if table not in flags:
            raise RuleError("Недопустимый раздел.")
        with self.db(True) as c:
            if c.execute(f"UPDATE {table} SET {flags[table]}=0 WHERE id=?", (oid,)).rowcount != 1:
                raise RuleError("Запись не найдена.")

    def external_offer(self, oid, method, amount, rub, instructions):
        config = self.setting("method:" + method)
        if not config:
            raise RuleError("Сначала настройте способ через /method название валюта реквизиты.")
        config = json.loads(config)
        try:
            amount = Decimal(str(amount).replace(",", "."))
        except InvalidOperation:
            raise RuleError("Некорректная сумма.") from None
        if not amount.is_finite() or amount <= 0 or not instructions.strip():
            raise RuleError("Укажите положительную сумму и согласованные условия.")
        with self.db(True) as c:
            self._invoice_guard(c, oid)
            o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if (
                not o
                or o["status"] not in {"requested", "quoted"}
                or o["checkout_until"] > time.time()
                or o["proof"]
            ):
                raise RuleError("Нельзя изменить предложение: заказ недоступен или платёж уже проверяется.")
            if o["discount"] == 100:
                raise RuleError("Для скидки 100% используйте обычное предложение и подтверждение бонуса.")
            factor = Decimal(100 - o["discount"]) / Decimal(100)
            quantum = Decimal("0.01") if config["currency"] in {"RUB", "USD", "EUR"} else Decimal("0.000001")
            net = (amount * factor).quantize(quantum, rounding=ROUND_UP)
            c.execute(
                "UPDATE orders SET status='quoted',rub=?,stars=NULL,points=NULL,invoice=NULL,payment_method=?,"
                "external_currency=?,external_amount=?,external_instructions=?,note=?,proof='',gateway=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (
                    math.ceil(positive(rub) * float(factor)),
                    method,
                    config["currency"],
                    str(net),
                    config["instructions"],
                    instructions,
                    config.get("provider", "manual"),
                    oid,
                ),
            )
        return self.order(oid)

    def payment_proof(self, uid, oid, proof):
        with self.db(True) as c:
            o = c.execute(
                "SELECT * FROM orders WHERE id=? AND user_id=? AND status='quoted'", (oid, uid)
            ).fetchone()
            if not o or not o["payment_method"]:
                raise RuleError("Заказ недоступен для внешней оплаты.")
            c.execute(
                "UPDATE orders SET proof=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(proof), oid)
            )

    def confirm_external(self, oid, reference):
        if not reference.strip():
            raise RuleError("Укажите уникальный номер операции из выписки / кабинета.")
        with self.db(True) as c:
            o = c.execute("SELECT * FROM orders WHERE id=? AND status='quoted'", (oid,)).fetchone()
            if not o or not o["payment_method"]:
                raise RuleError("Заказ не ожидает внешнюю оплату или уже подтверждён.")
            if o["gateway"] != "manual":
                raise RuleError("Этот способ подтверждается через API. Используйте кнопку проверки в заказе.")
            try:
                c.execute(
                    "INSERT INTO external_payments(order_id,method,reference) VALUES(?,?,?)",
                    (oid, o["payment_method"], reference.strip()),
                )
            except sqlite3.IntegrityError:
                raise RuleError("Эта операция уже использована для другого заказа.") from None
            c.execute("UPDATE orders SET status='paid',updated_at=CURRENT_TIMESTAMP WHERE id=?", (oid,))
            if o["coupon_id"]:
                c.execute("UPDATE coupons SET state='used' WHERE id=?", (o["coupon_id"],))

    def refund_external(self, oid, reference):
        if not reference.strip():
            raise RuleError("Укажите номер реально выполненного возврата.")
        with self.db(True) as c:
            o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            p = c.execute(
                "SELECT * FROM external_payments WHERE order_id=? AND refunded=0", (oid,)
            ).fetchone()
            if not o or not p or o["status"] not in {"paid", "working", "delivered"}:
                raise RuleError("Заказ недоступен для отметки возврата.")
            c.execute(
                "UPDATE external_payments SET refunded=1,refund_reference=? WHERE order_id=?",
                (reference.strip(), oid),
            )
            c.execute(
                "UPDATE orders SET status='refunded',coupon_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (oid,),
            )
            if o["coupon_id"]:
                c.execute("UPDATE coupons SET state='available' WHERE id=?", (o["coupon_id"],))
            self._sync_ych(c, o["slug"])

    def _invoice_guard(self, c, oid):
        if c.execute(
            "SELECT 1 FROM auto_invoices WHERE order_id=? AND state IN ('creating','uncertain','pending')",
            (oid,),
        ).fetchone():
            raise RuleError(
                "Платёжная ссылка уже создана. Дождитесь оплаты/истечения счёта или обратитесь в /paysupport."
            )

    def auto_invoice(self, oid):
        return self.one("SELECT * FROM auto_invoices WHERE order_id=?", (oid,))

    def reserve_auto(self, oid):
        with self.db(True) as c:
            existing = c.execute("SELECT * FROM auto_invoices WHERE order_id=?", (oid,)).fetchone()
            if existing:
                return dict(existing), False
            o = c.execute("SELECT * FROM orders WHERE id=? AND status='quoted'", (oid,)).fetchone()
            if not o or o["gateway"] not in {"cryptopay", "yoomoney"}:
                raise RuleError("Заказ не ожидает автоматическую оплату.")
            nonce = secrets.token_hex(20)
            iid = c.execute(
                "INSERT INTO auto_invoices(order_id,method,provider,nonce,amount,currency,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    oid,
                    o["payment_method"],
                    o["gateway"],
                    nonce,
                    o["external_amount"],
                    o["external_currency"],
                    time.time(),
                ),
            ).lastrowid
            return dict(c.execute("SELECT * FROM auto_invoices WHERE id=?", (iid,)).fetchone()), True

    def attach_auto(self, iid, remote_id, url, gross=""):
        if not url.startswith("https://"):
            raise RuleError("Провайдер не вернул безопасную платёжную ссылку.")
        with self.db(True) as c:
            c.execute(
                "UPDATE auto_invoices SET remote_id=?,url=?,gross=?,state='pending' WHERE id=? AND state IN ('creating','uncertain')",
                (str(remote_id), url, gross, iid),
            )

    def auto_state(self, iid, state):
        if state not in {"uncertain", "expired"}:
            raise RuleError("Некорректное состояние счёта.")
        with self.db(True) as c:
            c.execute("UPDATE auto_invoices SET state=? WHERE id=? AND state!='paid'", (state, iid))

    def confirm_auto(self, iid, reference):
        with self.db(True) as c:
            invoice = c.execute("SELECT * FROM auto_invoices WHERE id=?", (iid,)).fetchone()
            if not invoice:
                raise RuleError("Счёт не найден.")
            if invoice["state"] == "paid":
                return False
            o = c.execute("SELECT * FROM orders WHERE id=?", (invoice["order_id"],)).fetchone()
            if (
                not o
                or o["status"] != "quoted"
                or o["payment_method"] != invoice["method"]
                or o["gateway"] != invoice["provider"]
                or o["external_currency"] != invoice["currency"]
                or Decimal(o["external_amount"]) != Decimal(invoice["amount"])
            ):
                c.execute(
                    "INSERT OR IGNORE INTO auto_review(order_id,provider,reference,reason) VALUES(?,?,?,?)",
                    (
                        invoice["order_id"],
                        invoice["provider"],
                        str(reference),
                        "Payment arrived for a cancelled or changed order",
                    ),
                )
                c.execute("UPDATE auto_invoices SET state='review' WHERE id=?", (iid,))
                return "review"
            try:
                c.execute(
                    "INSERT INTO external_payments(order_id,method,reference) VALUES(?,?,?)",
                    (o["id"], invoice["provider"], str(reference)),
                )
            except sqlite3.IntegrityError:
                raise RuleError("Операция уже учтена.") from None
            c.execute("UPDATE auto_invoices SET state='paid' WHERE id=?", (iid,))
            c.execute("UPDATE orders SET status='paid',updated_at=CURRENT_TIMESTAMP WHERE id=?", (o["id"],))
            if o["coupon_id"]:
                c.execute("UPDATE coupons SET state='used' WHERE id=?", (o["coupon_id"],))
            return True

    def void_auto(self, oid):
        with self.db(True) as c:
            r = c.execute("SELECT * FROM auto_invoices WHERE order_id=?", (oid,)).fetchone()
            if not r or r["state"] not in {"pending", "uncertain", "creating"}:
                raise RuleError("Нет открытого автоматического счёта.")
            c.execute("UPDATE auto_invoices SET state='watching' WHERE order_id=?", (oid,))

    def free_slots(self, slug, uid=0):
        return self.rows(
            "SELECT s.* FROM slots s WHERE slug=? AND active=1 AND NOT EXISTS "
            "(SELECT 1 FROM orders o WHERE o.slot_id=s.id AND status IN ('requested','quoted','paid','working','delivered')) "
            "AND NOT EXISTS (SELECT 1 FROM slot_holds h WHERE h.slot_id=s.id AND h.user_id!=? AND h.expires_at>?) ORDER BY id",
            (slug, uid, time.time()),
        )

    def hold_slot(self, uid, sid, slug):
        with self.db(True) as c:
            c.execute("DELETE FROM slot_holds WHERE expires_at<=?", (time.time(),))
            row = c.execute(
                "SELECT s.* FROM slots s JOIN art_products p ON s.slug=p.slug WHERE s.id=? AND s.slug=? AND s.active=1 AND p.active=1",
                (sid, slug),
            ).fetchone()
            if (
                not row
                or c.execute(
                    "SELECT 1 FROM orders WHERE slot_id=? AND status IN ('requested','quoted','paid','working','delivered')",
                    (sid,),
                ).fetchone()
            ):
                raise RuleError("Слот уже занят или закрыт.")
            old = c.execute("SELECT user_id FROM slot_holds WHERE slot_id=?", (sid,)).fetchone()
            if old and old[0] != uid:
                raise RuleError("Другой клиент уже резервирует этот слот.")
            c.execute("DELETE FROM slot_holds WHERE user_id=?", (uid,))
            c.execute("INSERT INTO slot_holds VALUES(?,?,?)", (sid, uid, time.time() + 1800))
            return dict(row)

    def add_slot(self, slug, title):
        with self.db(True) as c:
            if not c.execute("SELECT 1 FROM art_products WHERE slug=?", (slug,)).fetchone():
                raise RuleError("Позиция не найдена.")
            sid = c.execute(
                "INSERT INTO slots(slug,title) VALUES(?,?)", (slug, title.strip() or "Слот")
            ).lastrowid
            self._sync_ych(c, slug)
            return sid

    def set_slot(self, sid, active):
        with self.db(True) as c:
            r = c.execute("SELECT * FROM slots WHERE id=?", (sid,)).fetchone()
            if not r:
                raise RuleError("Слот не найден.")
            c.execute("UPDATE slots SET active=? WHERE id=?", (int(active), sid))
            self._sync_ych(c, r["slug"])

    def add_ych(self, title, rub, count, description, media):
        count = positive(count)
        if count > 100:
            raise RuleError("Не больше 100 мест у одного YCH.")
        with self.db(True) as c:
            number = c.execute("SELECT COALESCE(MAX(id),0)+1 FROM ych").fetchone()[0]
            slug = f"ych_{number}"
            c.execute("INSERT INTO ych VALUES(?,?)", (number, slug))
            c.execute(
                "INSERT INTO art_products(slug,category,title,rub,description) VALUES(?,'ych',?,?,?)",
                (slug, title, positive(rub), description),
            )
            c.execute("INSERT INTO media VALUES(?,?,?)", ("product:" + slug, media["file_id"], media["kind"]))
            for n in range(1, count + 1):
                c.execute("INSERT INTO slots(slug,title) VALUES(?,?)", (slug, f"Место {n}"))
            return slug

    def _sync_ych(self, c, slug):
        if not c.execute("SELECT 1 FROM ych WHERE slug=?", (slug,)).fetchone():
            return
        free = c.execute(
            "SELECT 1 FROM slots s WHERE slug=? AND active=1 AND NOT EXISTS "
            "(SELECT 1 FROM orders o WHERE o.slot_id=s.id AND status IN ('requested','quoted','paid','working','delivered')) LIMIT 1",
            (slug,),
        ).fetchone()
        c.execute("UPDATE art_products SET active=? WHERE slug=?", (int(bool(free)), slug))

    def add_promo(self, code, percent, limit=None, days=None):
        code = code.strip().casefold()
        if not 3 <= len(code) <= 64 or not code.replace("_", "").replace("-", "").isalnum():
            raise RuleError("Промокод: 3–64 буквы/цифры, дефис или подчёркивание.")
        percent = positive(percent)
        if percent > 100:
            raise RuleError("Скидка: от 1 до 100%.")
        with self.db(True) as c:
            if c.execute("SELECT 1 FROM promos WHERE code=?", (code,)).fetchone():
                raise RuleError("Этот код уже существует. Используйте другое название.")
            c.execute(
                "INSERT INTO promos(code,percent,total_limit,expires_at) VALUES(?,?,?,?)",
                (
                    code,
                    percent,
                    positive(limit) if limit else None,
                    time.time() + positive(days) * 86400 if days else None,
                ),
            )

    def claim_promo(self, uid, code):
        code = code.strip().casefold()
        with self.db(True) as c:
            p = c.execute("SELECT * FROM promos WHERE code=? AND active=1", (code,)).fetchone()
            if not p or (p["expires_at"] is not None and p["expires_at"] <= time.time()):
                raise RuleError("Промокод не найден или истёк.")
            if c.execute("SELECT 1 FROM promo_claims WHERE code=? AND user_id=?", (code, uid)).fetchone():
                raise RuleError("Вы уже активировали этот промокод.")
            count = c.execute("SELECT COUNT(*) FROM promo_claims WHERE code=?", (code,)).fetchone()[0]
            if p["total_limit"] is not None and count >= p["total_limit"]:
                raise RuleError("Лимит промокода исчерпан.")
            cid = c.execute(
                "INSERT INTO coupons(user_id,reward_id,percent) VALUES(?,0,?)", (uid, p["percent"])
            ).lastrowid
            c.execute("INSERT INTO promo_claims VALUES(?,?,?)", (code, uid, cid))
            return cid, p["percent"]
