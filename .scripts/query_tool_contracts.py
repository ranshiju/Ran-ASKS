"""Pure API query contracts and bounded arithmetic; no tools, IO or eval."""
from __future__ import annotations

from decimal import Decimal, DecimalException, Underflow, localcontext
import ast
import math
import re


OPERATIONS = ("sum", "product", "difference", "quotient", "sum_products")


def schemas(signatures, *, intents, domains):
    result = {}
    required_names = {"query", "term", "node", "page", "name", "unit_id", "source_id", "locator", "ref"}
    for action, parameters in signatures.items():
        properties = {}
        required = []
        for name, default in parameters:
            field = {"type": "boolean"} if isinstance(default, bool) else {"type": "string"}
            if action == "hybrid_recall" and name == "subjects":
                field = {"type": "array", "maxItems": 3,
                         "items": {"type": "string", "minLength": 1, "maxLength": 160}}
            elif name in {"topk", "depth", "similar_topk"}:
                minimum = 0 if name == "similar_topk" else 1
                field = {"anyOf": [{"type": "integer", "minimum": minimum, "maximum": 20},
                                   {"type": "string", "pattern": r"^(?:[0-9]|1[0-9]|20)$" if minimum == 0 else r"^(?:[1-9]|1[0-9]|20)$"}]}
            elif name == "intent" or name == "profile":
                field["enum"] = list(intents)
            elif action == "semantic_search" and name == "scope":
                field["enum"] = ["node", "hub"]
            elif name == "domain":
                field["enum"] = ["", "academic", "public"] if action in {"source_search", "knowledge_search"} else ["", *sorted(domains)]
            elif name == "locator":
                field.update(minLength=3, pattern=r"^[^#\r\n]+#[^#\r\n]+$")
            elif action == "source_outline" and name == "source_id":
                field["pattern"] = r"S-[0-9a-f]{24}"
            elif action == "source_outline" and name in {"start_line", "limit"}:
                field = {"anyOf": [{"type": "integer", "minimum": 1},
                                   {"type": "string", "pattern": r"[1-9][0-9]*"}]}
                if name == "limit":
                    field["anyOf"][0]["maximum"] = 80
                    field["anyOf"][1]["pattern"] = r"(?:[1-9]|[1-7][0-9]|80)"
            elif action == "source_outline" and name == "query":
                field["description"] = "Literal substring of a source heading/caption, e.g. Table 10; empty lists all."
            if name in required_names and not (action == "source_outline" and name == "query"):
                required.append(name)
                if field.get("type") == "string":
                    field["minLength"] = 1
            elif default != "" and default is not None:
                field["default"] = default
            properties[name] = field
        result[action] = {"type": "object", "properties": properties,
                          "required": required, "additionalProperties": False}
    number = {"type": "number"}
    scalar_vector = {
        "type": "object", "properties": {
            "operation": {"type": "string", "enum": list(OPERATIONS)},
            "operands": {"type": "array", "minItems": 1, "maxItems": 256,
                         "items": {"anyOf": [number, {"type": "array", "minItems": 1,
                                                      "maxItems": 128, "items": number}]}}},
        "required": ["operation", "operands"], "additionalProperties": False}
    result["calculate"] = {"anyOf": [scalar_vector, {
        "type": "object", "properties": {
            "operation": {"type": "string", "enum": ["expression"]},
            "expression": {"type": "string", "minLength": 1, "maxLength": 1024},
            "variables": {"type": "object", "maxProperties": 16,
                          "additionalProperties": number}},
        "required": ["operation", "expression"], "additionalProperties": False}]}
    return result


def validate(value, schema, path="input"):
    """Return one mechanical error, without guessing parameter corrections."""
    if "anyOf" in schema:
        errors = [validate(value, choice, path) for choice in schema["anyOf"]]
        return "" if "" in errors else f"{path}: expected {schema}"
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "boolean": isinstance(value, bool),
             "integer": type(value) is int,
             "number": type(value) in (int, float)}.get(kind, False)
    if not valid:
        return f"{path}: expected {kind}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: allowed values {schema['enum']}"
    if kind == "object":
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            return f"{path}: too many properties"
        props = schema.get("properties", {})
        extra = set(value) - set(props)
        if extra and schema.get("additionalProperties") is False:
            return f"{path}: unknown parameters {sorted(extra)}; allowed {list(props)}"
        missing = set(schema.get("required", [])) - set(value)
        if missing:
            return f"{path}: missing {sorted(missing)}"
        for name, item in value.items():
            if name in props:
                error = validate(item, props[name], f"{path}.{name}")
                if error:
                    return error
            elif isinstance(schema.get("additionalProperties"), dict):
                error = validate(item, schema["additionalProperties"], f"{path}.{name}")
                if error:
                    return error
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000):
            return f"{path}: array length out of bounds"
        for index, item in enumerate(value):
            error = validate(item, schema["items"], f"{path}[{index}]")
            if error:
                return error
    elif kind == "string":
        if len(value.strip()) < schema.get("minLength", 0):
            return f"{path}: empty string"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path}: string too long"
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            return f"{path}: must match {schema['pattern']}"
    elif kind in {"integer", "number"}:
        if abs(value) > 1e100 or not math.isfinite(value):
            return f"{path}: number is nonfinite or exceeds 1e100"
        if value < schema.get("minimum", -1e100) or value > schema.get("maximum", 1e100):
            return f"{path}: number out of bounds"
    return ""


