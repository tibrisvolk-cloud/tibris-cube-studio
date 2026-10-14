import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from studio.store import Store, RuleError
from studio.migrate import migrate


@pytest.fixture
def s(tmp_path):
    store = Store(tmp_path / "test.db")
    for uid in (1, 2):
        store.user(uid)
    return store


def test_slot_hold_blocks_racing_user_and_expires(s):
    slot = s.free_slots("classic_head")[0]["id"]
    s.hold_slot(1, slot, "classic_head")
    with pytest.raises(RuleError):
        s.hold_slot(2, slot, "classic_head")
    with s.db(True) as c:
        c.execute("UPDATE slot_holds SET expires_at=0")
    s.hold_slot(2, slot, "classic_head")


def test_slots_belong_to_positions_not_categories(s):
    sid = s.free_slots("classic_head")[0]["id"]
    with pytest.raises(RuleError):
        s.hold_slot(1, sid, "classic_full")
    for _ in range(3):
        s.create_order(1, "classic_head", "brief", [])
    with pytest.raises(RuleError):
        s.create_order(2, "classic_head", "brief", [])
    assert len(s.free_slots("classic_full")) == 3


def test_slot_booking_is_atomic(s):
    slots = s.free_slots("classic_head")
    for slot in slots[1:]:
        s.set_slot(slot["id"], False)

    def claim(uid):
        try:
            s.create_order(uid, "classic_head", "brief", [])
            return 1
        except RuleError:
            return 0

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(claim, [1, 2])) == 1


def test_cancel_and_deliver_release_regular_slots(s):
    oid = s.create_order(1, "classic_head", "brief", [])
    assert len(s.free_slots("classic_head")) == 2
    s.cancel_order(1, oid)
    assert len(s.free_slots("classic_head")) == 3
    oid = s.create_order(1, "classic_head", "brief", [])
    o = s.quote(oid, 180, 100, "week")
    s.paid(1, o["invoice"], 100, "XTR", "p")
    s.delivered(oid, {"kind": "photo", "file_id": "result"})
    assert len(s.free_slots("classic_head")) == 3
    assert s.order(oid)["slot_label"]


def test_ych_sold_out_hidden_order_history_kept(s):
    slug = s.add_ych("cute pose", 150, 1, "conditions", {"kind": "photo", "file_id": "sketch"})
    oid = s.create_order(1, slug, "brief", [])
    assert s.one("SELECT active FROM art_products WHERE slug=?", (slug,))["active"] == 0
    assert s.order(oid)["slug"] == slug
    s.cancel_order(1, oid)
    assert s.one("SELECT active FROM art_products WHERE slug=?", (slug,))["active"] == 1
    oid = s.create_order(1, slug, "brief", [])
    o = s.quote(oid, 150, 100, "week")
    s.paid(1, o["invoice"], 100, "XTR", "p")
    s.delivered(oid, {"kind": "photo", "file_id": "result"})
    assert len(s.free_slots(slug)) == 0
    assert s.order(oid)["status"] == "delivered"


def test_promo_case_insensitive_single_claim_global_limit(s):
    s.add_promo("TIBRIS10", 10, 1, 2)
    cid, discount = s.claim_promo(1, " tibris10 ")
    assert discount == 10
    with pytest.raises(RuleError):
        s.claim_promo(1, "TIBRIS10")
    with pytest.raises(RuleError):
        s.claim_promo(2, "TIBRIS10")
    oid = s.create_order(1, "classic_head", "brief", [], cid)
    assert s.quote(oid, 180, 100, "week")["rub"] == 162


def test_expired_promo(s):
    s.add_promo("OLD", 10)
    with s.db(True) as c:
        c.execute("UPDATE promos SET expires_at=?", (time.time() - 1,))
    with pytest.raises(RuleError):
        s.claim_promo(1, "old")


def test_external_proof_is_not_payment_and_duplicate_reference_rejected(s):
    s.set_setting("method:sbp", json.dumps({"currency": "RUB", "instructions": "requisites"}))
    oid = s.create_order(1, "classic_head", "brief", [])
    s.external_offer(oid, "sbp", "180", 180, "week")
    s.payment_proof(1, oid, {"text": "I paid"})
    assert s.order(oid)["status"] == "quoted"
    with pytest.raises(RuleError):
        s.cancel_order(1, oid)
    s.confirm_external(oid, "bank-operation-1")
    with pytest.raises(RuleError):
        s.confirm_external(oid, "bank-operation-1")
    another = s.create_order(2, "classic_head", "brief", [])
    s.external_offer(another, "sbp", "180", 180, "week")
    with pytest.raises(RuleError):
        s.confirm_external(another, "bank-operation-1")
    with pytest.raises(RuleError):
        s.refund_points(oid)
    s.refund_external(oid, "bank-refund-1")
    assert s.order(oid)["status"] == "refunded"


def test_migration_never_overwrites_source_or_existing_destination(s, tmp_path):
    before = s.balance(1)
    target = tmp_path / "migrated.db"
    migrated = migrate(s.path, target)
    assert migrated.balance(1) == before
    assert Store(s.path).balance(1) == before
    with pytest.raises(ValueError):
        migrate(s.path, target)
