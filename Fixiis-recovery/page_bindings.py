"""Page bindings: fill a page's slots with values at push time.

A *binding* is a named slot in a page. It says **where** to write (a piece of
text, an element, or a regular expression) and **what** to write there by
default. When a page is pushed, the operator can supply a value for any named
binding, and the copy of the page that is served gets that value instead of
whatever the stored HTML had.

    stored HTML                                  served to the visitor
    <span class="amount">$0.00</span>   +  amount=₦50,000
                                            ->  <span class="amount">₦50,000</span>

The stored page is never modified: substitution happens on the bytes being
served for that one push.

Three matchers, in order of how specific they are:

    text      every exact occurrence of a string         "Sign in" -> "Continue"
    selector  the content of elements matching a CSS
              selector (tag, .class, #id, [attr=v],
              descendants)                               ".amount" -> "₦50,000"
    regex     every match of a Python regular expression  r"\\$\\d+" -> "₦50,000"

Bindings without a ``name`` are plain find -> replace rules that apply to
every push (the behaviour that existed before values were possible). Bindings
**with** a name become fields the panel asks for at push time; when the
operator leaves a field empty, the saved default is used.

This module only rewrites the operator's own page markup. It does not read,
collect or transmit anything a visitor types.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_BINDINGS = 50
MAX_MATCH = 2000
MAX_VALUE = 5000
MATCH_KINDS = ("text", "selector", "regex")


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------

def normalise_binding(raw: Any) -> Optional[Dict[str, Any]]:
    """One binding from whatever the caller sent, or None when unusable.

    Accepts the pre-values shape too: ``{"find": "...", "replace": "..."}``
    becomes a text binding whose default value is ``replace``.
    """
    if not isinstance(raw, dict):
        return None
    match = str(raw.get("match") or "").strip().lower()
    find = raw.get("find")
    if find is None:
        find = raw.get("replace_from") or raw.get("query") or ""
    find = str(find)
    if not find:
        return None
    if not match:
        # No explicit kind: a find/replace pair is text, anything that looks
        # like a regular expression is a regex (regex is the only place a
        # pattern like "\\d+" makes sense).
        match = "text"
    if match not in MATCH_KINDS:
        match = "text"
    if len(find) > MAX_MATCH:
        find = find[:MAX_MATCH]

    value = raw.get("value")
    if value is None:
        value = raw.get("replace", "")
    value = str(value)[:MAX_VALUE]

    name = str(raw.get("name") or "").strip()[:60]
    if name and not re.match(r"^[A-Za-z0-9 _.\-]+$", name):
        # Names are typed by an operator on a phone; keep them boring.
        name = re.sub(r"[^A-Za-z0-9 _.\-]", "", name).strip()[:60]

    if match == "regex":
        try:
            re.compile(find)
        except re.error as exc:
            raise ValueError(f"regex does not compile: {exc}")

    binding: Dict[str, Any] = {
        "name": name,
        "match": match,
        "find": find,
        "value": value,
        "escape": raw.get("escape", True) is not False,
    }
    if raw.get("label"):
        binding["label"] = str(raw["label"])[:80]
    return binding


def normalise_bindings(raw: Any) -> List[Dict[str, Any]]:
    """Validate a whole list; raises ValueError on a broken regex."""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_BINDINGS]:
        try:
            binding = normalise_binding(item)
        except ValueError:
            raise
        if binding:
            out.append(binding)
    return out


def slot_key(binding: Dict[str, Any]) -> str:
    """The name a push-time value is filed under.

    A binding's name if it has one, otherwise the thing it binds (the text,
    the selector or the pattern). That is what makes an un-named binding still
    a per-visitor question instead of one universal rewrite.
    """
    binding = binding or {}
    name = str(binding.get("name") or "").strip()
    if name:
        return name
    return str(binding.get("find") or "")[:200]


def _label_for(binding: Dict[str, Any]) -> str:
    """What to show above the input the operator types into."""
    label = str(binding.get("label") or "").strip()
    if label:
        return label[:80]
    shown = " ".join(str(binding.get("find") or "").split())
    if len(shown) > 48:
        shown = shown[:47] + "\u2026"
    return shown


def fields_for(bindings: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The questions a panel should ask before pushing this page.

    **Every** binding is a question: the operator decides what each visitor
    sees, rather than one rewrite applying to everyone. The saved value comes
    along as the default so the form can pre-fill it.
    """
    seen = set()
    fields: List[Dict[str, Any]] = []
    for b in bindings or []:
        if not isinstance(b, dict):
            continue
        key = slot_key(b)
        if not key or key in seen:
            continue
        seen.add(key)
        fields.append({
            "name": key,                       # what the panel sends back
            "label": _label_for(b),            # what it shows above the input
            "find": b.get("find") or "",
            "match": b.get("match") or "text",
            "default": b.get("value") or "",
        })
    return fields


