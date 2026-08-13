#!/usr/bin/env python3
"""Доминации: разложение движения и стадия ротации капитала.

Доминация — отношение, а не цена. Она может вырасти тремя разными способами,
и на графике доминации они выглядят одинаково:

1. Капитализация монеты выросла, рынок стоял   → реальный приток.
2. Капитализация стояла, рынок сжался          → чисто механический рост.
3. Капитализация упала, рынок упал сильнее     → рост доминации при падении монеты.

Различать их обязательно, иначе «доминация растёт» читается как «деньги идут
в монету», хотя денег могло не быть вовсе. Разложение делается через
тождество, а не на глаз:

    D = M / T        →        D_new/D_old = (M_new/M_old) / (T_new/T_old)

откуда изменение капитализации монеты восстанавливается однозначно:

    M_new/M_old = (D_new/D_old) * (T_new/T_old)

Отсюда же следует ловушка OTHERS.D: он считается от TOTAL, куда входит ETH.
Падение ETH сжимает знаменатель и поднимает OTHERS.D, даже если в мелкие
альты не пришло ни доллара.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, Sequence

UP: str = "рост"
DOWN: str = "падение"
FLAT: str = "боковик"


class DominanceError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# разложение движения доминации
# --------------------------------------------------------------------------


@dataclass
class Decomposition:
    dominance_change_pct: float
    total_change_pct: float
    mcap_change_pct: float
    driver: str
    reading: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def decompose(
    dom_before: float,
    dom_after: float,
    total_before: float,
    total_after: float,
    flat_threshold_pct: float = 0.5,
) -> Decomposition:
    """Разложить движение доминации на приток в монету и сжатие рынка."""
    for name, value in (
        ("доминация до", dom_before), ("доминация после", dom_after),
        ("TOTAL до", total_before), ("TOTAL после", total_after),
    ):
        if value <= 0:
            raise DominanceError(f"{name} должна быть положительной, получено {value!r}")

    dom_ratio = dom_after / dom_before
    total_ratio = total_after / total_before
    mcap_ratio = dom_ratio * total_ratio

    d_pct = (dom_ratio - 1) * 100
    t_pct = (total_ratio - 1) * 100
    m_pct = (mcap_ratio - 1) * 100

    def direction(pct: float) -> str:
        if abs(pct) <= flat_threshold_pct:
            return FLAT
        return UP if pct > 0 else DOWN

    d_dir, t_dir, m_dir = direction(d_pct), direction(t_pct), direction(m_pct)

    if d_dir == FLAT:
        driver = "без изменений"
        reading = "доля не изменилась в пределах порога"
    elif d_dir == UP and m_dir == UP and t_dir in (DOWN, FLAT):
        driver = "реальный приток"
        reading = "капитализация монеты растёт, рынок не растёт — доля набирается деньгами"
    elif d_dir == UP and m_dir == UP:
        driver = "приток быстрее рынка"
        reading = "растут и монета, и рынок, но монета быстрее"
    elif d_dir == UP and m_dir == FLAT:
        driver = "механический рост"
        reading = "капитализация монеты не изменилась — доля выросла из-за сжатия рынка"
    elif d_dir == UP and m_dir == DOWN:
        driver = "рост доли на падении"
        reading = "монета падает, но рынок падает сильнее — доля растёт при оттоке денег"
    elif d_dir == DOWN and m_dir == DOWN and t_dir in (UP, FLAT):
        driver = "реальный отток"
        reading = "капитализация монеты падает, рынок не падает — деньги уходят из монеты"
    elif d_dir == DOWN and m_dir == DOWN:
        driver = "отток быстрее рынка"
        reading = "падают и монета, и рынок, но монета быстрее"
    elif d_dir == DOWN and m_dir == FLAT:
        driver = "механическое падение"
        reading = "капитализация монеты не изменилась — доля упала из-за роста рынка"
    else:
        driver = "падение доли на росте"
        reading = "монета растёт, но рынок растёт сильнее — доля падает при притоке денег"

    return Decomposition(d_pct, t_pct, m_pct, driver, reading)


# --------------------------------------------------------------------------
# квадранты «цена × доминация»
# --------------------------------------------------------------------------


@dataclass
class Quadrant:
    number: int
    label: str
    regime: str
    alt_stance: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


_QUADRANTS: dict[tuple[str, str], Quadrant] = {
    (UP, UP): Quadrant(
        1, "BTC растёт, доля растёт",
        "деньги входят в биткоин, альты отстают или льются",
        "альты держать невыгодно: в BTC они дешевеют",
    ),
    (UP, DOWN): Quadrant(
        2, "BTC растёт, доля падает",
        "деньги входят широко, альты обгоняют биткоин",
        "фаза альтов — единственный квадрант, где альт обгоняет BTC на росте",
    ),
    (DOWN, UP): Quadrant(
        3, "BTC падает, доля растёт",
        "уход от риска внутри крипты, альты падают быстрее биткоина",
        "альты худший держатель: падают и в долларе, и в BTC",
    ),
    (DOWN, DOWN): Quadrant(
        4, "BTC падает, доля падает",
        "биткоин слабеет, альты держатся — поздняя стадия или локальная слабость BTC",
        "редкий и неустойчивый режим; часто предшествует общему проливу",
    ),
}


def quadrant(price_dir: str, dominance_dir: str) -> Quadrant | None:
    """Режим по паре «направление цены BTC × направление BTC.D»."""
    for name, value in (("цена", price_dir), ("доминация", dominance_dir)):
        if value not in (UP, DOWN, FLAT):
            raise DominanceError(f"{name}: направление {value!r} не распознано")
    if price_dir == FLAT or dominance_dir == FLAT:
        return None
    return _QUADRANTS[(price_dir, dominance_dir)]


# --------------------------------------------------------------------------
# стадия ротации капитала
# --------------------------------------------------------------------------

ROTATION_ORDER: tuple[str, ...] = ("BTC", "ETH", "крупные альты", "мелкие альты")


@dataclass
class RotationRead:
    stage: str | None
    confirmed: list[str]
    missing: list[str]
    contradictions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def rotation_stage(
    btc_d: str | None,
    ethbtc: str | None,
    total3: str | None,
    others_d: str | None,
) -> RotationRead:
    """Стадия ротации по четырём признакам.

    Последовательность притока: BTC → ETH → крупные альты → мелкие альты.
    Признаки полной ротации: BTC.D вниз, ETH/BTC вверх, TOTAL3 вверх,
    OTHERS.D вверх. Голосования нет — несовпадение называется, а не
    усредняется.
    """
    inputs = {
        "BTC.D": btc_d, "ETH/BTC": ethbtc, "TOTAL3": total3, "OTHERS.D": others_d,
    }
    missing = [k for k, v in inputs.items() if v is None]

    confirmed: list[str] = []
    contradictions: list[str] = []

    if btc_d == DOWN:
        confirmed.append("BTC.D снижается — доля биткоина уступает")
    elif btc_d == UP:
        contradictions.append("BTC.D растёт — капитал концентрируется в биткоине")

    if ethbtc == UP:
        confirmed.append("ETH/BTC растёт — мост в риск открыт")
    elif ethbtc == DOWN:
        contradictions.append("ETH/BTC падает — ETH не ведёт ротацию")

    if total3 == UP:
        confirmed.append("TOTAL3 растёт — капитализация альтов без BTC и ETH прибавляет")
    elif total3 == DOWN:
        contradictions.append("TOTAL3 падает — альты в абсолюте сжимаются")

    if others_d == UP:
        confirmed.append("OTHERS.D растёт — участвует широкий рынок")
    elif others_d == DOWN:
        contradictions.append("OTHERS.D падает — широкий рынок не участвует")

    if missing:
        stage = None
    elif len(confirmed) == 4:
        stage = "мелкие альты"
    elif btc_d == DOWN and ethbtc == UP and total3 != UP:
        stage = "ETH"
    elif btc_d == UP and ethbtc != UP:
        stage = "BTC"
    elif len(confirmed) >= 2:
        stage = "крупные альты"
    else:
        stage = "BTC"

    return RotationRead(stage, confirmed, missing, contradictions)


def others_d_trap(
    eth_mcap_change_pct: float,
    others_mcap_change_pct: float | None,
) -> dict[str, Any]:
    """Проверка, не механический ли рост OTHERS.D.

    OTHERS.D = OTHERS / TOTAL, а ETH сидит в TOTAL. Падение ETH сжимает
    знаменатель и поднимает OTHERS.D без единого доллара в мелкие альты.
    Ответ даёт только абсолютный OTHERS, а не его доля.
    """
    if others_mcap_change_pct is None:
        return {
            "resolved": False,
            "reason": (
                "нет абсолютного OTHERS: по одной доле отличить приток от сжатия "
                "знаменателя невозможно"
            ),
            "eth_mcap_change_pct": eth_mcap_change_pct,
        }
    if others_mcap_change_pct > 0:
        verdict = "реальный приток в мелкие альты"
    elif eth_mcap_change_pct < 0:
        verdict = "механический рост доли: OTHERS не вырос, знаменатель сжался падением ETH"
    else:
        verdict = "OTHERS не вырос, при этом ETH не падал — искать причину в остальных top-10"
    return {
        "resolved": True,
        "verdict": verdict,
        "eth_mcap_change_pct": eth_mcap_change_pct,
        "others_mcap_change_pct": others_mcap_change_pct,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


_DIRS = {"up": UP, "down": DOWN, "flat": FLAT}


def _dir(raw: str | None) -> str | None:
    if raw is None:
        return None
    if raw not in _DIRS:
        raise DominanceError(f"направление {raw!r} не распознано")
    return _DIRS[raw]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dominance", description="разложение доминаций и стадия ротации"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_dec = sub.add_parser("decompose", help="разложить движение доминации")
    p_dec.add_argument("--dom-before", type=float, required=True)
    p_dec.add_argument("--dom-after", type=float, required=True)
    p_dec.add_argument("--total-before", type=float, required=True)
    p_dec.add_argument("--total-after", type=float, required=True)
    p_dec.add_argument("--flat-threshold", type=float, default=0.5)

    p_q = sub.add_parser("quadrant", help="режим по цене и доминации")
    p_q.add_argument("--price", required=True, choices=list(_DIRS))
    p_q.add_argument("--dominance", required=True, choices=list(_DIRS))

    p_r = sub.add_parser("rotation", help="стадия ротации капитала")
    p_r.add_argument("--btc-d", choices=list(_DIRS))
    p_r.add_argument("--ethbtc", choices=list(_DIRS))
    p_r.add_argument("--total3", choices=list(_DIRS))
    p_r.add_argument("--others-d", choices=list(_DIRS))

    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "decompose":
            result = decompose(
                args.dom_before, args.dom_after,
                args.total_before, args.total_after, args.flat_threshold,
            )
            payload: Any = result.to_dict()
            if not args.json:
                print(f"Доминация: {result.dominance_change_pct:+.2f}%")
                print(f"TOTAL: {result.total_change_pct:+.2f}%")
                print(f"Капитализация монеты (восстановлена): {result.mcap_change_pct:+.2f}%")
                print(f"Причина: {result.driver} — {result.reading}")
        elif args.command == "quadrant":
            q = quadrant(_dir(args.price), _dir(args.dominance))
            payload = q.to_dict() if q else {"quadrant": None,
                                             "reason": "боковик по одной из величин"}
            if not args.json:
                if q is None:
                    print("Квадрант не определён: боковик по одной из величин")
                else:
                    print(f"Квадрант {q.number}: {q.label}")
                    print(f"Режим: {q.regime}")
                    print(f"Альты: {q.alt_stance}")
        else:
            read = rotation_stage(
                _dir(args.btc_d), _dir(args.ethbtc),
                _dir(args.total3), _dir(args.others_d),
            )
            payload = read.to_dict()
            if not args.json:
                print(f"Стадия: {read.stage or 'не определена'}")
                for c in read.confirmed:
                    print(f"  за: {c}")
                for c in read.contradictions:
                    print(f"  против: {c}")
                if read.missing:
                    print(f"  нет данных: {', '.join(read.missing)}")
    except DominanceError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
