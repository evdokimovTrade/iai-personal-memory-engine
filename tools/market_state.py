#!/usr/bin/env python3
"""Таблица индикаторов → расхождения → логика сделки.

Вход — по одной строке на индикатор, направление обозначается ⬜️ ↗️ ↘️
(принимаются также слова и ascii). Индикаторы: цена, ОИ, спотовая дельта,
фьючерсная дельта, Long/Short Account, NetOE Long, NetOE Short, bid- и
ask-дельты стакана.

Расхождения ищутся правилами, у каждого правила есть ярус:

  1 направление  — состояние ОИ/цена; имеет право вето
  2 подтверждение — расхождения дельт
  3 топливо      — позиционирование толпы
  4 тайминг      — перекос стакана

Ярус 1 — вето, а не вес. Если матрица говорит «не торговать» (состояния 3 и 9)
или фаза не задана, нижние ярусы не могут это перебить: они уточняют вход, а
не создают его.

Веса ярусов — допущение, а не измеренная величина. Они возвращаются вместе с
выводом, чтобы разбор можно было воспроизвести и оспорить.

Неизвестное не равно боковику. Индикатор, которого нет в таблице, попадает в
список недостающего, а правила, которым он нужен, остаются неразрешёнными.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from oi_state import (  # noqa: E402
    DOWN,
    EARLY,
    FLAT,
    LATE,
    UP,
    StateError,
    classify,
)

INDICATORS: dict[str, str] = {
    "price": "Цена",
    "oi": "Открытый интерес",
    "spot_cvd": "Спотовая дельта",
    "futures_cvd": "Фьючерсная дельта",
    "ls_account": "Long/Short Account",
    "nl": "NetOE Long",
    "ns": "NetOE Short",
    "bid_delta": "Bid-дельта стакана",
    "ask_delta": "Ask-дельта стакана",
    # CoinGlass отдаёт не раздельные bid и ask, а один готовый дисбаланс на
    # площадку. Это отдельные индикаторы, а не замена двум предыдущим:
    # источник с раздельными рядами продолжает работать через bid_delta/ask_delta.
    "futures_book_delta": "Bid&Ask дельта фьючерса",
    "spot_book_delta": "Bid&Ask дельта спота",
}

_ALIASES: dict[str, str] = {
    "price": "price", "цена": "price",
    "oi": "oi", "ои": "oi", "openinterest": "oi", "открытыйинтерес": "oi",
    "spot": "spot_cvd", "spotcvd": "spot_cvd", "spotdelta": "spot_cvd",
    "спот": "spot_cvd", "спотоваядельта": "spot_cvd", "дельтаспот": "spot_cvd",
    "futures": "futures_cvd", "futurescvd": "futures_cvd", "perp": "futures_cvd",
    "фьючерс": "futures_cvd", "фьючерснаядельта": "futures_cvd",
    "дельтафьючерс": "futures_cvd", "фьюч": "futures_cvd",
    "ls": "ls_account", "lsaccount": "ls_account", "longshort": "ls_account",
    "longshortaccount": "ls_account", "лонгшорт": "ls_account",
    "соотношениелонгшорт": "ls_account",
    "nl": "nl", "netoelong": "nl", "netoel": "nl",
    "ns": "ns", "netoeshort": "ns", "netoes": "ns",
    "bid": "bid_delta", "biddelta": "bid_delta", "бид": "bid_delta",
    "биддельта": "bid_delta",
    "ask": "ask_delta", "askdelta": "ask_delta", "аск": "ask_delta",
    "аскдельта": "ask_delta",
    "futuresbookdelta": "futures_book_delta", "fbd": "futures_book_delta",
    "фьючбук": "futures_book_delta", "бидаскфьючерс": "futures_book_delta",
    "spotbookdelta": "spot_book_delta", "sbd": "spot_book_delta",
    "спотбук": "spot_book_delta", "бидаскспот": "spot_book_delta",
}

_DIRECTION_TOKENS: dict[str, str] = {
    "⬜": FLAT, "◻": FLAT, "◽": FLAT, "□": FLAT, "=": FLAT, "~": FLAT,
    "flat": FLAT, "боковик": FLAT, "консолидация": FLAT, "бок": FLAT,
    "↗": UP, "⬆": UP, "↑": UP, "+": UP,
    "up": UP, "рост": UP, "вверх": UP, "растет": UP, "растёт": UP,
    "↘": DOWN, "⬇": DOWN, "↓": DOWN, "-": DOWN,
    "down": DOWN, "падение": DOWN, "вниз": DOWN, "падает": DOWN, "снижение": DOWN,
}

_SYMBOL_TOKENS: dict[str, str] = {
    ch: direction for ch, direction in _DIRECTION_TOKENS.items()
    if len(ch) == 1 and not ch.isascii()
}

# Ярус → вес. Допущение, подлежащее проверке на истории.
TIER_WEIGHTS: dict[int, float] = {1: 3.0, 2: 2.0, 3: 1.0, 4: 0.5}

TIER_NAMES: dict[int, str] = {
    1: "направление",
    2: "подтверждение",
    3: "топливо",
    4: "тайминг",
}

LONG: str = "long"
SHORT: str = "short"


class TableError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# разбор таблицы
# --------------------------------------------------------------------------


def _normalize_name(raw: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", raw.lower())


def parse_direction(token: str) -> str | None:
    # Вариационные селекторы и модификаторы эмодзи в ключах не участвуют.
    cleaned = "".join(ch for ch in token if ord(ch) not in (0xFE0F, 0xFE0E, 0x200D))
    cleaned = cleaned.strip().lower()
    if not cleaned:
        return None
    if cleaned in _DIRECTION_TOKENS:
        return _DIRECTION_TOKENS[cleaned]
    # Посимвольный разбор — только для стрелок и квадратов: эмодзи приходят
    # склеенными с текстом. Ascii-формы (+ - = ~) принимаются лишь целым
    # токеном, иначе дефис внутри слова читается как «падение».
    for ch in cleaned:
        if ch in _SYMBOL_TOKENS:
            return _SYMBOL_TOKENS[ch]
    return None


def parse_table(text: str) -> dict[str, str]:
    """Разобрать таблицу вида «цена ↗️» в словарь индикатор → направление."""
    result: dict[str, str] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[:\t]| {2,}", line, maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            name_part, dir_part = parts[0], parts[1]
        else:
            tokens = line.split()
            if len(tokens) < 2:
                raise TableError(f"строка {line_no}: не разобрана — {line!r}")
            name_part, dir_part = " ".join(tokens[:-1]), tokens[-1]

        key = _ALIASES.get(_normalize_name(name_part))
        if key is None:
            raise TableError(f"строка {line_no}: индикатор {name_part.strip()!r} не распознан")
        direction = parse_direction(dir_part)
        if direction is None:
            raise TableError(f"строка {line_no}: направление {dir_part.strip()!r} не распознано")
        if key in result and result[key] != direction:
            raise TableError(
                f"строка {line_no}: {INDICATORS[key]} задан дважды с разными значениями"
            )
        result[key] = direction
    return result


# --------------------------------------------------------------------------
# модель
# --------------------------------------------------------------------------


@dataclass
class Snapshot:
    timeframe: str
    values: dict[str, str]
    phase: str | None = None

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    @property
    def missing(self) -> list[str]:
        return [INDICATORS[k] for k in INDICATORS if k not in self.values]


@dataclass
class Finding:
    rule: str
    tier: int
    bias: str | None
    reading: str
    inputs: dict[str, str | None]
    resolved: bool = True
    caution: str | None = None

    @property
    def weight(self) -> float:
        return TIER_WEIGHTS[self.tier] if (self.resolved and self.bias) else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "tier": self.tier,
            "tier_name": TIER_NAMES[self.tier],
            "bias": self.bias,
            "reading": self.reading,
            "inputs": dict(self.inputs),
            "resolved": self.resolved,
            "caution": self.caution,
            "weight": self.weight,
        }


def _pending(rule: str, tier: int, inputs: dict[str, str | None]) -> Finding | None:
    absent = [INDICATORS[k] for k, v in inputs.items() if v is None]
    if not absent:
        return None
    return Finding(
        rule, tier, None, f"нет данных: {', '.join(absent)}", inputs, resolved=False
    )


# --------------------------------------------------------------------------
# правила
# --------------------------------------------------------------------------


def rule_state_matrix(snap: Snapshot, flat_threshold_pct: float) -> Finding:
    """Ярус 1. Состояние ОИ/цена по матрице девяти состояний."""
    inputs = {"price": snap.get("price"), "oi": snap.get("oi")}
    pending = _pending("Состояние ОИ/цена", 1, inputs)
    if pending is not None:
        return pending

    # Направления уже категориальные, поэтому в классификатор подаются
    # представители своих зон, а не измеренные проценты.
    proxy = {UP: flat_threshold_pct * 10, DOWN: -flat_threshold_pct * 10, FLAT: 0.0}
    verdict = classify(
        proxy[snap.values["oi"]],
        proxy[snap.values["price"]],
        flat_threshold_pct=flat_threshold_pct,
        phase=snap.phase,
    )
    return Finding(
        rule=f"Состояние {verdict.state}: {verdict.label}",
        tier=1,
        bias=verdict.direction,
        reading=f"{verdict.action} — {verdict.reason}",
        inputs=inputs,
        resolved=verdict.tradable or verdict.state in (2, 3, 9),
        caution="; ".join(verdict.cautions) or None,
    )


def rule_oi_vs_futures(snap: Snapshot) -> Finding:
    """Ярус 2. ОИ против фьючерсной дельты."""
    inputs = {"oi": snap.get("oi"), "futures_cvd": snap.get("futures_cvd")}
    pending = _pending("ОИ против фьючерсной дельты", 2, inputs)
    if pending is not None:
        return pending
    oi, fut = snap.values["oi"], snap.values["futures_cvd"]
    if oi == UP and fut == DOWN:
        return Finding("ОИ против фьючерсной дельты", 2, SHORT,
                       "позиции набираются под давлением продавца — накопление под пробой вниз",
                       inputs)
    if oi == DOWN and fut == UP:
        return Finding("ОИ против фьючерсной дельты", 2, LONG,
                       "позиции закрываются на растущей дельте — топливо для сквиза вверх",
                       inputs)
    return Finding("ОИ против фьючерсной дельты", 2, None,
                   "сонаправлены или в боковике: расхождения нет", inputs)


def rule_spot_vs_futures(snap: Snapshot) -> Finding:
    """Ярус 2. Спотовая дельта против фьючерсной."""
    inputs = {"spot_cvd": snap.get("spot_cvd"), "futures_cvd": snap.get("futures_cvd")}
    pending = _pending("Спот против фьючерса", 2, inputs)
    if pending is not None:
        return pending
    spot, fut = snap.values["spot_cvd"], snap.values["futures_cvd"]
    if fut == UP and spot == DOWN:
        return Finding("Спот против фьючерса", 2, SHORT,
                       "покупка только на деривативах при слабом споте — ложный пробой вверх",
                       inputs)
    if fut == DOWN and spot == UP:
        return Finding("Спот против фьючерса", 2, LONG,
                       "реальный спрос на споте против фьючерсной манипуляции — ложный пробой вниз",
                       inputs)
    return Finding("Спот против фьючерса", 2, None, "дельты сонаправлены", inputs)


def rule_price_vs_spot(snap: Snapshot) -> Finding:
    """Ярус 2. Цена против спотовой дельты — есть ли реальный спрос."""
    inputs = {"price": snap.get("price"), "spot_cvd": snap.get("spot_cvd")}
    pending = _pending("Цена против спотовой дельты", 2, inputs)
    if pending is not None:
        return pending
    price, spot = snap.values["price"], snap.values["spot_cvd"]
    if price == UP and spot == DOWN:
        return Finding("Цена против спотовой дельты", 2, SHORT,
                       "рост не обеспечен реальными покупками на споте", inputs)
    if price == DOWN and spot == UP:
        return Finding("Цена против спотовой дельты", 2, LONG,
                       "падение выкупается на споте — накопление", inputs)
    if price == spot and price in (UP, DOWN):
        return Finding("Цена против спотовой дельты", 2, LONG if price == UP else SHORT,
                       "спот подтверждает движение цены", inputs)
    if price == FLAT and spot == UP:
        return Finding("Цена против спотовой дельты", 2, SHORT,
                       "спотовые покупки не двигают цену — их поглощают, идёт распределение",
                       inputs)
    if price == FLAT and spot == DOWN:
        return Finding("Цена против спотовой дельты", 2, LONG,
                       "спотовые продажи не двигают цену — их поглощают, идёт накопление",
                       inputs)
    return Finding("Цена против спотовой дельты", 2, None,
                   "спот в боковике: поглощения не видно", inputs)


def rule_price_vs_futures(snap: Snapshot) -> Finding:
    """Ярус 2. Цена против фьючерсной дельты — чем оплачено движение."""
    inputs = {"price": snap.get("price"), "futures_cvd": snap.get("futures_cvd")}
    pending = _pending("Цена против фьючерсной дельты", 2, inputs)
    if pending is not None:
        return pending
    price, fut = snap.values["price"], snap.values["futures_cvd"]
    if price == UP and fut == DOWN:
        return Finding("Цена против фьючерсной дельты", 2, SHORT,
                       "рост на закрытии шортов, а не на покупках — топливо конечно", inputs,
                       caution="до истощения сквиза движение вверх может продолжаться")
    if price == DOWN and fut == UP:
        return Finding("Цена против фьючерсной дельты", 2, SHORT,
                       "агрессивные покупки поглощаются продавцом — покупатель слаб", inputs)
    if price == fut and price in (UP, DOWN):
        return Finding("Цена против фьючерсной дельты", 2, LONG if price == UP else SHORT,
                       "фьючерс подтверждает движение цены", inputs)
    if price == FLAT and fut == UP:
        return Finding("Цена против фьючерсной дельты", 2, SHORT,
                       "агрессивные покупки на фьючерсе не двигают цену — их поглощают",
                       inputs)
    if price == FLAT and fut == DOWN:
        return Finding("Цена против фьючерсной дельты", 2, LONG,
                       "агрессивные продажи на фьючерсе не двигают цену — их поглощают",
                       inputs)
    return Finding("Цена против фьючерсной дельты", 2, None,
                   "фьючерсная дельта в боковике: поглощения не видно", inputs)


def rule_crowd(snap: Snapshot) -> Finding:
    """Ярус 3. Позиционирование толпы против цены.

    Логика изъятия ликвидности: сторона, набранная толпой, и есть та, которую
    выносят. Правило читает толпу против движения, а не за ним.
    """
    inputs = {"price": snap.get("price"), "ls_account": snap.get("ls_account")}
    pending = _pending("Толпа против цены", 3, inputs)
    if pending is not None:
        return pending
    price, ls = snap.values["price"], snap.values["ls_account"]
    if price == UP and ls == DOWN:
        return Finding("Толпа против цены", 3, LONG,
                       "толпа шортит рост — топливо для сквиза вверх", inputs)
    if price == UP and ls == UP:
        return Finding("Толпа против цены", 3, SHORT,
                       "толпа лонгует рост — перегрев, выносить будут лонги", inputs)
    if price == DOWN and ls == UP:
        return Finding("Толпа против цены", 3, SHORT,
                       "толпа ловит нож — топливо для продолжения пролива", inputs)
    if price == DOWN and ls == DOWN:
        return Finding("Толпа против цены", 3, LONG,
                       "толпа сдалась — выносить некого, вероятен отскок", inputs)
    return Finding("Толпа против цены", 3, None, "боковик по одной из величин", inputs)


def rule_netoe(snap: Snapshot) -> Finding:
    """Ярус 3. NetOE Long против NetOE Short.

    Правило не выводится: знаковая договорённость NetOE не задана. Рост NL —
    это набор лонгов или чистый приток в лонги за вычетом закрытий? От ответа
    зависит направление вывода, поэтому вывод не делается.
    """
    inputs = {"nl": snap.get("nl"), "ns": snap.get("ns")}
    pending = _pending("NetOE Long против NetOE Short", 3, inputs)
    if pending is not None:
        return pending
    return Finding(
        "NetOE Long против NetOE Short", 3, None,
        f"NL {snap.values['nl']}, NS {snap.values['ns']} — "
        f"правило не определено: не задана знаковая договорённость NetOE",
        inputs, resolved=False,
    )


def rule_orderbook(snap: Snapshot) -> Finding:
    """Ярус 4. Перекос стакана."""
    inputs = {"bid_delta": snap.get("bid_delta"), "ask_delta": snap.get("ask_delta")}
    pending = _pending("Перекос стакана", 4, inputs)
    if pending is not None:
        return pending
    bid, ask = snap.values["bid_delta"], snap.values["ask_delta"]
    if bid == UP and ask == DOWN:
        return Finding("Перекос стакана", 4, LONG, "бид набирается, аск снимается", inputs,
                       caution="стакан переставляется мгновенно; только тайминг, не направление")
    if bid == DOWN and ask == UP:
        return Finding("Перекос стакана", 4, SHORT, "аск набирается, бид снимается", inputs,
                       caution="стакан переставляется мгновенно; только тайминг, не направление")
    return Finding("Перекос стакана", 4, None, "перекоса нет", inputs)


def rule_book_venues(snap: Snapshot) -> Finding:
    """Ярус 4. Дисбаланс стакана: фьючерс против спота.

    Для источников, отдающих один готовый Bid&Ask-дисбаланс на площадку
    (CoinGlass), а не раздельные бид и аск.
    """
    inputs = {
        "futures_book_delta": snap.get("futures_book_delta"),
        "spot_book_delta": snap.get("spot_book_delta"),
    }
    pending = _pending("Стакан: фьючерс против спота", 4, inputs)
    if pending is not None:
        return pending
    fut, spot = snap.values["futures_book_delta"], snap.values["spot_book_delta"]
    caution = "стакан переставляется мгновенно; только тайминг, не направление"
    if fut == DOWN and spot == UP:
        return Finding("Стакан: фьючерс против спота", 4, LONG,
                       "спотовый бид держит против давления на фьючерсе", inputs,
                       caution=caution)
    if fut == UP and spot == DOWN:
        return Finding("Стакан: фьючерс против спота", 4, SHORT,
                       "фьючерсный бид не подкреплён спотом", inputs, caution=caution)
    return Finding("Стакан: фьючерс против спота", 4, None,
                   "площадки сонаправлены: расхождения нет", inputs)


ALL_RULES = (
    rule_oi_vs_futures,
    rule_spot_vs_futures,
    rule_price_vs_spot,
    rule_price_vs_futures,
    rule_crowd,
    rule_netoe,
    rule_orderbook,
    rule_book_venues,
)


# --------------------------------------------------------------------------
# анализ одного ТФ
# --------------------------------------------------------------------------


@dataclass
class Analysis:
    timeframe: str
    findings: list[Finding]
    missing: list[str]
    veto: str | None
    bias: str | None
    score_long: float
    score_short: float
    divergences: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "bias": self.bias,
            "veto": self.veto,
            "score_long": self.score_long,
            "score_short": self.score_short,
            "missing": list(self.missing),
            "findings": [f.to_dict() for f in self.findings],
        }


_CONFIRMING = ("подтверждает",)


def analyze(snap: Snapshot, flat_threshold_pct: float = 1.0) -> Analysis:
    state = rule_state_matrix(snap, flat_threshold_pct)
    findings = [state] + [rule(snap) for rule in ALL_RULES]

    veto: str | None = None
    if not state.resolved:
        veto = state.reading
    elif state.bias is None and state.resolved:
        veto = f"{state.rule} — {state.reading}"

    score_long = sum(f.weight for f in findings if f.bias == LONG)
    score_short = sum(f.weight for f in findings if f.bias == SHORT)

    if veto is not None:
        bias = None
    elif score_long > score_short:
        bias = LONG
    elif score_short > score_long:
        bias = SHORT
    else:
        bias = None

    divergences = [
        f for f in findings
        if f.resolved and f.bias and not any(m in f.reading for m in _CONFIRMING) and f.tier != 1
    ]

    return Analysis(
        timeframe=snap.timeframe,
        findings=findings,
        missing=snap.missing,
        veto=veto,
        bias=bias,
        score_long=score_long,
        score_short=score_short,
        divergences=divergences,
    )


# --------------------------------------------------------------------------
# сведение двух таймфреймов
# --------------------------------------------------------------------------


@dataclass
class TradeLogic:
    direction: str | None
    entry_allowed: bool
    verdict: str
    senior: Analysis
    junior: Analysis | None
    missing: list[str]
    questions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "entry_allowed": self.entry_allowed,
            "verdict": self.verdict,
            "senior": self.senior.to_dict(),
            "junior": self.junior.to_dict() if self.junior else None,
            "missing": list(self.missing),
            "questions": list(self.questions),
        }


def combine(senior: Analysis, junior: Analysis | None) -> TradeLogic:
    """Старший ТФ задаёт направление, младший — подтверждает вход.

    Сделка против старшего ТФ не выдаётся: младший может только подтвердить
    направление старшего или отложить вход, но не развернуть его.
    """
    missing = sorted({*senior.missing, *(junior.missing if junior else [])})
    questions = [f"Направление по индикатору: {m}" for m in missing]

    if senior.veto is not None:
        return TradeLogic(None, False, f"Старший ТФ: {senior.veto}", senior, junior,
                          missing, questions)
    if senior.bias is None:
        return TradeLogic(None, False, "Старший ТФ: перевеса нет, направление не определено",
                          senior, junior, missing, questions)
    if junior is None:
        return TradeLogic(senior.bias, False,
                          f"Направление {senior.bias} по старшему ТФ; младший ТФ не задан — "
                          f"вход не подтверждён",
                          senior, junior, missing,
                          questions + ["Таблица по младшему ТФ для подтверждения входа"])
    if junior.veto is not None:
        return TradeLogic(senior.bias, False,
                          f"Направление {senior.bias} по старшему ТФ; младший ТФ: {junior.veto} — "
                          f"ждать",
                          senior, junior, missing, questions)
    if junior.bias == senior.bias:
        return TradeLogic(senior.bias, True,
                          f"Направление {senior.bias}: старший и младший ТФ согласны — "
                          f"вход подтверждён",
                          senior, junior, missing, questions)
    return TradeLogic(senior.bias, False,
                      f"Направление {senior.bias} по старшему ТФ, младший ТФ показывает "
                      f"{junior.bias or 'отсутствие перевеса'} — ждать подтверждения, "
                      f"против старшего не входить",
                      senior, junior, missing, questions)


# --------------------------------------------------------------------------
# вывод
# --------------------------------------------------------------------------


def render(logic: TradeLogic) -> str:
    out: list[str] = []
    for analysis, role in ((logic.senior, "Старший ТФ"), (logic.junior, "Младший ТФ")):
        if analysis is None:
            continue
        out.append(f"## {role} — {analysis.timeframe}")
        out.append("")
        for f in analysis.findings:
            mark = {LONG: "лонг", SHORT: "шорт"}.get(f.bias or "", "—")
            status = "" if f.resolved else " [не разрешено]"
            out.append(f"- [{TIER_NAMES[f.tier]}] {f.rule}: {mark}{status} — {f.reading}")
            if f.caution:
                out.append(f"  осторожно: {f.caution}")
        out.append("")
        out.append(f"Перевес: лонг {analysis.score_long:g} / шорт {analysis.score_short:g}")
        if analysis.missing:
            out.append(f"Нет данных: {', '.join(analysis.missing)}")
        out.append("")

    out.append("## Логика сделки")
    out.append("")
    out.append(logic.verdict)
    out.append(f"Направление: {logic.direction or 'не определено'}")
    out.append(f"Вход: {'подтверждён' if logic.entry_allowed else 'не подтверждён'}")
    if logic.questions:
        out.append("")
        out.append("Чего недостаёт:")
        for q in logic.questions:
            out.append(f"- {q}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="market_state",
        description="таблица индикаторов → расхождения → логика сделки",
    )
    parser.add_argument("--senior", required=True, help="файл с таблицей старшего ТФ или '-'")
    parser.add_argument("--senior-tf", default="старший", help="подпись ТФ, например 4h")
    parser.add_argument("--senior-phase", choices=[EARLY, LATE])
    parser.add_argument("--junior", help="файл с таблицей младшего ТФ")
    parser.add_argument("--junior-tf", default="младший")
    parser.add_argument("--junior-phase", choices=[EARLY, LATE])
    parser.add_argument("--flat-threshold", type=float, default=1.0)
    parser.add_argument("--json", action="store_true")
    return parser


def _read(source: str) -> str:
    return sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        senior = analyze(
            Snapshot(args.senior_tf, parse_table(_read(args.senior)), args.senior_phase),
            args.flat_threshold,
        )
        junior = None
        if args.junior:
            junior = analyze(
                Snapshot(args.junior_tf, parse_table(_read(args.junior)), args.junior_phase),
                args.flat_threshold,
            )
        logic = combine(senior, junior)
    except (TableError, StateError) as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(logic.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render(logic))
    return 0 if logic.entry_allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
