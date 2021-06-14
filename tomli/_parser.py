from datetime import date, datetime, time, timedelta, timezone, tzinfo
import string
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    Optional,
    TextIO,
    Tuple,
    Union,
)

from tomli._re import (
    RE_BIN,
    RE_DATETIME,
    RE_DEC_OR_FLOAT,
    RE_HEX,
    RE_LOCAL_TIME,
    RE_OCT,
)

if TYPE_CHECKING:
    from re import Match, Pattern


ASCII_CTRL = frozenset(chr(i) for i in range(32)) | frozenset(chr(127))

# Neither of these sets include quotation mark or backslash. They are
# currently handled as separate cases in the parser functions.
ILLEGAL_BASIC_STR_CHARS = ASCII_CTRL - frozenset("\t")
ILLEGAL_MULTILINE_BASIC_STR_CHARS = ASCII_CTRL - frozenset("\t\n\r")

ILLEGAL_LITERAL_STR_CHARS = ILLEGAL_BASIC_STR_CHARS
ILLEGAL_MULTILINE_LITERAL_STR_CHARS = ASCII_CTRL - frozenset("\t\n")

ILLEGAL_COMMENT_CHARS = ILLEGAL_BASIC_STR_CHARS

TOML_WS = frozenset(" \t")
TOML_WS_AND_NEWLINE = TOML_WS | frozenset("\n")
BARE_KEY_CHARS = frozenset(string.ascii_letters + string.digits + "-_")
KEY_INITIAL_CHARS = BARE_KEY_CHARS | frozenset("\"'")

BASIC_STR_ESCAPE_REPLACEMENTS = MappingProxyType(
    {
        "\\b": "\u0008",  # backspace
        "\\t": "\u0009",  # tab
        "\\n": "\u000A",  # linefeed
        "\\f": "\u000C",  # form feed
        "\\r": "\u000D",  # carriage return
        '\\"': "\u0022",  # quote
        "\\\\": "\u005C",  # backslash
    }
)

ParseFloat = Callable[[str], Any]
Key = Tuple[str, ...]
Pos = int


class TOMLDecodeError(ValueError):
    """An error raised if a document is not valid TOML."""


def load(fp: TextIO, *, parse_float: ParseFloat = float) -> Dict[str, Any]:
    """Parse TOML from a file object."""
    s = fp.read()
    return loads(s, parse_float=parse_float)


def loads(s: str, *, parse_float: ParseFloat = float) -> Dict[str, Any]:  # noqa: C901
    """Parse TOML from a string."""

    # The spec allows converting "\r\n" to "\n", even in string
    # literals. Let's do so to simplify parsing.
    src = s.replace("\r\n", "\n")
    pos = 0
    state = State()

    # Parse one statement at a time
    # (typically means one line in TOML source)
    while True:
        # 1. Skip line leading whitespace
        pos = skip_chars(src, pos, TOML_WS)

        # 2. Parse rules. Expect one of the following:
        #    - end of file
        #    - end of line
        #    - comment
        #    - key->value
        #    - append dict to list (and move to its namespace)
        #    - create dict (and move to its namespace)
        try:
            char = src[pos]
        except IndexError:
            break
        if char == "\n":
            pos += 1
            continue
        elif char == "#":
            pos = expect_comment(src, pos)
        elif char in KEY_INITIAL_CHARS:
            pos = key_value_rule(src, pos, state, parse_float)
        elif src[pos : pos + 2] == "[[":
            pos = create_list_rule(src, pos, state)
        elif char == "[":
            pos = create_dict_rule(src, pos, state)
        else:
            raise suffixed_err(src, pos, "Invalid statement")

        # 3. Skip trailing whitespace and line comment
        pos = skip_chars(src, pos, TOML_WS)
        pos = skip_comment(src, pos)

        # 4. Expect end of line or end of file
        try:
            char = src[pos]
        except IndexError:
            break
        if char != "\n":
            raise suffixed_err(
                src, pos, "Expected newline or end of document after a statement"
            )
        pos += 1

    return state.out.dict


class State:
    def __init__(self) -> None:
        # Mutable, read-only
        self.out = NestedDict()
        self.flags = Flags()

        # Immutable, read and write
        self.header_namespace: Key = ()


