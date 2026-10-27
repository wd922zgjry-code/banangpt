# -*- coding: utf-8 -*-
"""Очистка ответа модели для Telegram.

Убирает Markdown (жирный, заголовки, ссылки) и LaTeX, но:
  * знаки умножения в формулах остаются (B = F / (I * l), m*g*h, (a+b)*(c+d));
  * степени вида x**2 превращаются в x^2, а не теряют звёздочки;
  * блоки кода, цены ($100) и хэштеги (#1) не трогаются.
"""
import re

# ───────────────────────── LaTeX → обычный текст ─────────────────────────

_SYMBOLS = {
    # греческие
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "tau": "τ",
    "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ",
    "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ",
    "Psi": "Ψ", "Omega": "Ω",
    # операторы и символы
    "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "sim": "~", "equiv": "≡", "propto": "∝", "infty": "∞",
    "partial": "∂", "nabla": "∇", "int": "∫", "iint": "∬", "iiint": "∭",
    "oint": "∮", "sum": "∑", "prod": "∏", "to": "→", "rightarrow": "→",
    "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐",
    "leftrightarrow": "↔", "Leftrightarrow": "⇔", "degree": "°", "circ": "°",
    "angle": "∠", "perp": "⊥", "parallel": "∥", "in": "∈", "notin": "∉",
    "subset": "⊂", "cup": "∪", "cap": "∩", "forall": "∀", "exists": "∃",
    "ldots": "…", "dots": "…", "cdots": "⋯", "ell": "ℓ", "hbar": "ħ",
    "quad": " ", "qquad": "  ",
    # функции
    "sin": "sin", "cos": "cos", "tan": "tan", "tg": "tg", "ctg": "ctg",
    "cot": "cot", "arcsin": "arcsin", "arccos": "arccos", "arctan": "arctan",
    "sinh": "sh", "cosh": "ch", "ln": "ln", "log": "log", "lg": "lg",
    "exp": "exp", "lim": "lim", "min": "min", "max": "max", "det": "det",
}