# ---------------------------------------------------------------------------
# a very small HTML element tree, enough for selector bindings
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(
    r"<!--[\s\S]*?-->"                 # comment
    r"|<![^>]*>"                       # doctype / bogus comment
    r"|<(/?)([a-zA-Z][\w:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>"   # open/close tag
)

_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


class _Element:
    __slots__ = ("tag", "attrs", "inner_start", "inner_end", "parent")

    def __init__(self, tag: str, attrs: str, inner_start: int, parent):
        self.tag = tag
        self.attrs = attrs
        self.inner_start = inner_start
        self.inner_end: Optional[int] = None
        self.parent = parent


def _parse_attrs(raw: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for m in re.finditer(r"([\w:-]+)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+))?", raw or ""):
        key = m.group(1).lower()
        value = m.group(2) or ""
        if value[:1] in "\"'":
            value = value[1:-1]
        attrs.setdefault(key, value)
    return attrs


def _elements(html: str) -> List[_Element]:
    """Flat list of elements with the character range of their content."""
    stack: List[_Element] = []
    out: List[_Element] = []
    for m in _TAG_RE.finditer(html):
        closing, tag, attrs = m.group(1), m.group(2), m.group(3)
        if tag is None:
            continue
        tag = tag.lower()
        if closing:
            # Close the nearest matching open element; unmatched closers are
            # ignored (real pages are full of those).
            for i in range(len(stack) - 1, -1, -1):
                if stack[i].tag == tag:
                    for el in stack[i:]:
                        if el.inner_end is None:
                            el.inner_end = m.start()
                    del stack[i:]
                    break
            continue
        parent = stack[-1] if stack else None
        el = _Element(tag, attrs, m.end(), parent)
        out.append(el)
        if tag in _VOID or attrs.rstrip().endswith("/"):
            el.inner_end = m.end()
        else:
            stack.append(el)
    for el in stack:
        if el.inner_end is None:
            el.inner_end = len(html)
    return out


# ---- CSS selector subset: compounds joined by descendant spaces -----------

_COMPOUND_RE = re.compile(
    r"(?P<tag>[a-zA-Z][\w:-]*|\*)?"
    r"(?P<rest>(?:[.#][\w:-]+|\[[^\]]*\])*)"
)
_PART_RE = re.compile(r"([.#])([\w:-]+)|\[([^\]]*)\]")


class _Compound:
    __slots__ = ("tag", "id", "classes", "attrs")

    def __init__(self, tag=None, id=None, classes=None, attrs=None):
        self.tag = tag
        self.id = id
        self.classes = classes or []
        self.attrs = attrs or []

    def matches(self, el: _Element, attrs: Dict[str, str]) -> bool:
        if self.tag and self.tag != "*" and el.tag != self.tag:
            return False
        if self.id and attrs.get("id") != self.id:
            return False
        classes = (attrs.get("class") or "").split()
        for cls in self.classes:
            if cls not in classes:
                return False
        for key, expected in self.attrs:
            if key not in attrs:
                return False
            if expected is not None and attrs.get(key) != expected:
                return False
        return True


def _parse_selector(selector: str) -> List[_Compound]:
    selector = " ".join(selector.split())
    if not selector:
        return []
    compounds: List[_Compound] = []
    for part in selector.split(" "):
        m = _COMPOUND_RE.fullmatch(part)
        if not m:
            return []
        tag = (m.group("tag") or "*").lower()
        compound = _Compound(tag=tag)
        for pm in _PART_RE.finditer(m.group("rest") or ""):
            if pm.group(1) == ".":
                compound.classes.append(pm.group(2))
            elif pm.group(1) == "#":
                compound.id = pm.group(2)
            else:
                body = (pm.group(3) or "").strip()
                if "=" in body:
                    key, _, value = body.partition("=")
                    value = value.strip().strip("\"'")
                    compound.attrs.append((key.strip().lower(), value))
                else:
                    compound.attrs.append((body.lower(), None))
        compounds.append(compound)
    return compounds


def _matches_chain(el: _Element, compounds: List[_Compound], attrs_of) -> bool:
    """Right-most compound is the element itself; the rest walk ancestors."""
    if not compounds:
        return False
    if not compounds[-1].matches(el, attrs_of(el)):
        return False
    remaining = list(reversed(compounds[:-1]))
    node = el.parent
    while remaining and node is not None:
        if remaining[0].matches(node, attrs_of(node)):
            remaining.pop(0)
        node = node.parent
    return not remaining


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------

def _escape(value: str) -> str:
    return _html.escape(value, quote=False)


def _apply_text(html: str, find: str, value: str) -> Tuple[str, int]:
    if not find or find not in html:
        return html, 0
    count = html.count(find)
    return html.replace(find, value), count


def _apply_regex(html: str, pattern: str, value: str) -> Tuple[str, int]:
    try:
        compiled = re.compile(pattern)
    except re.error:
        return html, 0
    # The replacement is literal: an operator typing "\\1" means a backslash
    # and a one, not a group.
    return compiled.subn(lambda _m: value, html)


def _apply_selector(html: str, selector: str, value: str) -> Tuple[str, int]:
    compounds = _parse_selector(selector)
    if not compounds:
        return html, 0
    elements = _elements(html)
    attr_cache: Dict[int, Dict[str, str]] = {}

    def attrs_of(el: _Element) -> Dict[str, str]:
        key = id(el)
        if key not in attr_cache:
            attr_cache[key] = _parse_attrs(el.attrs)
        return attr_cache[key]

    spans: List[Tuple[int, int]] = []
    for el in elements:
        if el.inner_start is None or el.inner_end is None:
            continue
        if el.inner_end < el.inner_start:
            continue
        if _matches_chain(el, compounds, attrs_of):
            spans.append((el.inner_start, el.inner_end))

    if not spans:
        return html, 0

    # Outermost first, and drop anything nested inside an already replaced
    # element: writing the ancestor's content replaces the descendant anyway,
    # and applying both would corrupt the offsets.
    spans.sort(key=lambda s: (s[0], -s[1]))
    chosen: List[Tuple[int, int]] = []
    for start, end in spans:
        if chosen and start >= chosen[-1][0] and end <= chosen[-1][1]:
            continue
        chosen.append((start, end))

    out = html
    for start, end in sorted(chosen, key=lambda s: s[0], reverse=True):
        out = out[:start] + value + out[end:]
    return out, len(chosen)


def apply_bindings(
    html: str,
    bindings: Iterable[Dict[str, Any]],
    values: Optional[Dict[str, str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Rewrite ``html`` and report what happened.

    ``values`` maps a binding *name* to the operator's value for this push.
    A named binding with no value falls back to its saved default.
    """
    report: Dict[str, Any] = {"applied": [], "skipped": [], "total_replacements": 0}
    if not html or not bindings:
        return html, report
    values = {str(k): v for k, v in (values or {}).items() if v is not None}

    for raw in bindings:
        try:
            binding = normalise_binding(raw)
        except ValueError:
            report["skipped"].append({"name": "", "why": "regex does not compile"})
            continue
        if not binding:
            continue
        key = slot_key(binding)
        value = None
        if key and key in values:
            value = str(values[key])
        if value is None:
            value = binding["value"]
        if value == "":
            # An empty value means "leave the page alone": an operator who
            # cleared a field wants the original text, not a blank screen.
            report["skipped"].append({"name": key or binding["find"], "why": "empty value"})
            continue
        if binding["escape"]:
            value = _escape(value)

        kind = binding["match"]
        if kind == "selector":
            new_html, count = _apply_selector(html, binding["find"], value)
        elif kind == "regex":
            new_html, count = _apply_regex(html, binding["find"], value)
        else:
            new_html, count = _apply_text(html, binding["find"], value)

        if count:
            html = new_html
            report["total_replacements"] += count
            report["applied"].append({
                "name": key or binding["find"],
                "match": kind,
                "count": count,
                "from_value": key in values,
            })
        else:
            report["skipped"].append({"name": key or binding["find"], "why": "no match"})
    return html, report
