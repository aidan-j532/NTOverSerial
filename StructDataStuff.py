import re
import struct as _struct

# ok so this is the table of all the primitive types
# each one maps to (struct format char, size in bytes, is it signed or not)
_TYPES = {
    "bool":    ("?", 1, False),
    "boolean": ("?", 1, False),
    "char":    ("b", 1, True),
    "int8":    ("b", 1, True),
    "int16":   ("h", 2, True),
    "int32":   ("i", 4, True),
    "int64":   ("q", 8, True),
    "uint8":   ("B", 1, False),
    "uint16":  ("H", 2, False),
    "uint32":  ("I", 4, False),
    "uint64":  ("Q", 8, False),
    "float":   ("f", 4, False),
    "float32": ("f", 4, False),
    "double":  ("d", 8, False),
    "float64": ("d", 8, False),
}

_TOPIC_RE = re.compile(r"^struct:(.+?)(\[\])?$")
# matches one field decl, like:
#   float x;
#   optional float yaw
#   int8 id
#   int32 arr[4] : 8
#   uint8 data[?]
#   enum{Red=0,Blue=1} alliance
_FIELD_RE = re.compile(
    r"^(?:(?:enum\s*)?\{(?P<enum>[^}]*)\}\s*)?"
    r"(?P<optkw>optional\s+)?"
    r"(?P<type>[^\s\[\]?]+)"
    r"(?:\[\s*(?P<count>\?|\d+)\s*\])?"
    r"(?P<opt>\?)?"
    r"\s+"
    r"(?P<name>[A-Za-z_]\w*)"
    r"\s*(?:\[\s*(?P<count2>\?|\d+)\s*\])?"
    r"\s*(?::\s*(?P<bits>\d+))?\s*;?\s*$"
)


def _parse_enum(spec):
    # this turns "Red=0,Blue=1" into a dict {0: "Red", 1: "Blue"}
    if spec is None:
        return None
    mapping = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split("=", 1)]
        if len(parts) == 2:
            try:
                mapping[int(parts[1], 0)] = parts[0]
            except ValueError:
                continue
    return mapping or None


def parse_schema(schema):
    # parses the whole schema string into a list of field dicts
    # each dict has: name, type, count, vla, optional, bits, enum, struct
    fields = []
    # fields are split up by semicolons
    for decl in schema.split(";"):
        decl = decl.strip()
        if not decl:
            continue
        m = _FIELD_RE.match(decl)
        if not m:
            raise ValueError(f"unsupported schema declaration: {decl!r}")
        g = m.groupdict()
        ftype = g["type"]
        # the array count can be before or after the name, idk why but whatever
        count_s = g["count"] or g["count2"]
        vla = count_s == "?"
        if count_s and count_s != "?":
            count = int(count_s)
        else:
            count = 1
        bits = int(g["bits"]) if g["bits"] else None
        enum = _parse_enum(g["enum"])
        optional = bool(g["optkw"] or g["opt"])
        # if its not a primitive then its a nested struct
        is_struct = ftype not in _TYPES
        field = {
            "name": g["name"],
            "type": ftype,
            "count": count,
            "vla": vla,
            "optional": optional,
            "bits": bits,
            "enum": enum,
            "struct": is_struct,
        }
        # check all the edge cases so we dont break later
        if bits is not None and is_struct:
            raise ValueError(f"bit-field must be integer/bool type: {decl!r}")
        if bits is not None and ftype == "bool" and bits != 1:
            raise ValueError("bool bit-field must be 1 bit")
        if bits is not None and bits > _TYPES[ftype][1] * 8:
            raise ValueError(f"bit-field larger than storage type: {decl!r}")
        if (vla or optional) and bits is not None:
            raise ValueError(f"bit-field cannot be variable-length or optional: {decl!r}")
        if vla and optional:
            raise ValueError(f"field cannot be both variable-length and optional: {decl!r}")
        fields.append(field)
    return fields


def _sign_extend(val, width):
    # this makes an unsigned value actually negative if it needs to be
    sign = 1 << (width - 1)
    return (val ^ sign) - sign if val & sign else val


def _read_bits(cur, width):
    # pulls `width` bits out of the current bit-packed chunk and moves the cursor
    shift = cur["start_bit"]
    val = (cur["raw"] >> shift) & ((1 << width) - 1)
    cur["start_bit"] += width
    return val


