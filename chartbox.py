# -*- coding: utf-8 -*-
"""Безопасное (насколько это возможно) построение графиков из кода, который пишет Gemini.

Слои защиты (это защита в глубину, а НЕ настоящая песочница):
  1. AST-проверка: только numpy/matplotlib/math…, никаких open/exec/eval/os/sys/dunder-атрибутов;
  2. код выполняется в ОТДЕЛЬНОМ процессе с пустым окружением (без токенов и кук);
  3. таймаут + лимиты CPU/памяти/размера файлов (Linux);
  4. savefig/show/close подменены — писать файлы куда угодно код не может;
  5. harden_process(): родитель становится non-dumpable, и дочерний процесс не прочтёт
     /proc/<pid>/environ бота (там лежит TELEGRAM_TOKEN и куки Google).
Для настоящей изоляции запускай бота в контейнере/под отдельным пользователем без секретов на диске.
"""
import ast
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


class ChartError(Exception):
    """Понятная пользователю причина, почему график не построен."""


ALLOWED_IMPORTS = frozenset({
    "matplotlib", "numpy", "math", "cmath", "fractions", "statistics",
    "itertools", "functools", "decimal",
})

FORBIDDEN_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "input", "globals", "locals",
    "vars", "getattr", "setattr", "delattr", "breakpoint", "exit", "quit", "help",
    "memoryview", "super",
})

FORBIDDEN_ATTRS = frozenset({
    # доступ к системе
    "os", "sys", "subprocess", "system", "popen", "Popen", "builtins", "importlib",
    "ctypes", "shutil", "pathlib", "socket", "io", "environ", "getenv", "putenv",
    "spawn", "fork", "execv", "execve", "exec_module", "run", "call", "check_output",
    # файлы
    "open", "read", "write", "remove", "unlink", "rmdir", "rename", "mkdir",
    "load", "loadtxt", "genfromtxt", "fromfile", "tofile", "save", "savetxt",
    "savez", "savez_compressed", "memmap", "DataSource", "open_memmap", "fromregex",
    "imread", "imsave", "get_sample_data", "rc_file", "rc_params_from_file",
    "ctypeslib", "f2py", "distutils",
    # рефлексия
    "eval", "exec", "compile", "getattr", "setattr", "delattr", "vars", "globals", "locals",
})

MAX_CODE_CHARS = 20000
MAX_FIGURES = 4


def _check_module(name):
    root = (name or "").split(".")[0]
    if root not in ALLOWED_IMPORTS:
        raise ChartError("в коде графика нельзя импортировать «%s» (разрешены: numpy, matplotlib, math…)" % (root or "?"))


def validate_chart_code(code):
    if len(code) > MAX_CODE_CHARS:
        raise ChartError("код графика слишком длинный")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ChartError("синтаксическая ошибка в коде графика (строка %s): %s" % (e.lineno, e.msg))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                _check_module(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise ChartError("относительные импорты запрещены")
            _check_module(node.module)
            for a in node.names:
                if a.name == "*":
                    raise ChartError("«import *» запрещён")
                if a.name.startswith("_") or a.name in FORBIDDEN_NAMES or a.name in FORBIDDEN_ATTRS:
                    raise ChartError("запрещённый импорт «%s»" % a.name)
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in FORBIDDEN_ATTRS:
                raise ChartError("запрещённая операция «.%s»" % node.attr)
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES or node.id.startswith("__"):
                raise ChartError("запрещённое имя «%s»" % node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "__" in node.value:
                raise ChartError("подозрительная строка в коде графика")


_HEADER = """\
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.figure import Figure as __bx_Figure
__bx_save = __bx_Figure.savefig
__bx_plt = plt
plt.rcParams["figure.figsize"] = (8, 5)
def __bx_noop(*a, **k):
    return None
plt.show = __bx_noop
plt.savefig = __bx_noop
plt.close = __bx_noop
__bx_Figure.savefig = __bx_noop
__bx_Figure.show = __bx_noop
"""

_FOOTER = """
for __bx_i, __bx_n in enumerate(__bx_plt.get_fignums()[:%d]):
    __bx_save(__bx_plt.figure(__bx_n), "chart_%%d.png" %% __bx_i, bbox_inches="tight", dpi=200)
""" % MAX_FIGURES

_LAUNCHER = """\
import sys
try:
    import resource
    for _lim, _val in ((resource.RLIMIT_CPU, 20), (resource.RLIMIT_AS, 2 << 30),
                       (resource.RLIMIT_FSIZE, 50 << 20), (resource.RLIMIT_CORE, 0)):
        resource.setrlimit(_lim, (_val, _val))
except Exception:
    pass
import runpy
runpy.run_path(sys.argv[1], run_name="__main__")
"""


def _child_env(tmp):
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(tmp),
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(tmp / "mpl"),
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "LANG": "C.UTF-8",
    }
    for k in ("SYSTEMROOT", "TEMP", "TMP"):  # нужны python на Windows
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def _short_error(stderr):
    lines = [ln.strip() for ln in stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    return (lines[-1] if lines else "неизвестная ошибка")[:300]


def _strip_fences(code):
    code = code.strip()
    m = re.match(r"```[^\n]*\n(.*?)\n?```\Z", code, re.DOTALL)
    return (m.group(1) if m else code).strip()


def render_charts(code, timeout=30):
    """Возвращает список PNG (bytes) или бросает ChartError."""
    code = _strip_fences(code)
    validate_chart_code(code)
    with tempfile.TemporaryDirectory(prefix="chartbox_") as td:
        tmp = Path(td)
        (tmp / "mpl").mkdir()
        script = tmp / "chart_main.py"
        script.write_text(_HEADER + "\n" + code + "\n" + _FOOTER, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable or "python3", "-I", "-c", _LAUNCHER, str(script)],
                cwd=td, env=_child_env(tmp), capture_output=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ChartError("код графика выполнялся слишком долго (> %d с)" % timeout)
        if proc.returncode != 0:
            raise ChartError(_short_error(proc.stderr))
        pngs = sorted(tmp.glob("chart_[0-9].png"))
        if not pngs:
            raise ChartError("график не был построен (нет ни одной фигуры)")
        return [p.read_bytes() for p in pngs[:MAX_FIGURES]]


def harden_process():
    """Linux: запретить чтение /proc/<pid>/environ и ptrace процесса бота дочерними процессами."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes
        return ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE = 4
    except Exception:
        return False
