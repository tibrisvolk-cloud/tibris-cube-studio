import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from studio.store import RuleError, Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.user(1, "buyer")
    s.user(2, "other")
    s.adjust("1", 1000)
    s.adjust("2", 1000)
    return s


def test_code_only_once_even_from_old_button(store):
    tid = store.add_task("https://example.com", "code", 5, "code", code="ABC")
    store.complete_task(1, tid, "code", "abc")
    with pytest.raises(RuleError):
        store.complete_task(1, tid, "code", "ABC")
    assert store.balance(1) == 1005


@pytest.mark.parametrize("kind", ["subscription", "code", "external"])
def test_task_global_limit_enforced_at_completion(store, kind):
    tid = store.add_task("https://example.com", "task", 5, kind, limit=1, code="ABC")
    if kind == "external":
        store.start_task(1, tid)
        store.start_task(2, tid)
    evidence = "ABC" if kind == "code" else True
    store.complete_task(1, tid, kind, evidence)
    with pytest.raises(RuleError, match="лимит"):
        store.complete_task(2, tid, kind, evidence)
    assert store.balance(2) == 1000


def test_cannot_use_subscription_callback_to_bypass_code(store):
    tid = store.add_task("https://example.com", "task", 5, "code", code="SECRET")
    with pytest.raises(RuleError):
        store.complete_task(1, tid, "subscription", True)


def test_external_timer_persists_across_restart(store):
    tid = store.add_task("https://example.com", "task", 5, "external", delay=60)
    store.start_task(1, tid)
    reopened = Store(store.path)
    with pytest.raises(RuleError, match="Подождите"):
        reopened.complete_task(1, tid, "external")
    start = reopened.one("SELECT started_at FROM task_starts")["started_at"]
    assert reopened.complete_task(1, tid, "external", now=start + 60) == 5


def test_external_requires_start(store):
    tid = store.add_task("https://example.com", "task", 5, "external")
    with pytest.raises(RuleError, match="Начать"):
        store.complete_task(1, tid, "external")


def test_riddle_daily_reset_uses_one_attempt(store):
    rid = store.add_riddle("Question", "Answer", 10)
    for i in range(3):
        store.answer(1, rid, "wrong", now=100000 + i)
    with pytest.raises(RuleError):
        store.answer(1, rid, "wrong", now=100010)
    correct, reward, remaining = store.answer(1, rid, "wrong", now=186400)
    assert remaining == 2
    assert store.one("SELECT attempts_count FROM user_riddle_attempts")["attempts_count"] == 1


def test_riddle_correct_third_attempt_and_no_second_reward(store):
    rid = store.add_riddle("Question", "Answer", 10)
    store.answer(1, rid, "wrong")
    store.answer(1, rid, "wrong")
    assert store.answer(1, rid, "  ANSWER  ")[0] is True
    with pytest.raises(RuleError):
        store.answer(1, rid, "Answer")
    assert store.balance(1) == 1010


def test_riddle_global_limit(store):
    rid = store.add_riddle("Question", "answer", 10, limit=1)
    store.answer(1, rid, "answer")
    with pytest.raises(RuleError, match="лимит"):
        store.answer(2, rid, "answer")


def test_partial_legacy_migrations_and_repeated_initialization(tmp_path):
    path = tmp_path / "legacy.db"
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE riddles(id INTEGER PRIMARY KEY,text TEXT,answer TEXT,points_reward INTEGER,total_limit INTEGER,is_active INTEGER)"
    )
    c.execute(
        "CREATE TABLE purchases(id INTEGER PRIMARY KEY,user_id INTEGER,item_id INTEGER,content TEXT,purchased_at TEXT)"
    )
    c.commit()
    c.close()
    Store(path)
    Store(path)
    c = sqlite3.connect(path)
    assert {"image", "image_kind", "total_limit"} <= {r[1] for r in c.execute("PRAGMA table_info(riddles)")}
    assert "delivered" in {r[1] for r in c.execute("PRAGMA table_info(purchases)")}
    c.close()


def test_purchase_atomic_no_overselling(store):
    iid = store.add_item("last item", 100, total=1)

    def buy(uid):
        try:
            store.buy_item(uid, iid)
            return True
        except RuleError:
            return False

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(buy, [1, 2]))
    assert sum(results) == 1
    assert store.balance(1) + store.balance(2) == 1900
    assert len(store.rows("SELECT * FROM purchases")) == 1


def test_purchase_personal_limit_and_content_snapshot(store):
    iid = store.add_item("item", 100, per_user=1, manual=False, content="original")
    pid, item = store.buy_item(1, iid)
    with store.db(True) as c:
        c.execute("UPDATE shop_items SET content='edited' WHERE id=?", (iid,))
    assert store.one("SELECT * FROM purchases WHERE id=?", (pid,))["content"] == "original"
    with pytest.raises(RuleError):
        store.buy_item(1, iid)
    assert store.balance(1) == 900


def test_task_atomic_one_completion(store):
    tid = store.add_task("https://example.com", "task", 5, "code", code="ABC")

    def claim(_):
        try:
            store.complete_task(1, tid, "code", "ABC")
            return True
        except RuleError:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(claim, [1, 1])) == 1
    assert store.balance(1) == 1005


