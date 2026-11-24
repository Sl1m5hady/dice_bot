"""Find dice expressions in chat text and parse a restricted arithmetic grammar."""

from collections.abc import Iterator
from dataclasses import dataclass
import math
import operator
import re


DICE_SIDES = (4, 6, 8, 10, 12, 20, 100)
MAX_EXPRESSION_LENGTH = 200
MAX_DICE_PER_TERM = 100
MAX_DICE_PER_EXPRESSION = 500
MAX_FORMULAS = 5
MAX_DEPTH = 20
MAX_MAGNITUDE = 10**100
MAX_EXPONENT = 100
DESCRIPTION_SEPARATOR = " — "
LIST_MARKER = re.compile(r"^[ \t]*[0-9]+([.)])[ \t]+")

KEEP_NOTATION = r"([kK][hHlL])([0-9]+)?"
TOKEN_RE = re.compile(
    r"(?P<dice>[0-9]*[dDкК][0-9]+\w*)"
    r"|(?P<number>[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
    r"|(?P<operator>\*\*|//|[+*/()-])"
    r"|(?P<word>\w+)"
    r"|(?P<invalid>!=|[%^=&|~<>\[\]{}\\@$×÷−·⋅])"
    r"|(?P<separator>[!?]+|\S)"
)
DICE_RE = re.compile(rf"([0-9]*)[dDкК]([0-9]+)(?:{KEEP_NOTATION})?")
KEEP_RE = re.compile(KEEP_NOTATION)

PRECEDENCE = {"+": 1, "-": 1, "*": 2, "/": 2, "//": 2, "**": 4}
UNARY_PRECEDENCE = 3
OPERATIONS = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "//": operator.floordiv,
}


class FormulaError(ValueError):
    """A user-facing mistake or limit in a dice expression."""


@dataclass(frozen=True)
class Formula:
    text: str
    description: str = ""


@dataclass(frozen=True)
class Number:
    value: int | float


@dataclass(frozen=True)
class Dice:
    sides: int
    count: int
    keep: str | None
    kept: int


@dataclass(frozen=True)
class Unary:
    symbol: str
    operand: "Expression"


@dataclass(frozen=True)
class Binary:
    symbol: str
    left: "Expression"
    right: "Expression"


Expression = Number | Dice | Unary | Binary


def _formula_spans(text: str) -> Iterator[tuple[int, int]]:
    tokens = list(TOKEN_RE.finditer(text))
    parentheses = []
    start = end = 0
    has_dice = False
    for index, token in enumerate(tokens):
        kind = token.lastgroup
        if token.group() == "(":
            previous = tokens[index - 1] if index else None
            attached = previous is not None and previous.lastgroup == "dice" and previous.end() == token.start()
            prose = not attached and index + 1 < len(tokens) and tokens[index + 1].lastgroup == "word"
            parentheses.append(prose)
            if prose:
                kind = "separator"
        elif token.group() == ")" and parentheses and parentheses.pop():
            kind = "separator"
        elif kind == "separator" and token.group()[0] in "!?" and start != end:
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if following is not None and (
                following.group() in PRECEDENCE
                or (following.start() == token.end() and following.lastgroup in {"dice", "number", "operator"})
            ):
                kind = "invalid"
        if kind == "separator" or (kind == "word" and token.start() != end):
            if has_dice:
                yield start, end
            start = end = token.end()
            has_dice = False
            continue

        if start == end:
            if kind == "word":
                start = end = token.end()
                continue
            start = token.start()
        end = token.end()
        has_dice = has_dice or kind == "dice"

    if has_dice:
        yield start, end


def find_formulas(text: str) -> list[Formula]:
    formulas = []
    for line in text.splitlines():
        marker = LIST_MARKER.match(line)
        if marker:
            rest = line[marker.end():]
            # A decimal literal followed by an operator is part of the expression.
            if marker[1] == ")" or not rest.startswith(("+", "-", "*", "/")):
                line = rest
        segments = line.split(DESCRIPTION_SEPARATOR)
        for index, segment in enumerate(segments):
            for start, end in _formula_spans(segment):
                description = ""
                if index < len(segments) - 1 and end == len(segment.rstrip()):
                    description = DESCRIPTION_SEPARATOR.join(segments[index + 1:]).strip()
                    formulas.append(Formula(segment[start:end], description))
                    break
                formulas.append(Formula(segment[start:end]))
                if len(formulas) > MAX_FORMULAS:
                    return formulas
            else:
                continue
            break
        if len(formulas) > MAX_FORMULAS:
            return formulas
    return formulas


def check_number(value: int | float) -> int | float:
    limit = float(MAX_MAGNITUDE) if isinstance(value, float) else MAX_MAGNITUDE
    if not abs(value) <= limit:
        raise FormulaError("Слишком большое число.")
    return value


def _power(base: int | float, exponent: int | float) -> int | float:
    if abs(exponent) > MAX_EXPONENT:
        raise FormulaError(f"Показатель степени по модулю должен быть не больше {MAX_EXPONENT}.")

    if isinstance(base, int) and isinstance(exponent, int) and exponent >= 0:
        result = 1
        magnitude = abs(base)
        for _ in range(exponent):
            if magnitude and abs(result) > MAX_MAGNITUDE // magnitude:
                raise FormulaError("Слишком большое число.")
            result *= base
        return result

    if base < 0 and exponent % 1:
        raise FormulaError("Степень даёт комплексное число.")
    if base and exponent * math.log(abs(base)) > math.log(MAX_MAGNITUDE):
        raise FormulaError("Слишком большое число.")
    return base**exponent