class SchemaRegistry:
    # stores all the struct schemas so we can decode them later
    # structs can be looked up by their full type string ("struct:Foo")
    # or a short alias like "Foo" / "photonstruct:Foo"
    def __init__(self):
        self._fields = {}    # struct name -> list of field dicts
        self._schemas = {}   # struct name -> the raw schema string
        self._aliases = {}   # alias -> the real struct name

    def register(self, struct_name, schema):
        # saves the parsed schema and makes an alias for it
        self._fields[struct_name] = parse_schema(schema)
        self._schemas[struct_name] = schema
        alias = None
        # take off the "struct:" or "photonstruct:" bit to make a short alias
        if struct_name.startswith("struct:"):
            alias = struct_name[len("struct:"):]
        elif struct_name.startswith("photonstruct:"):
            alias = struct_name[len("photonstruct:"):]
        if alias and alias not in self._fields and alias not in self._aliases:
            self._aliases[alias] = struct_name

    def _resolve(self, name):
        # figures out the real struct name from whatever name/alias we got
        if name in self._fields:
            return name
        return self._aliases.get(name)

    def has_type(self, type_str):
        # checks if this is a known struct or an array of one
        if self._resolve(type_str) is not None:
            return True
        if type_str.endswith("[]") and self._resolve(type_str[:-2]) is not None:
            return True
        return False

    def get_schema(self, struct_name):
        # gives back the original schema string for a struct
        base = struct_name[:-2] if struct_name.endswith("[]") else struct_name
        resolved = self._resolve(base)
        return self._schemas.get(resolved) if resolved else None

    def decode_struct(self, struct_name, buf, pos=0):
        # decodes one struct from the buffer starting at pos
        # returns (a dict of name->value, the new position)
        # field order matches whatever order they had in the schema
        resolved = self._resolve(struct_name)
        if resolved is None:
            raise KeyError(f"unknown struct schema: {struct_name}")
        out = {}
        cur = None  # the current bit-packing chunk we're pulling from, if any
        for f in self._fields[resolved]:
            if f["bits"] is not None:
                val, cur, pos = self._decode_bitfield(buf, pos, f, cur)
                out[f["name"]] = val
            else:
                pos = self._flush(cur, pos)
                cur = None
                out[f["name"]], pos = self._decode_field(buf, pos, f)
        pos = self._flush(cur, pos)
        return out, pos

    def decode_type(self, type_str, buf):
        # decodes the buffer as a struct OR an array of structs
        if self._resolve(type_str) is not None:
            val, _ = self.decode_struct(type_str, buf, 0)
            return val
        if type_str.endswith("[]"):
            base = type_str[:-2]
            if self._resolve(base) is not None:
                # figure out how big one element is, then slice them all out
                _, elem_size = self.decode_struct(base, buf, 0)
                if elem_size <= 0:
                    return []
                out = []
                pos = 0
                while pos + elem_size <= len(buf):
                    val, pos = self.decode_struct(base, buf, pos)
                    out.append(val)
                return out
        raise KeyError(f"unknown struct schema: {type_str}")

    @staticmethod
    def _flush(cur, pos):
        # skips past any leftover bit-packing chunk by moving pos forward
        return pos + cur["size"] if cur is not None else pos

    def _decode_field(self, buf, pos, f):
        # decodes a single field, dealing with vla / optional / arrays
        if f["vla"]:
            # variable-length array: the first byte tells us how many elements
            n = buf[pos]
            pos += 1
            vals = []
            for _ in range(n):
                val, pos = self._decode_one(buf, pos, f)
                vals.append(val)
            return vals, pos
        if f["optional"]:
            # optional field: next byte is a "is this here or not" flag
            present = buf[pos]
            pos += 1
            if not present:
                return None, pos
            return self._decode_one(buf, pos, f)
        if f["count"] > 1 and f["type"] != "char":
            vals = []
            for _ in range(f["count"]):
                val, pos = self._decode_one(buf, pos, f)
                vals.append(val)
            return vals, pos
        return self._decode_one(buf, pos, f)

    def _decode_one(self, buf, pos, f):
        # decodes a single value with no array/optional stuff around it
        t = f["type"]
        if f["struct"]:
            return self.decode_struct(t, buf, pos)
        if t == "char" and f["count"] > 1:
            # a char array is basically a null-terminated string
            s = buf[pos:pos + f["count"]].split(b"\x00", 1)[0]
            return s.decode("utf-8", "replace"), pos + f["count"]
        fmt, sz, _ = _TYPES[t]
        val = _struct.unpack_from("<" + fmt, buf, pos)[0]
        pos += sz
        # if this field has an enum, swap the raw number for the name
        if f["enum"] is not None and isinstance(val, (int, bool)) and val in f["enum"]:
            val = f["enum"][val]
        return val, pos

    def _decode_bitfield(self, buf, pos, f, cur):
        # decodes a bit-packed field, reusing the current chunk if it fits
        #
        # `cur` has the raw bits we loaded plus a cursor saying how many bits
        # we've already taken. if the field fits in whats left, we read from it
        # otherwise we load a whole new chunk from the buffer
        is_bool = f["type"] == "bool"
        size = 1 if is_bool else _TYPES[f["type"]][1]
        width = f["bits"]
        # try to reuse the current chunk if theres room for this field
        if cur is not None:
            remaining = cur["size"] * 8 - cur["start_bit"]
            if is_bool:
                if remaining >= 1:
                    return _read_bits(cur, 1), cur, pos
            elif cur["size"] == size and remaining >= width:
                return self._post_int(_read_bits(cur, width), f), cur, pos
        # no room left (or no chunk yet), so flush and grab a new one
        pos = self._flush(cur, pos)
        storage_size = size if not is_bool else 1
        cur = {
            "size": storage_size,
            "start_bit": 0,
            "raw": int.from_bytes(buf[pos:pos + storage_size], "little"),
        }
        if is_bool:
            return _read_bits(cur, 1), cur, pos
        return self._post_int(_read_bits(cur, width), f), cur, pos

    def _post_int(self, val, f):
        # post-processing for an int: sign-extend it and apply the enum if any
        if f["type"] in ("int8", "int16", "int32", "int64"):
            val = _sign_extend(val, f["bits"])
        if f["enum"] is not None and val in f["enum"]:
            val = f["enum"][val]
        return val