def _evaluate_expression(expression, variables):
    """Interpret only scalar arithmetic; source/model text never becomes code."""
    def bounded(value):
        if not value.is_finite() or abs(value) > Decimal("1e100") or (value and abs(value) < Decimal("1e-100")):
            raise ValueError("expression intermediate magnitude outside 1e-100..1e100")
        return value

    functions = {"sqrt", "min", "max"}
    for name in variables:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", name) or name in functions:
            raise ValueError("expression variable must be a short name distinct from sqrt/min/max")
    values = {name: bounded(Decimal(str(value))) for name, value in variables.items()}
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ValueError("invalid arithmetic expression; use ** for powers") from exc
    if sum(1 for _ in ast.walk(tree)) > 128:
        raise ValueError("expression exceeds 128 AST nodes")
    expression = expression.strip()

    def visit(node, depth=0):
        if depth > 16:
            raise ValueError("expression exceeds 16 levels")
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            literal = ast.get_source_segment(expression, node)
            if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", literal or ""):
                raise ValueError("expression requires decimal number literals")
            return bounded(Decimal(literal))
        if isinstance(node, ast.Name) and node.id in values:
            return values[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand, depth+1)
            return bounded(value if isinstance(node.op, ast.UAdd) else -value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            left, right = visit(node.left, depth+1), visit(node.right, depth+1)
            if isinstance(node.op, ast.Add): value = left+right
            elif isinstance(node.op, ast.Sub): value = left-right
            elif isinstance(node.op, ast.Mult): value = left*right
            elif isinstance(node.op, ast.Div): value = left/right
            else:
                if abs(right) > 1000:
                    raise ValueError("expression exponent exceeds 1000")
                value = left**right
            return bounded(value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions and not node.keywords:
            name = node.func.id
            if (name == "sqrt" and len(node.args) != 1) or not 1 <= len(node.args) <= 16:
                raise ValueError("sqrt requires one argument; min/max require 1..16")
            args = [visit(arg, depth+1) for arg in node.args]
            return bounded(args[0].sqrt() if name == "sqrt" else min(args) if name == "min" else max(args))
        raise ValueError("expression permits numbers, supplied variables, + - * / ** and sqrt/min/max only")

    with localcontext() as context:
        context.prec = 34
        context.Emax, context.Emin = 200, -200
        context.traps[Underflow] = True
        return visit(tree.body)


def calculate(operation, operands=None, *, expression=None, variables=None):
    schema = schemas({}, intents=(), domains=())["calculate"]
    supplied = {"operation": operation, **{k: v for k, v in {
        "operands": operands, "expression": expression, "variables": variables}.items() if v is not None}}
    error = validate(supplied, schema)
    if error:
        raise ValueError(error)
    vector = operation == "sum_products"
    if vector:
        if not 2 <= len(operands) <= 8 or not all(isinstance(v, list) for v in operands):
            raise ValueError("sum_products requires 2–8 vectors")
        shapes = [len(v) for v in operands]
        if len(set(shapes)) != 1:
            raise ValueError(f"vector lengths differ: {shapes}")
    elif operation != "expression":
        if any(isinstance(v, list) for v in operands):
            raise ValueError("scalar operation requires scalar operands")
        if operation in {"difference", "quotient"} and len(operands) != 2:
            raise ValueError("difference/quotient require exactly two operands")
        shapes = []
    else:
        shapes = []

    def product(values):
        result = Decimal(1)
        for value in values:
            result *= Decimal(str(value))
        return result

    def encode(value):
        if not value.is_finite() or abs(value) > Decimal("1e100"):
            raise ValueError("result exceeds numerical bounds")
        return int(value) if value == value.to_integral_value() else str(value.normalize())

    try:
        with localcontext() as context:
            context.prec = 34
            terms = [product(column) for column in zip(*operands)] if vector else []
            if operation == "expression":
                result = _evaluate_expression(expression, variables or {})
            elif vector:
                result = sum(terms, Decimal(0))
            elif operation == "sum":
                result = sum((Decimal(str(v)) for v in operands), Decimal(0))
            elif operation == "product":
                result = product(operands)
            elif operation == "difference":
                result = Decimal(str(operands[0])) - Decimal(str(operands[1]))
            else:
                result = Decimal(str(operands[0])) / Decimal(str(operands[1]))
            inputs = {"expression": expression, "variables": variables or {}} if operation == "expression" else {"operands": operands}
            return {"operation": operation, **inputs, "vector_lengths": shapes,
                    "terms": [encode(v) for v in terms], "result": encode(result),
                    "precision_decimal_digits": 34, "fact_evidence": False,
                    "scope": "Arithmetic for supplied operands only; formula choice and source applicability are not verified."}
    except DecimalException as exc:
        raise ValueError(f"arithmetic error: {type(exc).__name__}") from None
