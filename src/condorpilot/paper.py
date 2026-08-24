"""Broker-agnostic paper-trading safety primitives.

This module deliberately stops short of a real broker adapter. It provides the state,
idempotency, reconciliation, persistence, and account-risk controls that an adapter must obey.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from condorpilot.models import OptionType


class PaperTradingError(RuntimeError):
    """Base error for paper-trading safety failures."""


class SafetyHalt(PaperTradingError):
    """Raised when the kill switch or account-risk gate blocks a new order."""


class IdempotencyConflict(PaperTradingError):
    """Raised when an idempotency key is reused for a different order intent."""


class StateTransitionError(PaperTradingError):
    """Raised when an order attempts an invalid lifecycle transition."""


class ReconciliationError(PaperTradingError):
    """Raised when local and broker state cannot be reconciled safely."""


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class NetPriceEffect(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"


class OrderStatus(StrEnum):
    NEW = "new"
    SUBMITTING = "submitting"
    WORKING = "working"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    ERROR = "error"

    @property
    def terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.ERROR,
        }


@dataclass(frozen=True, slots=True)
class OptionContract:
    symbol: str
    expiration: date
    strike: float
    option_type: OptionType

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol must not be empty")
        if self.strike <= 0 or not math.isfinite(self.strike):
            raise ValueError("strike must be finite and positive")

    @property
    def key(self) -> str:
        return f"{self.symbol.upper()}|{self.expiration}|{self.strike:g}|{self.option_type.value}"


@dataclass(frozen=True, slots=True)
class ComboLeg:
    contract: OptionContract
    side: OrderSide
    ratio: int = 1

    def __post_init__(self) -> None:
        if self.ratio <= 0:
            raise ValueError("leg ratio must be positive")


@dataclass(frozen=True, slots=True)
class PaperOrderIntent:
    idempotency_key: str
    legs: tuple[ComboLeg, ...]
    quantity: int
    limit_price: float
    price_effect: NetPriceEffect
    max_loss_per_combo: float

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if len(self.legs) < 2:
            raise ValueError("combo order must contain at least two legs")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.limit_price < 0 or not math.isfinite(self.limit_price):
            raise ValueError("limit_price must be finite and non-negative")
        if self.max_loss_per_combo <= 0 or not math.isfinite(self.max_loss_per_combo):
            raise ValueError("max_loss_per_combo must be finite and positive")
        symbols = {leg.contract.symbol.upper() for leg in self.legs}
        if len(symbols) != 1:
            raise ValueError("all combo legs must share one underlying symbol")
        keys = [leg.contract.key for leg in self.legs]
        if len(keys) != len(set(keys)):
            raise ValueError("combo order contains duplicate contracts")

    @property
    def symbol(self) -> str:
        return self.legs[0].contract.symbol.upper()

    @property
    def maximum_order_risk(self) -> float:
        return self.max_loss_per_combo * self.quantity

    @property
    def fingerprint(self) -> str:
        payload = {
            "legs": [
                {
                    "contract": leg.contract.key,
                    "side": leg.side.value,
                    "ratio": leg.ratio,
                }
                for leg in self.legs
            ],
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "price_effect": self.price_effect.value,
            "max_loss_per_combo": self.max_loss_per_combo,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class LegFill:
    contract_key: str
    filled_quantity: int

    def __post_init__(self) -> None:
        if self.filled_quantity < 0:
            raise ValueError("filled_quantity must be non-negative")


@dataclass(frozen=True, slots=True)
class BrokerOrderSnapshot:
    broker_order_id: str
    status: OrderStatus
    leg_fills: tuple[LegFill, ...]
    average_net_price: float | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if not self.broker_order_id.strip():
            raise ValueError("broker_order_id must not be empty")
        if self.average_net_price is not None and (
            self.average_net_price < 0 or not math.isfinite(self.average_net_price)
        ):
            raise ValueError("average_net_price must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ManagedOrder:
    intent: PaperOrderIntent
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    broker_order_id: str | None = None
    leg_fills: tuple[LegFill, ...] = ()
    average_net_price: float | None = None
    failure_reason: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    @property
    def has_legging_exposure(self) -> bool:
        """True when legs have not filled in equal combo proportions."""
        fill_by_key = {fill.contract_key: fill.filled_quantity for fill in self.leg_fills}
        progress: list[float] = []
        for leg in self.intent.legs:
            expected = self.intent.quantity * leg.ratio
            actual = fill_by_key.get(leg.contract.key, 0)
            progress.append(actual / expected)
        return bool(progress) and not all(math.isclose(item, progress[0]) for item in progress)


_ALLOWED_TRANSITIONS = {
    OrderStatus.NEW: {OrderStatus.SUBMITTING, OrderStatus.ERROR},
    OrderStatus.SUBMITTING: {
        OrderStatus.WORKING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.ERROR,
    },
    OrderStatus.WORKING: {
        OrderStatus.WORKING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.ERROR,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED,
        OrderStatus.ERROR,
    },
    OrderStatus.CANCEL_PENDING: {
        OrderStatus.CANCEL_PENDING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.ERROR,
    },
    OrderStatus.FILLED: {OrderStatus.FILLED},
    OrderStatus.CANCELLED: {OrderStatus.CANCELLED},
    OrderStatus.REJECTED: {OrderStatus.REJECTED},
    OrderStatus.ERROR: {OrderStatus.ERROR},
}


def _validate_snapshot(order: ManagedOrder, snapshot: BrokerOrderSnapshot) -> None:
    if order.broker_order_id is not None and snapshot.broker_order_id != order.broker_order_id:
        raise ReconciliationError("broker order id changed during reconciliation")
    expected = {
        leg.contract.key: order.intent.quantity * leg.ratio for leg in order.intent.legs
    }
    seen: set[str] = set()
    previous = {item.contract_key: item.filled_quantity for item in order.leg_fills}
    for fill in snapshot.leg_fills:
        if fill.contract_key not in expected:
            raise ReconciliationError(f"broker returned unknown contract {fill.contract_key}")
        if fill.contract_key in seen:
            raise ReconciliationError(f"broker returned duplicate fill {fill.contract_key}")
        if fill.filled_quantity > expected[fill.contract_key]:
            raise ReconciliationError(f"fill exceeds requested quantity for {fill.contract_key}")
        if fill.filled_quantity < previous.get(fill.contract_key, 0):
            raise ReconciliationError(f"fill quantity moved backwards for {fill.contract_key}")
        seen.add(fill.contract_key)


def apply_broker_snapshot(
    order: ManagedOrder,
    snapshot: BrokerOrderSnapshot,
    *,
    observed_at: datetime | None = None,
) -> ManagedOrder:
    """Apply one monotonic broker observation through the guarded state machine."""
    _validate_snapshot(order, snapshot)
    if snapshot.status not in _ALLOWED_TRANSITIONS[order.status]:
        raise StateTransitionError(
            f"invalid order transition {order.status.value} -> {snapshot.status.value}"
        )
    now = observed_at or datetime.now(UTC)
    return replace(
        order,
        status=snapshot.status,
        updated_at=now,
        broker_order_id=snapshot.broker_order_id,
        leg_fills=snapshot.leg_fills,
        average_net_price=snapshot.average_net_price,
        failure_reason=snapshot.message if snapshot.status in {OrderStatus.REJECTED, OrderStatus.ERROR} else None,
    )


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    equity: float
    daily_realized_pnl: float = 0.0
    total_defined_risk: float = 0.0
    symbol_defined_risk: tuple[tuple[str, float], ...] = ()
    open_position_count: int = 0
    working_order_count: int = 0

    def __post_init__(self) -> None:
        if self.equity <= 0 or not math.isfinite(self.equity):
            raise ValueError("equity must be finite and positive")
        if self.total_defined_risk < 0:
            raise ValueError("total_defined_risk must be non-negative")
        if self.open_position_count < 0 or self.working_order_count < 0:
            raise ValueError("position/order counts must be non-negative")
        for _, risk in self.symbol_defined_risk:
            if risk < 0:
                raise ValueError("symbol risk must be non-negative")

    def risk_for(self, symbol: str) -> float:
        wanted = symbol.upper()
        return sum(risk for item, risk in self.symbol_defined_risk if item.upper() == wanted)


@dataclass(frozen=True, slots=True)
class AccountRiskLimits:
    max_order_risk_fraction: float = 0.02
    max_total_defined_risk_fraction: float = 0.10
    max_symbol_defined_risk_fraction: float = 0.04
    max_daily_loss_fraction: float = 0.03
    max_open_positions: int = 5
    max_working_orders: int = 3
    max_contracts_per_order: int = 5

    def __post_init__(self) -> None:
        fractions = (
            self.max_order_risk_fraction,
            self.max_total_defined_risk_fraction,
            self.max_symbol_defined_risk_fraction,
            self.max_daily_loss_fraction,
        )
        if any(not 0 < item <= 1 for item in fractions):
            raise ValueError("risk fractions must be within (0, 1]")
        if min(self.max_open_positions, self.max_working_orders, self.max_contracts_per_order) <= 0:
            raise ValueError("risk count limits must be positive")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...]


def evaluate_order_risk(
    intent: PaperOrderIntent,
    account: AccountSnapshot,
    limits: AccountRiskLimits | None = None,
) -> RiskDecision:
    limits = limits or AccountRiskLimits()
    reasons: list[str] = []
    order_risk = intent.maximum_order_risk
    if order_risk > account.equity * limits.max_order_risk_fraction:
        reasons.append("order defined risk exceeds per-order account limit")
    if account.total_defined_risk + order_risk > account.equity * limits.max_total_defined_risk_fraction:
        reasons.append("aggregate defined risk exceeds account limit")
    if account.risk_for(intent.symbol) + order_risk > account.equity * limits.max_symbol_defined_risk_fraction:
        reasons.append("underlying concentration exceeds symbol risk limit")
    if account.daily_realized_pnl <= -(account.equity * limits.max_daily_loss_fraction):
        reasons.append("daily realized loss limit reached")
    if account.open_position_count >= limits.max_open_positions:
        reasons.append("maximum open position count reached")
    if account.working_order_count >= limits.max_working_orders:
        reasons.append("maximum working order count reached")
    if intent.quantity > limits.max_contracts_per_order:
        reasons.append("order quantity exceeds contract limit")
    return RiskDecision(allowed=not reasons, reasons=tuple(reasons))


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    active: bool = False
    reason: str | None = None
    tripped_at: datetime | None = None


class PaperBroker(Protocol):
    """Minimal adapter contract that a future paper broker integration must implement."""

    def account_snapshot(self) -> AccountSnapshot: ...

    def submit_order(self, intent: PaperOrderIntent) -> BrokerOrderSnapshot: ...

    def get_order(self, broker_order_id: str) -> BrokerOrderSnapshot | None: ...

    def open_orders(self) -> tuple[BrokerOrderSnapshot, ...]: ...

    def cancel_order(self, broker_order_id: str) -> BrokerOrderSnapshot: ...


@dataclass(frozen=True, slots=True)
class PaperState:
    orders: tuple[ManagedOrder, ...] = ()
    kill_switch: KillSwitchState = KillSwitchState()

    def by_key(self, key: str) -> ManagedOrder | None:
        return next((order for order in self.orders if order.intent.idempotency_key == key), None)

    def by_broker_id(self, broker_order_id: str) -> ManagedOrder | None:
        return next((order for order in self.orders if order.broker_order_id == broker_order_id), None)


class PaperStateStore(Protocol):
    def load(self) -> PaperState: ...

    def save(self, state: PaperState) -> None: ...


class InMemoryPaperStateStore:
    def __init__(self, state: PaperState | None = None) -> None:
        self._state = state or PaperState()

    def load(self) -> PaperState:
        return self._state

    def save(self, state: PaperState) -> None:
        self._state = state


def _state_to_dict(state: PaperState) -> dict:
    def order_dict(order: ManagedOrder) -> dict:
        return {
            "intent": {
                "idempotency_key": order.intent.idempotency_key,
                "legs": [
                    {
                        "symbol": leg.contract.symbol,
                        "expiration": leg.contract.expiration.isoformat(),
                        "strike": leg.contract.strike,
                        "option_type": leg.contract.option_type.value,
                        "side": leg.side.value,
                        "ratio": leg.ratio,
                    }
                    for leg in order.intent.legs
                ],
                "quantity": order.intent.quantity,
                "limit_price": order.intent.limit_price,
                "price_effect": order.intent.price_effect.value,
                "max_loss_per_combo": order.intent.max_loss_per_combo,
            },
            "status": order.status.value,
            "created_at": order.created_at.isoformat(),
            "updated_at": order.updated_at.isoformat(),
            "broker_order_id": order.broker_order_id,
            "leg_fills": [asdict(item) for item in order.leg_fills],
            "average_net_price": order.average_net_price,
            "failure_reason": order.failure_reason,
        }

    return {
        "schema_version": 1,
        "orders": [order_dict(order) for order in state.orders],
        "kill_switch": {
            "active": state.kill_switch.active,
            "reason": state.kill_switch.reason,
            "tripped_at": (
                state.kill_switch.tripped_at.isoformat() if state.kill_switch.tripped_at else None
            ),
        },
    }


def _state_from_dict(payload: dict) -> PaperState:
    if payload.get("schema_version") != 1:
        raise PaperTradingError("unsupported paper-state schema")
    orders: list[ManagedOrder] = []
    for raw in payload.get("orders", []):
        intent_raw = raw["intent"]
        legs = tuple(
            ComboLeg(
                contract=OptionContract(
                    symbol=leg["symbol"],
                    expiration=date.fromisoformat(leg["expiration"]),
                    strike=float(leg["strike"]),
                    option_type=OptionType(leg["option_type"]),
                ),
                side=OrderSide(leg["side"]),
                ratio=int(leg["ratio"]),
            )
            for leg in intent_raw["legs"]
        )
        intent = PaperOrderIntent(
            idempotency_key=intent_raw["idempotency_key"],
            legs=legs,
            quantity=int(intent_raw["quantity"]),
            limit_price=float(intent_raw["limit_price"]),
            price_effect=NetPriceEffect(intent_raw["price_effect"]),
            max_loss_per_combo=float(intent_raw["max_loss_per_combo"]),
        )
        orders.append(
            ManagedOrder(
                intent=intent,
                status=OrderStatus(raw["status"]),
                created_at=datetime.fromisoformat(raw["created_at"]),
                updated_at=datetime.fromisoformat(raw["updated_at"]),
                broker_order_id=raw.get("broker_order_id"),
                leg_fills=tuple(LegFill(**item) for item in raw.get("leg_fills", [])),
                average_net_price=raw.get("average_net_price"),
                failure_reason=raw.get("failure_reason"),
            )
        )
    kill = payload.get("kill_switch", {})
    tripped_at = kill.get("tripped_at")
    return PaperState(
        orders=tuple(orders),
        kill_switch=KillSwitchState(
            active=bool(kill.get("active", False)),
            reason=kill.get("reason"),
            tripped_at=datetime.fromisoformat(tripped_at) if tripped_at else None,
        ),
    )


class JsonFilePaperStateStore:
    """Small atomic JSON journal for restart recovery in a single-process paper service."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> PaperState:
        if not self.path.exists():
            return PaperState()
        return _state_from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, state: PaperState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(_state_to_dict(state), indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


@dataclass(frozen=True, slots=True)
class ReconciliationIssue:
    code: str
    message: str
    broker_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    issues: tuple[ReconciliationIssue, ...]
    refreshed_orders: int

    @property
    def clean(self) -> bool:
        return not self.issues


class PaperTradingEngine:
    """Conservative coordinator for a future broker paper adapter."""

    def __init__(
        self,
        broker: PaperBroker,
        store: PaperStateStore,
        *,
        risk_limits: AccountRiskLimits | None = None,
    ) -> None:
        self.broker = broker
        self.store = store
        self.risk_limits = risk_limits or AccountRiskLimits()

    def _replace_order(self, state: PaperState, updated: ManagedOrder) -> PaperState:
        orders = tuple(
            updated if item.intent.idempotency_key == updated.intent.idempotency_key else item
            for item in state.orders
        )
        return replace(state, orders=orders)

    def trip_kill_switch(self, reason: str) -> PaperState:
        if not reason.strip():
            raise ValueError("kill-switch reason must not be empty")
        state = self.store.load()
        updated = replace(
            state,
            kill_switch=KillSwitchState(
                active=True,
                reason=reason,
                tripped_at=datetime.now(UTC),
            ),
        )
        self.store.save(updated)
        return updated

    def reset_kill_switch(self) -> PaperState:
        state = self.store.load()
        updated = replace(state, kill_switch=KillSwitchState())
        self.store.save(updated)
        return updated

    def submit(self, intent: PaperOrderIntent) -> ManagedOrder:
        state = self.store.load()
        if state.kill_switch.active:
            raise SafetyHalt(f"kill switch active: {state.kill_switch.reason}")
        existing = state.by_key(intent.idempotency_key)
        if existing is not None:
            if existing.intent.fingerprint != intent.fingerprint:
                raise IdempotencyConflict("idempotency key reused for a different order")
            return existing

        decision = evaluate_order_risk(intent, self.broker.account_snapshot(), self.risk_limits)
        if not decision.allowed:
            raise SafetyHalt("; ".join(decision.reasons))

        now = datetime.now(UTC)
        order = ManagedOrder(
            intent=intent,
            status=OrderStatus.NEW,
            created_at=now,
            updated_at=now,
        )
        state = replace(state, orders=(*state.orders, order))
        self.store.save(state)

        submitting = replace(order, status=OrderStatus.SUBMITTING, updated_at=datetime.now(UTC))
        state = self._replace_order(state, submitting)
        self.store.save(state)
        try:
            snapshot = self.broker.submit_order(intent)
            updated = apply_broker_snapshot(submitting, snapshot)
        except Exception as exc:
            failed = replace(
                submitting,
                status=OrderStatus.ERROR,
                updated_at=datetime.now(UTC),
                failure_reason=str(exc),
            )
            self.store.save(self._replace_order(state, failed))
            self.trip_kill_switch(f"broker submission failure for {intent.idempotency_key}: {exc}")
            raise

        self.store.save(self._replace_order(state, updated))
        if updated.has_legging_exposure:
            self.trip_kill_switch(
                f"uneven multi-leg fill detected for broker order {updated.broker_order_id}"
            )
        return updated

    def refresh_order(self, idempotency_key: str) -> ManagedOrder:
        state = self.store.load()
        order = state.by_key(idempotency_key)
        if order is None:
            raise KeyError(idempotency_key)
        if order.broker_order_id is None or order.terminal:
            return order
        snapshot = self.broker.get_order(order.broker_order_id)
        if snapshot is None:
            self.trip_kill_switch(f"broker lost non-terminal order {order.broker_order_id}")
            raise ReconciliationError(f"broker order {order.broker_order_id} not found")
        updated = apply_broker_snapshot(order, snapshot)
        self.store.save(self._replace_order(state, updated))
        if updated.has_legging_exposure:
            self.trip_kill_switch(
                f"uneven multi-leg fill detected for broker order {updated.broker_order_id}"
            )
        return updated

    def recover(self) -> ReconciliationReport:
        """Refresh all non-terminal persisted orders after process restart."""
        state = self.store.load()
        issues: list[ReconciliationIssue] = []
        refreshed = 0
        for order in state.orders:
            if order.terminal or order.broker_order_id is None:
                continue
            snapshot = self.broker.get_order(order.broker_order_id)
            if snapshot is None:
                issues.append(
                    ReconciliationIssue(
                        code="missing_broker_order",
                        message="persisted non-terminal order is missing at broker",
                        broker_order_id=order.broker_order_id,
                    )
                )
                continue
            try:
                updated = apply_broker_snapshot(order, snapshot)
            except PaperTradingError as exc:
                issues.append(
                    ReconciliationIssue(
                        code="state_mismatch",
                        message=str(exc),
                        broker_order_id=order.broker_order_id,
                    )
                )
                continue
            state = self._replace_order(state, updated)
            refreshed += 1
            if updated.has_legging_exposure:
                issues.append(
                    ReconciliationIssue(
                        code="legging_exposure",
                        message="multi-leg order has uneven fill progress",
                        broker_order_id=order.broker_order_id,
                    )
                )
        self.store.save(state)
        if issues:
            self.trip_kill_switch("restart reconciliation found unresolved broker/local mismatches")
        return ReconciliationReport(issues=tuple(issues), refreshed_orders=refreshed)

    def reconcile_open_orders(self) -> ReconciliationReport:
        """Compare broker working orders with the local journal and fail closed on drift."""
        state = self.store.load()
        broker_orders = {item.broker_order_id: item for item in self.broker.open_orders()}
        local = {
            item.broker_order_id: item
            for item in state.orders
            if item.broker_order_id is not None and not item.terminal
        }
        issues: list[ReconciliationIssue] = []
        refreshed = 0
        for broker_id in sorted(set(broker_orders) - set(local)):
            issues.append(
                ReconciliationIssue(
                    code="unknown_broker_order",
                    message="broker has an open order absent from local state",
                    broker_order_id=broker_id,
                )
            )
        for broker_id in sorted(set(local) - set(broker_orders)):
            issues.append(
                ReconciliationIssue(
                    code="missing_open_order",
                    message="local non-terminal order is absent from broker open orders",
                    broker_order_id=broker_id,
                )
            )
        for broker_id in sorted(set(local) & set(broker_orders)):
            try:
                updated = apply_broker_snapshot(local[broker_id], broker_orders[broker_id])
            except PaperTradingError as exc:
                issues.append(
                    ReconciliationIssue(
                        code="state_mismatch",
                        message=str(exc),
                        broker_order_id=broker_id,
                    )
                )
                continue
            state = self._replace_order(state, updated)
            refreshed += 1
            if updated.has_legging_exposure:
                issues.append(
                    ReconciliationIssue(
                        code="legging_exposure",
                        message="multi-leg order has uneven fill progress",
                        broker_order_id=broker_id,
                    )
                )
        self.store.save(state)
        if issues:
            self.trip_kill_switch("open-order reconciliation found broker/local drift")
        return ReconciliationReport(issues=tuple(issues), refreshed_orders=refreshed)