_SUP_MAP = str.maketrans("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
_SUP_OK = set("0123456789+-=()ni")
_SUB_MAP = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
_SUB_OK = set("0123456789+-=()")

_WRAP = (r"text|textrm|textbf|textit|mathrm|mathbf|mathit|mathsf|mathcal|"
         r"mathbb|boldsymbol|operatorname|mbox|rm|bf")
_RE_WRAP = re.compile(r"\\(?:%s)\s*\{([^{}]*)\}" % _WRAP)
_RE_ACCENT = re.compile(r"\\(vec|overrightarrow|bar|overline|hat|dot|ddot|tilde)\s*\{([^{}]*)\}")
_ACCENT = {"vec": "\u20d7", "overrightarrow": "\u20d7", "bar": "\u0304",
           "overline": "\u0304", "hat": "\u0302", "dot": "\u0307",
           "ddot": "\u0308", "tilde": "\u0303"}
_RE_FRAC = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_RE_SQRT_N = re.compile(r"\\sqrt\s*\[([^\]]*)\]\s*\{([^{}]*)\}")
_RE_SQRT = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_RE_CIRC = re.compile(r"\^\s*(?:\{\s*\\circ\s*\}|\\circ)")
_RE_SUP = re.compile(r"\^\s*(?:\{([^{}]*)\}|(-?[A-Za-z0-9α-ωΑ-Ω]))")
_RE_SUB = re.compile(r"_\s*(?:\{([^{}]*)\}|([0-9]))")
_RE_CMD = re.compile(r"\\([A-Za-z]+)")
_RE_LEFT_RIGHT = re.compile(r"\\(?:left|right|bigl|bigr|Bigl|Bigr|big|Big)\s*(\\[{}|]|[()\[\]|.])?")
_RE_SIMPLE = re.compile(r"[\w.,·×°²³⁴⁵⁶⁷⁸⁹⁰¹]+|\([^()]*\)|√\([^()]*\)")


def _paren(s):
    s = s.strip()
    return s if _RE_SIMPLE.fullmatch(s) else "(%s)" % s


def _sup(m):
    body = (m.group(1) if m.group(1) is not None else m.group(2)).strip().replace("−", "-")
    if body and all(ch in _SUP_OK for ch in body):
        return body.translate(_SUP_MAP)
    return "^" + body if len(body) == 1 else "^(%s)" % body


def _sub(m):
    body = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
    if body and all(ch in _SUB_OK for ch in body):
        return body.translate(_SUB_MAP)
    return "_" + body


def latex_to_text(s, strict=True):
    """strict=True — внутри формульных разделителей (всё чистим),
    strict=False — вне их (только заведомо известные команды)."""
    if "\\" not in s and not (strict and ("^" in s or "_" in s or "{" in s)):
        return s

    if strict:
        if "\\begin" in s:
            s = s.replace("&", "  ")
        s = re.sub(r"\\(?:begin|end)\s*\{[^{}]*\}", "", s)
        s = s.replace("\\\\", "\n")
        s = _RE_LEFT_RIGHT.sub(
            lambda m: "" if m.group(1) in (None, ".") else m.group(1).lstrip("\\"), s)

    for _ in range(8):
        before = s
        s = _RE_WRAP.sub(lambda m: m.group(1), s)
        s = _RE_ACCENT.sub(lambda m: m.group(2) + _ACCENT[m.group(1)], s)
        s = _RE_FRAC.sub(lambda m: "%s/%s" % (_paren(m.group(1)), _paren(m.group(2))), s)
        s = _RE_SQRT_N.sub(lambda m: "%s^(1/%s)" % (_paren(m.group(2)), m.group(1).strip()), s)
        s = _RE_SQRT.sub(lambda m: "√(%s)" % m.group(1).strip(), s)
        if s == before:
            break

    s = _RE_CIRC.sub("°", s)
    if strict:
        for _ in range(3):
            s = _RE_SUP.sub(_sup, s)
        s = _RE_SUB.sub(_sub, s)

    def sym(m):
        name = m.group(1)
        if name in _SYMBOLS:
            return _SYMBOLS[name]
        return name if strict else m.group(0)

    s = _RE_CMD.sub(sym, s)

    if strict:
        s = re.sub(r"\\[,;:]", " ", s)
        s = s.replace("\\!", "").replace("\\ ", " ")
        s = re.sub(r"\\([%_&#$])", r"\1", s)
        s = s.replace("{", "").replace("}", "").replace("\\", "")
        s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip() if strict else s


_RE_DISPLAY = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_RE_BRACKET = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
_RE_PAREN = re.compile(r"\\\((.+?)\\\)", re.DOTALL)
_RE_INLINE = re.compile(r"(?<![\\$\w])\$(?=[^\s$])([^$\n]+?)(?<=[^\s$\\])\$(?![\d$])")


def _looks_like_math(c):
    if re.fullmatch(r"[A-Za-zα-ωΑ-Ω][\w']{0,2}", c):
        return True
    return bool(re.search(r"[\\^_{}=+\-*/<>()]", c) and re.search(r"[A-Za-z\\α-ωΑ-Ω]", c))


def _convert_math_delimiters(t):
    t = _RE_DISPLAY.sub(lambda m: latex_to_text(m.group(1)), t)
    t = _RE_BRACKET.sub(lambda m: latex_to_text(m.group(1)), t)
    t = _RE_PAREN.sub(lambda m: latex_to_text(m.group(1)), t)
    t = _RE_INLINE.sub(
        lambda m: latex_to_text(m.group(1)) if _looks_like_math(m.group(1)) else m.group(0), t)
    return t


# ───────────────────────── Markdown → обычный текст ─────────────────────────

def _strip_markdown(t):
    t = re.sub(r"!?\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", t)
    # жирный/жирный курсив: открывающие ** не после буквы/цифры, поэтому x**2 не страдает
    t = re.sub(r"(?<![\w*])\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*(?![\w*])", r"\1", t)
    t = re.sub(r"(?<![\w*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![\w*])", r"\1", t)
    t = re.sub(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])", r"\1", t)
    # маркеры списка
    t = re.sub(r"(?m)^([ \t]*)[*+][ \t]+", r"\1• ", t)
    # курсив *слово*: не трогаем m*g*h, 2*3*4, (a+b)*(c+d), "F * m"
    t = re.sub(r"(?<![\w*)\]}])\*(?=[^\s*])([^*\n]*?[^\s*]|[^\s*])\*(?![\w*(\[{])", r"\1", t)
    # степени: x**2 → x^2, (a+b)**2 → (a+b)^2, a ** b → a ^ b
    t = re.sub(r"(?<=[\w)\]])\*\*(?=[\w(\[-])", "^", t)
    t = re.sub(r"(?<=\S) \*\* (?=\S)", " ^ ", t)
    t = t.replace("**", "")
    # заголовки и горизонтальные линии
    t = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+", "", t)
    t = re.sub(r"(?m)^[ \t]*([-*_])\1{2,}[ \t]*$", "", t)
    # inline-код
    t = re.sub(r"`([^`\n]+)`", r"\1", t)
    return t.replace("`", "")


