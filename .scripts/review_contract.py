#!/usr/bin/env python3
"""Offline advisory content contracts and a deliberately bounded schema dialect."""
from __future__ import annotations

import hashlib
import html
import io
import json
import math
import re

CONTENT_VERSION = "advisory-review-content-v1"
MAX_BYTES = 512 * 1024
MAX_TEXT = 120000
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
SCHEMA_KEYS = {"type", "properties", "required", "additionalProperties", "items",
               "minLength", "maxLength", "minItems", "maxItems", "enum", "description"}
TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def image_info(data, formats):
    from PIL import Image
    if not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ValueError('Image exceeds its byte budget')
    with Image.open(io.BytesIO(data)) as image:
        kind = image.format
        if kind not in formats or getattr(image, 'n_frames', 1) != 1:
            raise ValueError('Unsupported or animated image')
        width, height = image.size
        if not 0 < width * height <= MAX_IMAGE_PIXELS:
            raise ValueError('Image exceeds its pixel budget')
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        if image.getexif().get(274, 1) != 1:
            raise ValueError('Normalize image orientation explicitly before import')
        image.load()
    return width, height, kind


def png_info(data):
    return image_info(data, {'PNG'})[:2]


class ReviewError(ValueError):
    """A bounded technical error, never a verdict on the reviewed artifact."""


def require(condition, message):
    if not condition:
        raise ReviewError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ReviewError(f"Non-finite JSON constant: {value}")


def decode(data):
    require(len(data) <= MAX_BYTES, "JSON exceeds 512 KiB")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_constant)
    except ReviewError:
        raise
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ReviewError("Invalid UTF-8 JSON") from error
    _bounded(value)
    return value


def _bounded(value, depth=0):
    require(depth <= 16, "JSON nesting exceeds 16 levels")
    if isinstance(value, dict):
        require(len(value) <= 100, "Too many object fields")
        for item in value.values():
            _bounded(item, depth + 1)
    elif isinstance(value, list):
        require(len(value) <= 1000, "Too many array entries")
        for item in value:
            _bounded(item, depth + 1)
    elif isinstance(value, float):
        require(math.isfinite(value), "Non-finite number")


def schema_check(spec, depth=0):
    require(depth <= 12 and isinstance(spec, dict), "Invalid local schema")
    require(not set(spec) - SCHEMA_KEYS, "Unsupported schema keyword (no references or code)")
    require(spec.get("type") in TYPES, "Schema requires one supported type")
    allowed = {"type", "enum", "description"}
    allowed |= {"properties", "required", "additionalProperties"} if spec["type"] == "object" else set()
    allowed |= {"items", "minItems", "maxItems"} if spec["type"] == "array" else set()
    allowed |= {"minLength", "maxLength"} if spec["type"] == "string" else set()
    require(set(spec) <= allowed, "Schema keyword does not apply to its type")
    if "description" in spec:
        require(isinstance(spec["description"], str), "Invalid schema description")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in spec:
            require(type(spec[key]) is int and 0 <= spec[key] <= MAX_TEXT,
                    "Invalid schema bound")
    for minimum, maximum in (("minLength", "maxLength"), ("minItems", "maxItems")):
        require(spec.get(minimum, 0) <= spec.get(maximum, MAX_TEXT), "Contradictory schema bounds")
    if "enum" in spec:
        require(isinstance(spec["enum"], list) and 0 < len(spec["enum"]) <= 100,
                "Invalid schema enum")
    if spec["type"] == "object":
        properties = spec.get("properties", {})
        required = spec.get("required", [])
        require(isinstance(properties, dict) and isinstance(required, list),
                "Invalid object schema")
        require(all(isinstance(key, str) for key in required)
                and len(required) == len(set(required)) and set(required) <= set(properties),
                "Invalid required properties")
        require(type(spec.get("additionalProperties", False)) is bool,
                "additionalProperties must be boolean")
        for child in properties.values():
            schema_check(child, depth + 1)
    if spec["type"] == "array":
        require("items" in spec, "Array schema requires items")
        schema_check(spec["items"], depth + 1)


def validate(value, spec, label="content"):
    kind = spec["type"]
    valid_type = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "boolean": type(value) is bool,
        "integer": type(value) is int, "number": type(value) in (int, float),
        "null": value is None,
    }[kind]
    require(valid_type, f"{label}: expected {kind}")
    if "enum" in spec:
        require(any(type(value) is type(item) and value == item for item in spec["enum"]),
                f"{label}: unexpected enum value")
    if kind == "object":
        properties = spec.get("properties", {})
        require(set(spec.get("required", [])) <= set(value), f"{label}: missing fields")
        require(spec.get("additionalProperties", False) or not set(value) - set(properties),
                f"{label}: unexpected fields")
        for key, child in value.items():
            if key in properties:
                validate(child, properties[key], f"{label}.{key}")
    elif kind == "array":
        require(spec.get("minItems", 0) <= len(value) <= spec.get("maxItems", 1000),
                f"{label}: array size out of bounds")
        for index, child in enumerate(value):
            validate(child, spec["items"], f"{label}[{index}]")
    elif kind == "string":
        require(spec.get("minLength", 0) <= len(value) <= spec.get("maxLength", 12000),
                f"{label}: text size out of bounds")


