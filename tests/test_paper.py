from __future__ import annotations

from datetime import date

import pytest

from condorpilot.models import OptionType
from condorpilot.paper import (
    AccountRiskLimits,
    AccountSnapshot,
    BrokerOrderSnapshot,
    ComboLeg,
    IdempotencyConflict,
    InMemoryPaperStateStore,
    JsonFilePaperStateStore,
    LegFill,
    NetPriceEffect,
    OptionContract,
    OrderSide,
    OrderStatus,
    PaperOrderIntent,
    PaperTradingEngine,
    ReconciliationError,
    SafetyHalt,
    apply_broker_snapshot,
    evaluate_order_risk,
)


class FakeBroker:
    def __init__(self) -> None:
        self.account = AccountSnapshot(equity=50_000)
        self.submit_calls = 0
        self.submit_snapshot: BrokerOrderSnapshot | None = None
        self.orders: dict[str, BrokerOrderSnapshot] = {}
        self.open_order_snapshots: tuple[BrokerOrderSnapshot, ...] = ()

    def account_snapshot(self) -> AccountSnapshot:
        return self.account

    def submit_order(self, intent: PaperOrderIntent) -> BrokerOrderSnapshot:
        self.submit_calls += 1
        snapshot = self.submit_snapshot or _snapshot("broker-1", OrderStatus.WORKING, intent)
        self.orders[snapshot.broker_order_id] = snapshot
        return snapshot

    def get_order(self, broker_order_id: str) -> BrokerOrderSnapshot | None:
        return self.orders.get(broker_order_id)

    def open_orders(self) -> tuple[BrokerOrderSnapshot, ...]:
        return self.open_order_snapshots

    def cancel_order(self, broker_order_id: str) -> BrokerOrderSnapshot:
        current = self.orders[broker_order_id]
        cancelled = BrokerOrderSnapshot(
            broker_order_id=broker_order_id,
            status=OrderStatus.CANCELLED,
            leg_fills=current.leg_fills,
        )
        self.orders[broker_order_id] = cancelled
        return cancelled


def _intent(*, key: str = "entry-2026-01-05", quantity: int = 1, max_loss: float = 400) -> PaperOrderIntent:
    expiration = date(2026, 2, 20)
    return PaperOrderIntent(
        idempotency_key=key,
        legs=(
            ComboLeg(
                OptionContract("SPY", expiration, 585, OptionType.PUT),
                OrderSide.BUY,
            ),
            ComboLeg(
                OptionContract("SPY", expiration, 590, OptionType.PUT),
                OrderSide.SELL,
            ),
            ComboLeg(
                OptionContract("SPY", expiration, 650, OptionType.CALL),
                OrderSide.SELL,
            ),
            ComboLeg(
                OptionContract("SPY", expiration, 655, OptionType.CALL),
                OrderSide.BUY,
            ),
        ),
        quantity=quantity,
        limit_price=1.0,
        price_effect=NetPriceEffect.CREDIT,
        max_loss_per_combo=max_loss,
    )


def _fills(intent: PaperOrderIntent, *quantities: int) -> tuple[LegFill, ...]:
    return tuple(
        LegFill(contract_key=leg.contract.key, filled_quantity=quantity)
        for leg, quantity in zip(intent.legs, quantities, strict=True)
    )


def _snapshot(
    broker_id: str,
    status: OrderStatus,
    intent: PaperOrderIntent,
    fills: tuple[int, ...] | None = None,
) -> BrokerOrderSnapshot:
    quantities = fills or tuple(0 for _ in intent.legs)
    return BrokerOrderSnapshot(
        broker_order_id=broker_id,
        status=status,
        leg_fills=_fills(intent, *quantities),
    )


def test_duplicate_idempotency_key_returns_existing_order_without_resubmit() -> None:
    broker = FakeBroker()
    store = InMemoryPaperStateStore()
    engine = PaperTradingEngine(broker, store)
    intent = _intent()

    first = engine.submit(intent)
    second = engine.submit(intent)

    assert first == second
    assert broker.submit_calls == 1


def test_reusing_idempotency_key_for_different_intent_is_rejected() -> None:
    broker = FakeBroker()
    engine = PaperTradingEngine(broker, InMemoryPaperStateStore())
    engine.submit(_intent())

    with pytest.raises(IdempotencyConflict):
        engine.submit(_intent(quantity=2))

    assert broker.submit_calls == 1