class Flags:
    """Flags that map to parsed keys/namespaces."""

    # Marks an immutable namespace (inline array or inline table).
    FROZEN = 0
    # Marks a nest that has been explicitly created and can no longer
    # be opened using the "[table]" syntax.
    EXPLICIT_NEST = 1

    def __init__(self) -> None:
        self._flags: Dict[str, dict] = {}

    def unset_all(self, key: Key) -> None:
        cont = self._flags
        for k in key[:-1]:
            if k not in cont:
                return
            cont = cont[k]["nested"]
        cont.pop(key[-1], None)

    def set_for_relative_key(self, head_key: Key, rel_key: Key, flag: int) -> None:
        cont = self._flags
        for k in head_key:
            if k not in cont:
                cont[k] = {"flags": set(), "recursive_flags": set(), "nested": {}}
            cont = cont[k]["nested"]
        for k in rel_key:
            if k in cont:
                cont[k]["flags"].add(flag)
            else:
                cont[k] = {"flags": {flag}, "recursive_flags": set(), "nested": {}}
            cont = cont[k]["nested"]

    def set(self, key: Key, flag: int, *, recursive: bool) -> None:  # noqa: A003
        cont = self._flags
        key_parent, key_stem = key[:-1], key[-1]
        for k in key_parent:
            if k not in cont:
                cont[k] = {"flags": set(), "recursive_flags": set(), "nested": {}}
            cont = cont[k]["nested"]
        if key_stem not in cont:
            cont[key_stem] = {"flags": set(), "recursive_flags": set(), "nested": {}}
        cont[key_stem]["recursive_flags" if recursive else "flags"].add(flag)

    def is_(self, key: Key, flag: int) -> bool:
        if not key:
            return False  # document root has no flags
        cont = self._flags
        for k in key[:-1]:
            if k not in cont:
                return False
            inner_cont = cont[k]
            if flag in inner_cont["recursive_flags"]:
                return True
            cont = inner_cont["nested"]
        key_stem = key[-1]
        if key_stem in cont:
            cont = cont[key_stem]
            return flag in cont["flags"] or flag in cont["recursive_flags"]
        return False


class NestedDict:
    def __init__(self) -> None:
        # The parsed content of the TOML document
        self.dict: Dict[str, Any] = {}

    def get_or_create_nest(
        self,
        key: Key,
        *,
        access_lists: bool = True,
    ) -> dict:
        cont: Any = self.dict
        for k in key:
            if k not in cont:
                cont[k] = {}
            cont = cont[k]
            if access_lists and isinstance(cont, list):
                cont = cont[-1]
            if not isinstance(cont, dict):
                raise KeyError("There is no nest behind this key")
        return cont

    def append_nest_to_list(self, key: Key) -> None:
        cont = self.get_or_create_nest(key[:-1])
        last_key = key[-1]
        if last_key in cont:
            list_ = cont[last_key]
            if not isinstance(list_, list):
                raise KeyError("An object other than list found behind this key")
            list_.append({})
        else:
            cont[last_key] = [{}]


def skip_chars(src: str, pos: Pos, chars: Iterable[str]) -> Pos:
    try:
        while src[pos] in chars:
            pos += 1
    except IndexError:
        pass
    return pos


def skip_until(
    src: str, pos: Pos, expect_char: str, *, error_on: Iterable[str], error_on_eof: bool
) -> Pos:
    while True:
        try:
            char = src[pos]
        except IndexError:
            if error_on_eof:
                raise suffixed_err(src, pos, f'Expected "{expect_char!r}"')
            break
        if char == expect_char:
            break
        if char in error_on:
            raise suffixed_err(src, pos, f'Found invalid character "{char!r}"')
        pos += 1
    return pos


def skip_comment(src: str, pos: Pos) -> Pos:
    try:
        char: Optional[str] = src[pos]
    except IndexError:
        char = None
    if char == "#":
        return expect_comment(src, pos)
    return pos


def skip_comments_and_array_ws(src: str, pos: Pos) -> Pos:
    while True:
        pos_before_skip = pos
        pos = skip_chars(src, pos, TOML_WS_AND_NEWLINE)
        pos = skip_comment(src, pos)
        if pos == pos_before_skip:
            break
    return pos


