"""Evaluate dice rolls and format compact Telegram replies."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape
import random

from dice_parser import (
    PRECEDENCE,
    UNARY_PRECEDENCE,
    Binary,
    Dice,
    Expression,
    Formula,
    FormulaError,
    MAX_FORMULAS,
    Number,
    Unary,
    calculate,
    find_formulas as find_formulas,
    parse_formula,
)


MAX_DESCRIPTION_LENGTH = 120
MAX_REPLY_LENGTH = 4096
ADDITION = PRECEDENCE["+"]
ATOM = max(PRECEDENCE.values()) + 1
OPERATOR_DISPLAY = {"*": "×", "**": "^"}

RandInt = Callable[[int, int], int]


@dataclass(frozen=True)
class Result:
    value: int | float
    parts: tuple[str, ...]
    precedence: int = ATOM
    dice_sum: bool = False


def _parenthesize(parts: tuple[str, ...], needed: bool) -> tuple[str, ...]:
    return ("(", *parts, ")") if needed else parts


def _roll(dice: Dice, randint: RandInt) -> Result:
    values = [randint(1, dice.sides) for _ in range(dice.count)]
    if not values:
        return Result(0, ("0",))

    if dice.keep:
        order = sorted(range(dice.count), key=values.__getitem__, reverse=dice.keep == "kh")
        selected = set(order[:dice.kept])
    else:
        selected = set(range(dice.count))

    parts = ["["] if dice.keep else []
    separator = ", " if dice.keep else " + "
    for index, value in enumerate(values):
        if index:
            parts.append(separator)
        emoji = "🎲"
        if dice.sides == 20 and index in selected:
            emoji = {1: "💀", 20: "🔥"}.get(value, emoji)
        face = f"{emoji}{value}"
        parts.append(face if index in selected else f"<s>{face}</s>")
    if dice.keep:
        parts.append("]")

    dice_sum = not dice.keep and dice.count > 1
    return Result(
        sum(values[index] for index in selected), tuple(parts), ADDITION if dice_sum else ATOM, dice_sum
    )


def _evaluate(expression: Expression, randint: RandInt) -> Result:
    if isinstance(expression, Number):
        return Result(expression.value, (escape(str(expression.value)),))
    if isinstance(expression, Dice):
        return _roll(expression, randint)
    if isinstance(expression, Unary):
        operand = _evaluate(expression.operand, randint)
        value = -operand.value if expression.symbol == "-" else operand.value
        parts = _parenthesize(operand.parts, operand.precedence < UNARY_PRECEDENCE)
        return Result(value, (expression.symbol, *parts), UNARY_PRECEDENCE)

    assert isinstance(expression, Binary)
    left = _evaluate(expression.left, randint)
    right = _evaluate(expression.right, randint)
    symbol = expression.symbol
    precedence = PRECEDENCE[symbol]
    value = calculate(symbol, left.value, right.value)

    left_parts = _parenthesize(
        left.parts, left.precedence < precedence or (symbol == "**" and left.precedence == precedence)
    )
    flatten_dice_sum = symbol == "+" and right.dice_sum and isinstance(left.value, int)
    wrap_right = right.precedence < precedence or (
        right.precedence == precedence and symbol != "**" and not flatten_dice_sum
    )
    right_parts = _parenthesize(right.parts, wrap_right)
    return Result(value, (*left_parts, f" {OPERATOR_DISPLAY.get(symbol, symbol)} ", *right_parts), precedence)


def _format_formula(formula: Formula, randint: RandInt) -> tuple[str, ...]:
    if len(formula.description) > MAX_DESCRIPTION_LENGTH:
        raise FormulaError(f"Максимальная длина подписи — {MAX_DESCRIPTION_LENGTH} символов.")
    result = _evaluate(parse_formula(formula.text), randint)
    parts = (*result.parts, f" = <b>{escape(str(result.value))}</b>")
    if formula.description:
        parts = (*parts, f" — {escape(formula.description)}")
    return parts


def _split_replies(lines: Sequence[tuple[str, ...]]) -> list[str]:
    # Counting raw HTML in UTF-16 also bounds the rendered Telegram message.
    replies = []
    parts = []
    length = 0
    for line in lines:
        sizes = [len(part.encode("utf-16-le")) // 2 for part in line]
        if parts:
            if length + 1 + sum(sizes) <= MAX_REPLY_LENGTH:
                parts.append("\n")
                length += 1
            else:
                replies.append("".join(parts).strip())
                parts = []
                length = 0
        for part, size in zip(line, sizes):
            if length + size > MAX_REPLY_LENGTH:
                replies.append("".join(parts).strip())
                parts = []
                length = 0
            parts.append(part)
            length += size
    if parts:
        replies.append("".join(parts).strip())
    return replies


def roll_replies(formulas: Sequence[Formula], *, randint: RandInt | None = None) -> list[str]:
    if len(formulas) > MAX_FORMULAS:
        return [f"Максимум {MAX_FORMULAS} формул в сообщении."]
    if randint is None:
        randint = random.randint

    lines = []
    for formula in formulas:
        try:
            lines.append(_format_formula(formula, randint))
        except FormulaError as error:
            lines.append((escape(str(error)),))
    return _split_replies(lines)