def test_coupon_reservation_discount_and_release(store):
    rid = store.reward("10%", 20, 10)
    cid = store.buy_reward(1, rid)
    oid = store.create_order(1, "classic_head", "brief", [], cid)
    with pytest.raises(RuleError):
        store.create_order(1, "classic_head", "other", [], cid)
    with pytest.raises(RuleError):
        store.create_order(2, "classic_head", "steal", [], cid)
    o = store.quote(oid, 180, 101, "week")
    assert (o["rub"], o["stars"]) == (162, 91)
    store.cancel_order(1, oid)
    assert store.one("SELECT state FROM coupons WHERE id=?", (cid,))["state"] == "available"
    store.create_order(1, "classic_head", "another", [], cid)


def make_quote(s):
    oid = s.create_order(1, "classic_head", "brief", [])
    return s.quote(oid, 180, 250, "week; one correction")


def test_payment_duplicate_replay_and_second_charge_refund(store):
    o = make_quote(store)
    store.precheckout(1, o["invoice"], 250, "XTR")
    assert store.paid(1, o["invoice"], 250, "XTR", "charge-1") == (o["id"], True, "paid")
    assert store.paid(1, o["invoice"], 250, "XTR", "charge-1") == (o["id"], False, "paid")
    assert store.paid(1, o["invoice"], 250, "XTR", "charge-2")[2] == "refund_required"
    assert store.order(o["id"])["charge"] == "charge-1"


@pytest.mark.parametrize("uid,amount,currency", [(2, 250, "XTR"), (1, 1, "XTR"), (1, 250, "USD")])
def test_precheckout_rejects_tampered_invoice(store, uid, amount, currency):
    o = make_quote(store)
    with pytest.raises(RuleError):
        store.precheckout(uid, o["invoice"], amount, currency)


def test_quote_replacement_invalidates_old_invoice(store):
    o = make_quote(store)
    new = store.quote(o["id"], 200, 300, "changed scope")
    assert new["invoice"] != o["invoice"]
    with pytest.raises(RuleError):
        store.precheckout(1, o["invoice"], 250, "XTR")
    assert store.paid(1, o["invoice"], 250, "XTR", "late")[2] == "refund_required"


def test_checkout_blocks_cancel_and_requote(store):
    o = make_quote(store)
    store.precheckout(1, o["invoice"], 250, "XTR")
    with pytest.raises(RuleError):
        store.cancel_order(1, o["id"])
    with pytest.raises(RuleError):
        store.quote(o["id"], 200, 300, "changed")


def test_unpaid_cannot_deliver_or_start_work(store):
    o = make_quote(store)
    with pytest.raises(RuleError):
        store.working(o["id"])
    with pytest.raises(RuleError):
        store.delivered(o["id"], {"file_id": "x"})


def test_points_art_atomic_and_refund_once(store):
    with store.db(True) as c:
        c.execute("UPDATE art_products SET points=12 WHERE slug='classic_head'")
    oid = store.create_order(1, "classic_head", "brief", [])
    store.quote(oid, 180, None, "week", points=12)
    store.redeem_art(1, oid)
    with pytest.raises(RuleError):
        store.redeem_art(1, oid)
    assert store.balance(1) == 988
    store.refund_points(oid)
    with pytest.raises(RuleError):
        store.refund_points(oid)
    assert store.balance(1) == 1000


def test_full_discount_redemption_and_refund_restores_coupon(store):
    cid = store.buy_reward(1, store.reward("free", 100, 100))
    oid = store.create_order(1, "classic_sticker", "brief", [], cid)
    o = store.quote(oid, 70, 100, "week")
    assert o["stars"] == 0
    with pytest.raises(RuleError):
        store.precheckout(1, o["invoice"], 0, "XTR")
    store.redeem_art(1, oid)
    store.refund_points(oid)
    assert store.one("SELECT state FROM coupons WHERE id=?", (cid,))["state"] == "available"


def test_stars_refund_replay_preserves_other_order(store):
    o = make_quote(store)
    store.paid(1, o["invoice"], 250, "XTR", "paid")
    store.paid(1, o["invoice"], 250, "XTR", "extra")
    store.mark_refund("extra")
    assert store.order(o["id"])["status"] == "paid"
    store.mark_refund("paid")
    store.mark_refund("paid")
    assert store.order(o["id"])["status"] == "refunded"


def test_identity_updates_and_negative_balance_prevented(store):
    store.user(1, "new_name")
    assert store.adjust("@new_name", 0, "set")[1] == 0
    with pytest.raises(RuleError):
        store.adjust("1", 1, "remove")


def test_dialog_persistence_and_expiry(store):
    store.save_dialog(1, "order", {"step": "refs", "refs": []})
    assert Store(store.path).dialog(1)[0] == "order"
    with store.db(True) as c:
        c.execute("UPDATE dialogs SET expires_at=0")
    assert store.dialog(1) is None


def test_actual_rub_prices_without_example_star_prices(store):
    rows = store.rows("SELECT * FROM art_products")
    assert len(rows) == 12
    assert all(p["stars"] is None and p["points"] is None for p in rows)
    assert store.one("SELECT rub FROM art_products WHERE slug='classic_head'")["rub"] == 180
    assert store.one("SELECT rub FROM art_products WHERE slug='mc_4d'")["rub"] == 400