def expect_comment(src: str, pos: Pos) -> Pos:
    pos += 1
    return skip_until(
        src, pos, "\n", error_on=ILLEGAL_COMMENT_CHARS, error_on_eof=False
    )


def create_dict_rule(src: str, pos: Pos, state: State) -> Pos:
    pos += 1
    pos = skip_chars(src, pos, TOML_WS)
    pos, key = parse_key(src, pos)

    if state.flags.is_(key, Flags.EXPLICIT_NEST) or state.flags.is_(key, Flags.FROZEN):
        raise suffixed_err(src, pos, f"Can not declare {key} twice")
    state.flags.set(key, Flags.EXPLICIT_NEST, recursive=False)
    try:
        state.out.get_or_create_nest(key)
    except KeyError:
        raise suffixed_err(src, pos, "Can not overwrite a value")
    state.header_namespace = key

    if src[pos : pos + 1] != "]":
        raise suffixed_err(src, pos, 'Expected "]" at the end of a table declaration')
    return pos + 1


def create_list_rule(src: str, pos: Pos, state: State) -> Pos:
    pos += 2
    pos = skip_chars(src, pos, TOML_WS)
    pos, key = parse_key(src, pos)

    if state.flags.is_(key, Flags.FROZEN):
        raise suffixed_err(src, pos, f"Can not mutate immutable namespace {key}")
    # Free the namespace now that it points to another empty list item...
    state.flags.unset_all(key)
    # ...but this key precisely is still prohibited from table declaration
    state.flags.set(key, Flags.EXPLICIT_NEST, recursive=False)
    try:
        state.out.append_nest_to_list(key)
    except KeyError:
        raise suffixed_err(src, pos, "Can not overwrite a value")
    state.header_namespace = key

    end_marker = src[pos : pos + 2]
    if end_marker != "]]":
        raise suffixed_err(
            src,
            pos,
            f'Found "{end_marker!r}" at the end of an array declaration.'
            ' Expected "]]"',
        )
    return pos + 2


def key_value_rule(src: str, pos: Pos, state: State, parse_float: ParseFloat) -> Pos:
    pos, key, value = parse_key_value_pair(src, pos, parse_float)
    key_parent, key_stem = key[:-1], key[-1]
    abs_key_parent = state.header_namespace + key_parent
    abs_key = state.header_namespace + key

    if state.flags.is_(abs_key_parent, Flags.FROZEN):
        raise suffixed_err(
            src, pos, f"Can not mutate immutable namespace {abs_key_parent}"
        )
    # Containers in the relative path can't be opened with the table syntax after this
    state.flags.set_for_relative_key(state.header_namespace, key, Flags.EXPLICIT_NEST)
    try:
        nest = state.out.get_or_create_nest(abs_key_parent)
    except KeyError:
        raise suffixed_err(src, pos, "Can not overwrite a value")
    if key_stem in nest:
        raise suffixed_err(src, pos, f"Can not define {abs_key} twice")
    # Mark inline table and array namespaces recursively immutable
    if isinstance(value, (dict, list)):
        state.flags.set(abs_key, Flags.FROZEN, recursive=True)
    nest[key_stem] = value
    return pos


def parse_key_value_pair(
    src: str, pos: Pos, parse_float: ParseFloat
) -> Tuple[Pos, Key, Any]:
    pos, key = parse_key(src, pos)
    try:
        char: Optional[str] = src[pos]
    except IndexError:
        char = None
    if char != "=":
        raise suffixed_err(src, pos, 'Expected "=" after a key in a key/value pair')
    pos += 1
    pos = skip_chars(src, pos, TOML_WS)
    pos, value = parse_value(src, pos, parse_float)
    return pos, key, value


def parse_key(src: str, pos: Pos) -> Tuple[Pos, Key]:
    pos, key_part = parse_key_part(src, pos)
    key = [key_part]
    pos = skip_chars(src, pos, TOML_WS)
    while True:
        try:
            char: Optional[str] = src[pos]
        except IndexError:
            char = None
        if char != ".":
            break
        pos += 1
        pos = skip_chars(src, pos, TOML_WS)
        pos, key_part = parse_key_part(src, pos)
        key.append(key_part)
        pos = skip_chars(src, pos, TOML_WS)
    return pos, tuple(key)