def calculate(symbol: str, left: int | float, right: int | float) -> int | float:
    try:
        result = _power(left, right) if symbol == "**" else OPERATIONS[symbol](left, right)
    except ZeroDivisionError:
        raise FormulaError("Деление на ноль.") from None
    except OverflowError:
        raise FormulaError("Слишком большое число.") from None
    return check_number(result)


def _evaluate_count(expression: Expression) -> int | float:
    if isinstance(expression, Number):
        return expression.value
    if isinstance(expression, Unary):
        value = _evaluate_count(expression.operand)
        return -value if expression.symbol == "-" else value
    if isinstance(expression, Binary):
        return calculate(
            expression.symbol, _evaluate_count(expression.left), _evaluate_count(expression.right)
        )
    raise FormulaError("Количество кубиков должно быть числом.")


class Parser:
    def __init__(self, text: str):
        self.tokens = list(TOKEN_RE.finditer(text))
        self.index = 0
        self.rolls = 0

    def peek(self) -> str:
        return self.tokens[self.index].group() if self.index < len(self.tokens) else ""

    def take(self) -> re.Match[str]:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def expect_close(self) -> None:
        if self.peek() != ")":
            raise FormulaError("Не хватает закрывающей скобки." if not self.peek() else "Неверная формула.")
        self.take()

    def expression(self, min_precedence: int = 1, depth: int = 0, *, allow_dice: bool = True) -> Expression:
        if depth > MAX_DEPTH:
            raise FormulaError(f"Максимальная вложенность — {MAX_DEPTH}.")
        if not self.peek():
            raise FormulaError("Неверная формула.")

        token = self.take()
        symbol = token.group()
        if symbol in ("+", "-"):
            operand = self.expression(UNARY_PRECEDENCE, depth + 1, allow_dice=allow_dice)
            left = Unary(symbol, operand)
        elif symbol == "(":
            left = self.expression(depth=depth + 1, allow_dice=allow_dice)
            self.expect_close()
        elif token.lastgroup == "number":
            left = Number(check_number(float(symbol) if "." in symbol else int(symbol)))
        elif token.lastgroup == "dice" and allow_dice:
            left = self.dice(token, depth)
        else:
            raise FormulaError("Неверная формула.")

        while PRECEDENCE.get(self.peek(), 0) >= min_precedence:
            symbol = self.take().group()
            precedence = PRECEDENCE[symbol]
            if symbol == "**":
                right = self.expression(precedence, depth + 1, allow_dice=allow_dice)
            else:
                right = self.expression(precedence + 1, depth, allow_dice=allow_dice)
            left = Binary(symbol, left, right)
        return left

    def dice(self, token: re.Match[str], depth: int) -> Dice:
        notation = DICE_RE.fullmatch(token.group())
        if notation is None:
            raise FormulaError("Неверная запись кубика. Для преимущества — d20kh, для помехи — d20kl.")

        count_text, sides_text, keep, kept_text = notation.groups()
        sides = int(sides_text)
        if sides not in DICE_SIDES:
            raise FormulaError("Допустимые кубики — d4, d6, d8, d10, d12, d20 и d100.")

        count = int(count_text) if count_text else None
        if self.peek() == "(" and self.tokens[self.index].start() == token.end():
            self.take()
            if self.peek() != ")":
                if count is not None:
                    raise FormulaError("Укажи количество кубиков один раз.")
                count = _evaluate_count(self.expression(depth=depth + 1, allow_dice=False))
            self.expect_close()
            closing = self.tokens[self.index - 1]
            if keep is None and self.peek() and self.tokens[self.index].start() == closing.end():
                suffix = KEEP_RE.fullmatch(self.peek())
                if suffix is not None:
                    keep, kept_text = suffix.groups()
                    self.take()

        if count is None:
            count = 2 if keep and kept_text is None else 1
        if not isinstance(count, int) or count < 0:
            raise FormulaError("Количество кубиков должно быть целым числом от нуля.")
        if count > MAX_DICE_PER_TERM:
            raise FormulaError(f"Максимум {MAX_DICE_PER_TERM} кубиков за один бросок.")
        self.rolls += count
        if self.rolls > MAX_DICE_PER_EXPRESSION:
            raise FormulaError(f"Максимум {MAX_DICE_PER_EXPRESSION} кубиков в формуле.")

        kept = count
        if keep:
            kept = int(kept_text) if kept_text is not None else 1
        if keep and not 1 <= kept <= count:
            raise FormulaError("kh/kl должны оставлять от одного до числа брошенных кубиков.")
        return Dice(sides, count, keep.lower() if keep else None, kept)


def parse_formula(text: str) -> Expression:
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise FormulaError(f"Максимальная длина формулы — {MAX_EXPRESSION_LENGTH} символов.")
    parser = Parser(text)
    expression = parser.expression()
    if parser.peek():
        if parser.peek() == ")":
            raise FormulaError("Лишняя закрывающая скобка.")
        raise FormulaError("Неверная формула.")
    if not any(token.lastgroup == "dice" for token in parser.tokens):
        raise FormulaError("В формуле нужен хотя бы один кубик.")
    return expression