_FENCE_RE = re.compile(r"(```[^\n]*\n.*?```)", re.DOTALL)
_FENCE_BODY_RE = re.compile(r"```[^\n]*\n(.*?)\n?```\Z", re.DOTALL)


def _clean_text_part(t):
    t = _convert_math_delimiters(t)
    t = latex_to_text(t, strict=False)
    t = _strip_markdown(t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t)


def clean_formatting(text):
    """Главная функция очистки. Код в ```блоках``` остаётся как есть (без ограждений)."""
    if not text:
        return ""
    out = []
    for chunk in _FENCE_RE.split(text):
        m = _FENCE_BODY_RE.match(chunk) if chunk.startswith("```") else None
        out.append("\n%s\n" % m.group(1) if m else _clean_text_part(chunk))
    return "".join(out).strip()


def split_blocks(text):
    """Для PDF/DOCX: [("text", очищенный текст) | ("code", код как есть)]."""
    blocks = []
    for chunk in _FENCE_RE.split(text or ""):
        m = _FENCE_BODY_RE.match(chunk) if chunk.startswith("```") else None
        if m:
            blocks.append(("code", m.group(1)))
        else:
            cleaned = _clean_text_part(chunk).strip()
            if cleaned:
                blocks.append(("text", cleaned))
    return blocks


# ───────────────────────── служебные метки и графики ─────────────────────────

_PY_FENCE_RE = re.compile(r"```[ \t]*(?:python|py)?[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _is_chart(code):
    return "plt." in code or "matplotlib" in code


def find_chart_code(text):
    for m in _PY_FENCE_RE.finditer(text or ""):
        if _is_chart(m.group(1)):
            return m.group(1).strip()
    return None


def remove_chart_blocks(text):
    return _PY_FENCE_RE.sub(lambda m: "" if _is_chart(m.group(1)) else m.group(0), text or "")


def strip_service_markers(text):
    """Для истории диалога: без меток, а графический код заменяется заглушкой."""
    if not text:
        return ""
    text = re.sub(r"\[SEND_AS_FILE:[^\]]+\]", "", text)
    text = re.sub(r"\[GENERATE_IMAGE:.*?\]", "", text, flags=re.DOTALL)
    text = _PY_FENCE_RE.sub(lambda m: "[построен график]" if _is_chart(m.group(1)) else m.group(0), text)
    return clean_formatting(text)


def split_text(text, limit=4000):
    """Режет по переводам строк/пробелам, а не посреди слова."""
    text = text or ""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n ")
    chunks.append(text)
    return [c for c in chunks if c.strip()] or [""]