def parse_key_part(src: str, pos: Pos) -> Tuple[Pos, str]:
    try:
        char: Optional[str] = src[pos]
    except IndexError:
        char = None
    if char in BARE_KEY_CHARS:
        start_pos = pos
        pos = skip_chars(src, pos, BARE_KEY_CHARS)
        return pos, src[start_pos:pos]
    if char == "'":
        return parse_literal_str(src, pos)
    if char == '"':
        return parse_basic_str(src, pos)
    raise suffixed_err(src, pos, "Invalid initial character for a key part")


def parse_basic_str(src: str, pos: Pos) -> Tuple[Pos, str]:
    pos += 1
    return parse_string(
        src,
        pos,
        delim='"',
        delim_len=1,
        error_on=ILLEGAL_BASIC_STR_CHARS,
        parse_escapes=parse_basic_str_escape,
    )


def parse_array(src: str, pos: Pos, parse_float: ParseFloat) -> Tuple[Pos, list]:
    pos += 1
    array: list = []

    pos = skip_comments_and_array_ws(src, pos)
    if src[pos : pos + 1] == "]":
        return pos + 1, array
    while True:
        pos, val = parse_value(src, pos, parse_float)
        array.append(val)
        pos = skip_comments_and_array_ws(src, pos)

        c = src[pos : pos + 1]
        if c == "]":
            return pos + 1, array
        if c != ",":
            raise suffixed_err(src, pos, "Unclosed array")
        pos += 1

        pos = skip_comments_and_array_ws(src, pos)
        if src[pos : pos + 1] == "]":
            return pos + 1, array


def parse_inline_table(
    src: str, pos: Pos, parse_float: ParseFloat
) -> Tuple[Pos, dict]:  # noqa: C901
    pos += 1
    # We use a subset of the functionality NestedDict provides. We use it for
    # the convenient getter, and recursive freeze for inner arrays and tables.
    # Cutting a new lighter NestedDict base class could work here?
    nested_dict = NestedDict()
    flags = Flags()

    pos = skip_chars(src, pos, TOML_WS)
    if src[pos : pos + 1] == "}":
        return pos + 1, nested_dict.dict
    while True:
        pos, key, value = parse_key_value_pair(src, pos, parse_float)
        key_parent, key_stem = key[:-1], key[-1]
        if flags.is_(key, Flags.FROZEN):
            raise suffixed_err(src, pos, f"Can not mutate immutable namespace {key}")
        try:
            nest = nested_dict.get_or_create_nest(key_parent, access_lists=False)
        except KeyError:
            raise suffixed_err(src, pos, "Can not overwrite a value")
        if key_stem in nest:
            raise suffixed_err(src, pos, f'Duplicate inline table key "{key_stem}"')
        nest[key_stem] = value
        pos = skip_chars(src, pos, TOML_WS)
        c = src[pos : pos + 1]
        if c == "}":
            return pos + 1, nested_dict.dict
        if c != ",":
            raise suffixed_err(src, pos, "Unclosed inline table")
        if isinstance(value, (dict, list)):
            flags.set(key, Flags.FROZEN, recursive=True)
        pos += 1
        pos = skip_chars(src, pos, TOML_WS)


def parse_basic_str_escape(
    src: str, pos: Pos, *, multiline: bool = False
) -> Tuple[Pos, str]:
    escape_id = src[pos : pos + 2]
    pos += 2
    if multiline and escape_id in {"\\ ", "\\\t", "\\\n"}:
        # Skip whitespace until next non-whitespace character or end of
        # the doc. Error if non-whitespace is found before newline.
        if escape_id != "\\\n":
            pos = skip_chars(src, pos, TOML_WS)
            char = src[pos : pos + 1]
            if not char:
                return pos, ""
            if char != "\n":
                raise suffixed_err(src, pos, 'Unescaped "\\" in a string')
            pos += 1
        pos = skip_chars(src, pos, TOML_WS_AND_NEWLINE)
        return pos, ""
    if escape_id in BASIC_STR_ESCAPE_REPLACEMENTS:
        return pos, BASIC_STR_ESCAPE_REPLACEMENTS[escape_id]
    if escape_id == "\\u":
        return parse_hex_char(src, pos, 4)
    if escape_id == "\\U":
        return parse_hex_char(src, pos, 8)
    if len(escape_id) != 2:
        raise suffixed_err(src, pos, "Unterminated string")
    raise suffixed_err(src, pos, 'Unescaped "\\" in a string')


