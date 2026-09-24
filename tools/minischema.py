"""Минимальный валидатор JSON Schema (draft 2020-12 подмножество) для проверки примеров без внешних зависимостей."""
import re

def validate(inst, sch, path="$", errors=None):
    errors = [] if errors is None else errors
    if "$ref" in sch and sch["$ref"].startswith("#/$defs/"):
        sch = {**_root["$defs"][sch["$ref"].split("/")[-1]], **{k: v for k, v in sch.items() if k != "$ref"}}
    t = sch.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}
    if t:
        tl = t if isinstance(t, list) else [t]
        ok = any(isinstance(inst, types[x]) and not (x in ("integer", "number") and isinstance(inst, bool)) for x in tl)
        if not ok: errors.append(f"{path}: тип {type(inst).__name__} не соответствует {t}"); return errors
    if "enum" in sch and inst not in sch["enum"]: errors.append(f"{path}: значение {inst!r} не в enum")
    if "const" in sch and inst != sch["const"]: errors.append(f"{path}: значение {inst!r} != const {sch['const']!r}")
    if isinstance(inst, str):
        if "minLength" in sch and len(inst) < sch["minLength"]: errors.append(f"{path}: короче {sch['minLength']}")
        if "maxLength" in sch and len(inst) > sch["maxLength"]: errors.append(f"{path}: длиннее {sch['maxLength']}")
        if "pattern" in sch and not re.search(sch["pattern"], inst): errors.append(f"{path}: не соответствует pattern {sch['pattern']}")
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in sch and inst < sch["minimum"]: errors.append(f"{path}: < minimum {sch['minimum']}")
        if "maximum" in sch and inst > sch["maximum"]: errors.append(f"{path}: > maximum {sch['maximum']}")
    if isinstance(inst, list):
        if "minItems" in sch and len(inst) < sch["minItems"]: errors.append(f"{path}: элементов меньше {sch['minItems']}")
        if "maxItems" in sch and len(inst) > sch["maxItems"]: errors.append(f"{path}: элементов больше {sch['maxItems']}")
        if "items" in sch:
            for i, x in enumerate(inst): validate(x, sch["items"], f"{path}[{i}]", errors)
    if isinstance(inst, dict):
        for r in sch.get("required", []):
            if r not in inst: errors.append(f"{path}: нет обязательного поля {r}")
        props = sch.get("properties", {})
        for k, v in inst.items():
            if k in props: validate(v, props[k], f"{path}.{k}", errors)
            elif sch.get("additionalProperties") is False and not k.startswith("_"): errors.append(f"{path}: лишнее поле {k}")
    return errors

def validate_root(inst, schema):
    global _root; _root = schema
    return validate(inst, schema)