def test_account_risk_gate_blocks_order_before_broker_submission() -> None:
    broker = FakeBroker()
    broker.account = AccountSnapshot(
        equity=50_000,
        total_defined_risk=4_900,
        symbol_defined_risk=(("SPY", 1_900),),
    )
    engine = PaperTradingEngine(broker, InMemoryPaperStateStore())

    with pytest.raises(SafetyHalt):
        engine.submit(_intent())

    assert broker.submit_calls == 0


def test_daily_loss_and_contract_limits_are_account_level_gates() -> None:
    account = AccountSnapshot(equity=50_000, daily_realized_pnl=-1_600)
    decision = evaluate_order_risk(
        _intent(quantity=6),
        account,
        AccountRiskLimits(max_contracts_per_order=5),
    )

    assert not decision.allowed
    assert "daily realized loss limit reached" in decision.reasons
    assert "order quantity exceeds contract limit" in decision.reasons


def test_uneven_partial_fill_trips_kill_switch_and_blocks_new_orders() -> None:
    broker = FakeBroker()
    intent = _intent()
    broker.submit_snapshot = _snapshot(
        "broker-1",
        OrderStatus.PARTIALLY_FILLED,
        intent,
        fills=(0, 1, 0, 0),
    )
    store = InMemoryPaperStateStore()
    engine = PaperTradingEngine(broker, store)

    order = engine.submit(intent)

    assert order.has_legging_exposure
    assert store.load().kill_switch.active
    with pytest.raises(SafetyHalt):
        engine.submit(_intent(key="second-order"))


def test_balanced_partial_combo_fill_preserves_defined_risk_shape() -> None:
    broker = FakeBroker()
    intent = _intent(quantity=2)
    broker.submit_snapshot = _snapshot(
        "broker-1",
        OrderStatus.PARTIALLY_FILLED,
        intent,
        fills=(1, 1, 1, 1),
    )
    store = InMemoryPaperStateStore()
    order = PaperTradingEngine(broker, store).submit(intent)

    assert not order.has_legging_exposure
    assert not store.load().kill_switch.active


def test_fill_quantities_must_be_monotonic() -> None:
    broker = FakeBroker()
    intent = _intent(quantity=2)
    broker.submit_snapshot = _snapshot(
        "broker-1",
        OrderStatus.PARTIALLY_FILLED,
        intent,
        fills=(1, 1, 1, 1),
    )
    engine = PaperTradingEngine(broker, InMemoryPaperStateStore())
    order = engine.submit(intent)

    backwards = _snapshot(
        "broker-1",
        OrderStatus.PARTIALLY_FILLED,
        intent,
        fills=(0, 1, 1, 1),
    )
    with pytest.raises(ReconciliationError):
        apply_broker_snapshot(order, backwards)


def test_json_store_supports_restart_recovery(tmp_path) -> None:
    broker = FakeBroker()
    path = tmp_path / "paper-state.json"
    first_engine = PaperTradingEngine(broker, JsonFilePaperStateStore(path))
    intent = _intent()
    submitted = first_engine.submit(intent)
    assert submitted.status is OrderStatus.WORKING

    broker.orders["broker-1"] = _snapshot(
        "broker-1",
        OrderStatus.FILLED,
        intent,
        fills=(1, 1, 1, 1),
    )
    restarted = PaperTradingEngine(broker, JsonFilePaperStateStore(path))
    report = restarted.recover()
    restored = restarted.store.load().by_key(intent.idempotency_key)

    assert report.clean
    assert report.refreshed_orders == 1
    assert restored is not None
    assert restored.status is OrderStatus.FILLED
    assert path.read_text(encoding="utf-8").startswith("{")


def test_restart_missing_broker_order_trips_kill_switch(tmp_path) -> None:
    broker = FakeBroker()
    store = JsonFilePaperStateStore(tmp_path / "paper-state.json")
    engine = PaperTradingEngine(broker, store)
    engine.submit(_intent())
    broker.orders.clear()

    report = PaperTradingEngine(broker, store).recover()

    assert not report.clean
    assert report.issues[0].code == "missing_broker_order"
    assert store.load().kill_switch.active


def test_unknown_broker_open_order_trips_kill_switch() -> None:
    broker = FakeBroker()
    intent = _intent()
    store = InMemoryPaperStateStore()
    engine = PaperTradingEngine(broker, store)
    engine.submit(intent)
    broker.open_order_snapshots = (
        _snapshot("broker-1", OrderStatus.WORKING, intent),
        _snapshot("broker-foreign", OrderStatus.WORKING, intent),
    )

    report = engine.reconcile_open_orders()

    assert any(issue.code == "unknown_broker_order" for issue in report.issues)
    assert store.load().kill_switch.active