def parse_basic_str_escape_multiline(src: str, pos: Pos) -> Tuple[Pos, str]:
    return parse_basic_str_escape(src, pos, multiline=True)


def parse_hex_char(src: str, pos: Pos, hex_len: int) -> Tuple[Pos, str]:
    hex_str = src[pos : pos + hex_len]
    if len(hex_str) != hex_len or any(c not in string.hexdigits for c in hex_str):
        raise suffixed_err(src, pos, "Invalid hex value")
    pos += hex_len
    hex_int = int(hex_str, 16)
    if not is_unicode_scalar_value(hex_int):
        raise suffixed_err(src, pos, "Escaped character is not a Unicode scalar value")
    return pos, chr(hex_int)


def parse_literal_str(src: str, pos: Pos) -> Tuple[Pos, str]:
    pos += 1  # Skip starting apostrophe
    start_pos = pos
    pos = skip_until(
        src, pos, "'", error_on=ILLEGAL_LITERAL_STR_CHARS, error_on_eof=True
    )
    return pos + 1, src[start_pos:pos]  # Skip ending apostrophe


def parse_multiline_str(src: str, pos: Pos, *, literal: bool) -> Tuple[Pos, str]:
    pos += 3
    if src[pos : pos + 1] == "\n":
        pos += 1

    if literal:
        delim = "'"
        illegal_chars = ILLEGAL_MULTILINE_LITERAL_STR_CHARS
        escape_parser = None
    else:
        delim = '"'
        illegal_chars = ILLEGAL_MULTILINE_BASIC_STR_CHARS
        escape_parser = parse_basic_str_escape_multiline
    pos, result = parse_string(
        src,
        pos,
        delim=delim,
        delim_len=3,
        error_on=illegal_chars,
        parse_escapes=escape_parser,
    )

    # Add at maximum two extra apostrophes/quotes if the end sequence
    # is 4 or 5 chars long instead of just 3.
    if src[pos : pos + 1] != delim:
        return pos, result
    pos += 1
    if src[pos : pos + 1] != delim:
        return pos, result + delim
    pos += 1
    return pos, result + (delim * 2)


def parse_string(
    src: str,
    pos: Pos,
    *,
    delim: str,
    delim_len: int,
    error_on: Iterable[str],
    parse_escapes: Optional[Callable[[str, Pos], Tuple[Pos, str]]] = None,
) -> Tuple[Pos, str]:
    expect_after = delim * (delim_len - 1)
    result = ""
    start_pos = pos
    while True:
        try:
            char = src[pos]
        except IndexError:
            raise suffixed_err(src, pos, "Unterminated string")
        if char == delim:
            if src[pos + 1 : pos + delim_len] == expect_after:
                return pos + delim_len, result + src[start_pos:pos]
            pos += 1
            continue
        if parse_escapes and char == "\\":
            result += src[start_pos:pos]
            pos, parsed_escape = parse_escapes(src, pos)
            result += parsed_escape
            start_pos = pos
            continue
        if char in error_on:
            raise suffixed_err(src, pos, f'Illegal character "{char!r}"')
        pos += 1


def parse_regex(src: str, pos: Pos, regex: "Pattern") -> Tuple[Pos, str]:
    match = regex.match(src, pos)
    if not match:
        raise suffixed_err(src, pos, "Unexpected sequence")
    match_str = match.group()
    pos = match.end()
    return pos, match_str


def parse_datetime(
    src: str, pos: Pos, match: "Match"
) -> Tuple[Pos, Union[datetime, date]]:
    match_str = match.group()
    pos = match.end()
    groups: Any = match.groups()
    year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
    hour_str = groups[3]
    if hour_str is None:
        # Returning local date
        return pos, date(year, month, day)
    hour, minute, sec = int(hour_str), int(groups[4]), int(groups[5])
    micros_str, offset_hour_str = groups[6], groups[7]
    micros = int(micros_str[1:].ljust(6, "0")[:6]) if micros_str else 0
    if offset_hour_str is not None:
        offset_dir = 1 if "+" in match_str else -1
        tz: Optional[tzinfo] = timezone(
            timedelta(
                hours=offset_dir * int(offset_hour_str),
                minutes=offset_dir * int(groups[8]),
            )
        )
    elif "Z" in match_str:
        tz = timezone.utc
    else:  # local date-time
        tz = None
    return pos, datetime(year, month, day, hour, minute, sec, micros, tzinfo=tz)