def text_spec(limit=6000, required=False):
    return {"type": "string", "maxLength": limit, "minLength": 1 if required else 0}


def array_spec(item, maximum=30):
    return {"type": "array", "items": item, "maxItems": maximum}


def object_spec(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


SCOPE = object_spec({
    "type": {"type": "string", "enum": ["document", "blocks", "image"]},
    "block_ids": array_spec(text_spec(40, True), 100), "quote": text_spec(),
    "location": text_spec(), "region": array_spec({"type": "number"}, 4),
    "attachment_id": text_spec(40, True),
}, ("type",))
ITEM = object_spec({
    "id": text_spec(80, True),
    "kind": {"type": "string", "enum": ["observation", "concern", "suggestion",
                                            "strength", "question", "tradeoff"]},
    "scope": SCOPE, "observation": text_spec(required=True),
    "basis": text_spec(required=True), "options": array_spec(text_spec(), 6),
    "uncertainty": text_spec(),
}, ("id", "kind", "scope", "observation", "basis"))


def content_schema(extension):
    return object_spec({
        "schema": {"type": "string", "enum": [CONTENT_VERSION]},
        "overview": text_spec(required=True), "feedback": array_spec(ITEM),
        "limitations": array_spec(text_spec(), 20), "extensions": extension,
        "material_observation": object_spec({
            "image_received": {"type": "boolean"},
            "readability": {"type": "string", "enum": ["readable", "partly-readable", "unreadable", "unavailable"]},
            "views": array_spec(object_spec({
                "attachment_id": text_spec(40, True), "image_received": {"type": "boolean"},
                "readability": {"type": "string", "enum": ["readable", "partly-readable", "unreadable", "unavailable"]},
                "display_size": array_spec({"type": "integer"}, 2), "limitations": text_spec(),
            }, ("attachment_id", "image_received", "readability")), 7),
        }, ("image_received", "readability")),
    }, ("schema", "overview", "feedback", "limitations"))


def validate_content(value, extension, blocks, material_kind="text", image_views=None):
    require(len(encode(value)) <= MAX_BYTES // 2, "Feedback exceeds the normalized content budget")
    schema_check(extension)
    validate(value, content_schema(extension))
    validate(value.get("extensions", {}), extension, "content.extensions")
    if material_kind == "image":
        observation = value.get("material_observation", {})
        require(observation.get("image_received") is True and observation.get("readability") in {"readable", "partly-readable"},
                "Image was not received or cannot be read; this review is technically incomplete")
    expected_images = {item["attachment_id"] for item in image_views or []}
    observed = value.get("material_observation", {}).get("views", [])
    if expected_images:
        require(len(observed) == len(expected_images) and {item["attachment_id"] for item in observed} == expected_images,
                "Report receipt and readability for every declared image attachment")
        for item in observed:
            require(item["image_received"] is True and item["readability"] in {"readable", "partly-readable"},
                    "A declared image view was not received or is unreadable; review is technically incomplete")
    else:
        require(not observed, "Per-view observations require a declared view set")
    for item in observed:
        if "display_size" in item:
            require(len(item["display_size"]) == 2 and all(size > 0 for size in item["display_size"]),
                    "Self-reported display size must contain two positive dimensions")
    identifiers = set()
    diagnostics = []
    block_map = {block["id"]: block["text"] for block in blocks}
    for item in value["feedback"]:
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item["id"]), "Invalid feedback ID")
        require(item["id"] not in identifiers, "Duplicate feedback ID")
        identifiers.add(item["id"])
        scope = item["scope"]
        if "attachment_id" in scope:
            require(material_kind == "image" and scope["type"] == "image" and
                    scope["attachment_id"] in (expected_images or {"document-1"}), "Unknown image attachment scope")
        if scope["type"] == "image":
            require(material_kind == "image", "Image scope requires an image input")
            require(not scope.get("block_ids") and not scope.get("quote"), "Image observations cannot claim verified text anchors")
            require(bool(scope.get("location")) or bool(scope.get("region")), "Image scope requires a visible location")
        if "region" in scope:
            region = scope["region"]
            require(len(region) == 4 and all(0 <= coordinate <= 1 for coordinate in region) and
                    region[2] > 0 and region[3] > 0 and region[0] + region[2] <= 1 and region[1] + region[3] <= 1,
                    "Image region must be normalized x,y,width,height")
        if material_kind == "image":
            require(scope["type"] != "blocks" and not scope.get("block_ids") and not scope.get("quote"),
                    "Images have no verified text blocks or quotations")
        references = scope.get("block_ids", [])
        require(len(references) == len(set(references)), "Duplicate block references")
        if scope["type"] == "blocks" and not references:
            diagnostics.append({"item_id": item["id"], "code": "missing_anchor"})
        if set(references) - set(block_map):
            diagnostics.append({"item_id": item["id"], "code": "unmatched_anchor"})
        corpus = "".join(block_map.get(reference, "") for reference in references) \
            if references else "".join(block_map.values())
        if scope.get("quote") and scope["quote"] not in corpus:
            diagnostics.append({"item_id": item["id"], "code": "unmatched_quote"})
    return diagnostics


def safe_text(value):
    escaped = html.escape(str(value), quote=True)
    for char in "\\`*_[]!{}#|:":
        escaped = escaped.replace(char, f"&#{ord(char)};")
    return escaped