def parse_localtime(src: str, pos: Pos, match: "Match") -> Tuple[Pos, time]:
    pos = match.end()
    groups = match.groups()
    hour, minute, sec = int(groups[0]), int(groups[1]), int(groups[2])
    micros_str = groups[3]
    micros = int(micros_str[1:].ljust(6, "0")[:6]) if micros_str else 0
    return pos, time(hour, minute, sec, micros)


def parse_dec_or_float(
    src: str, pos: Pos, match: "Match", parse_float: ParseFloat
) -> Tuple[Pos, Any]:
    match_str = match.group()
    pos = match.end()
    if "." in match_str or "e" in match_str or "E" in match_str:
        return pos, parse_float(match_str)
    return pos, int(match_str)


def parse_value(  # noqa: C901
    src: str, pos: Pos, parse_float: ParseFloat
) -> Tuple[Pos, Any]:
    try:
        char: Optional[str] = src[pos]
    except IndexError:
        char = None

    # Basic strings
    if char == '"':
        if src[pos + 1 : pos + 3] == '""':
            return parse_multiline_str(src, pos, literal=False)
        return parse_basic_str(src, pos)

    # Literal strings
    if char == "'":
        if src[pos + 1 : pos + 3] == "''":
            return parse_multiline_str(src, pos, literal=True)
        return parse_literal_str(src, pos)

    # Booleans
    if char == "t":
        if src[pos + 1 : pos + 4] == "rue":
            return pos + 4, True
    if char == "f":
        if src[pos + 1 : pos + 5] == "alse":
            return pos + 5, False

    # Dates and times
    date_match = RE_DATETIME.match(src, pos)
    if date_match:
        return parse_datetime(src, pos, date_match)
    localtime_match = RE_LOCAL_TIME.match(src, pos)
    if localtime_match:
        return parse_localtime(src, pos, localtime_match)

    # Non-decimal integers
    if char == "0":
        second_char = src[pos + 1 : pos + 2]
        if second_char == "x":
            pos += 2
            pos, hex_str = parse_regex(src, pos, RE_HEX)
            return pos, int(hex_str, 16)
        if second_char == "o":
            pos += 2
            pos, oct_str = parse_regex(src, pos, RE_OCT)
            return pos, int(oct_str, 8)
        if second_char == "b":
            pos += 2
            pos, bin_str = parse_regex(src, pos, RE_BIN)
            return pos, int(bin_str, 2)

    # Decimal integers and "normal" floats.
    # The regex will greedily match any type starting with a decimal
    # char, so needs to be located after handling of non-decimal ints,
    # and dates and times.
    dec_match = RE_DEC_OR_FLOAT.match(src, pos)
    if dec_match:
        return parse_dec_or_float(src, pos, dec_match, parse_float)

    # Arrays
    if char == "[":
        return parse_array(src, pos, parse_float)

    # Inline tables
    if char == "{":
        return parse_inline_table(src, pos, parse_float)

    # Special floats
    first_three = src[pos : pos + 3]
    if first_three in {"inf", "nan"}:
        return pos + 3, parse_float(first_three)
    first_four = src[pos : pos + 4]
    if first_four in {"-inf", "+inf", "-nan", "+nan"}:
        return pos + 4, parse_float(first_four)

    raise suffixed_err(src, pos, "Invalid value")


def suffixed_err(src: str, pos: Pos, msg: str) -> TOMLDecodeError:
    """Return a `TOMLDecodeError` where error message is suffixed with
    coordinates in source."""

    def coord_repr(src: str, pos: Pos) -> str:
        if pos >= len(src):
            return "end of document"
        line = src.count("\n", 0, pos) + 1
        if line == 1:
            column = pos + 1
        else:
            column = pos - src.rindex("\n", 0, pos)
        return f"line {line}, column {column}"

    return TOMLDecodeError(f"{msg} (at {coord_repr(src, pos)})")


def is_unicode_scalar_value(codepoint: int) -> bool:
    return (0 <= codepoint <= 55295) or (57344 <= codepoint <= 1114111)
