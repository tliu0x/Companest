"""Apply a module move map to a checkout.

Usage: python restructure.py <repo_root> <layout.json> [--dry-run]

layout.json: {"moves": [{"from": "companest/x.py", "to": "companest/pkg/x.py"}, ...], "deletes": ["scripts/y.py", ...]}

Steps (all computed against the tree as it is before any move):
  1. rewrite relative imports and "from <pkg> import <submodule>" statements with ast
  2. rewrite absolute dotted references (imports, patch targets, docs) with one regex pass
  3. rewrite file-path references in text files
  4. git mv / git rm, creating __init__.py for new packages
Entries whose source file does not exist in this tree are skipped, so the same
layout can be applied to trees that differ by a few files.
"""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

TEXT_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".sh", ".txt", ".cfg", ".ini", ".json", ".tf", ""}
SKIP_FILES = {"uv.lock", "LICENSE"}


def dotted(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    layout = json.loads(Path(sys.argv[2]).read_text())
    dry_run = "--dry-run" in sys.argv

    tracked = [p for p in git(root, "ls-files").splitlines() if (root / p).is_file()]
    moves = {m["from"]: m["to"] for m in layout["moves"] if m["from"] != m["to"] and m["from"] in tracked}
    deletes = [d for d in layout.get("deletes", []) if d in tracked]

    # Module tables. Only files inside the package take part in import resolution.
    old_modules = {dotted(p): p for p in tracked if p.startswith("companest/") and p.endswith(".py")}
    new_path = {p: moves.get(p, p) for p in tracked}
    new_name = {name: dotted(new_path[path]) for name, path in old_modules.items()}
    for src, dst in moves.items():
        if src.endswith(".py") and dst.startswith("companest/") and not src.startswith("companest/"):
            new_name[dotted(src)] = dotted(dst)
    is_package = {name for name, path in old_modules.items() if path.endswith("__init__.py")}

    def package_of(module: str, path: str) -> str:
        return module if path.endswith("__init__.py") else module.rpartition(".")[0]

    def relative(from_pkg: str, target: str) -> tuple[int, str]:
        """Express absolute module `target` relative to package `from_pkg`."""
        a, b = from_pkg.split("."), target.split(".")
        common = 0
        while common < min(len(a), len(b)) and a[common] == b[common]:
            common += 1
        return len(a) - common + 1, ".".join(b[common:])

    def rewrite_imports(path: str, source: str) -> str:
        inside = path.startswith("companest/")
        old_mod = dotted(path)
        old_pkg = package_of(old_mod, path) if inside else None
        dst = new_path[path]
        new_inside = dst.startswith("companest/")
        new_pkg = package_of(dotted(dst), dst) if new_inside else None

        lines = source.splitlines(keepends=True)
        edits = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                if not inside:
                    continue
                base = old_pkg.split(".")
                base = base[: len(base) - (node.level - 1)]
                target_old = ".".join(base + ([node.module] if node.module else []))
            else:
                target_old = node.module or ""
                if not (target_old == "companest" or target_old.startswith("companest.")):
                    continue

            # Group imported names by the module they must now come from.
            groups: dict[str, list[ast.alias]] = {}
            for alias in node.names:
                sub_old = f"{target_old}.{alias.name}"
                if alias.name != "*" and sub_old in old_modules:
                    sub_new = new_name[sub_old]
                    parent, _, leaf = sub_new.rpartition(".")
                    item = ast.alias(name=leaf, asname=alias.asname or (alias.name if leaf != alias.name else None))
                    groups.setdefault(parent, []).append(item)
                else:
                    groups.setdefault(new_name.get(target_old, target_old), []).append(alias)

            statements = []
            for target_new, names in groups.items():
                if node.level and new_inside:
                    level, module = relative(new_pkg, target_new)
                    stmt = ast.ImportFrom(module=module or None, names=names, level=level)
                else:
                    stmt = ast.ImportFrom(module=target_new, names=names, level=0)
                statements.append(stmt)

            original = ast.ImportFrom(module=node.module, names=node.names, level=node.level)
            if len(statements) == 1 and ast.unparse(statements[0]) == ast.unparse(original):
                continue

            indent = " " * node.col_offset
            multiline = node.end_lineno > node.lineno
            rendered = []
            for stmt in statements:
                if multiline and len(stmt.names) > 1:
                    head = "from " + "." * stmt.level + (stmt.module or "") + " import ("
                    body = "".join(
                        f"{indent}    {a.name}{' as ' + a.asname if a.asname else ''},\n" for a in stmt.names
                    )
                    rendered.append(f"{head}\n{body}{indent})")
                else:
                    rendered.append(ast.unparse(stmt))
            first = lines[node.lineno - 1]
            last = lines[node.end_lineno - 1]
            prefix = first[: node.col_offset]
            suffix = last[node.end_col_offset:]
            text = prefix + f"\n{indent}".join(rendered) + suffix
            edits.append((node.lineno - 1, node.end_lineno, text))

        for start, end, text in sorted(edits, reverse=True):
            lines[start:end] = [text]
        return "".join(lines)

    renamed = {old: new for old, new in new_name.items() if old != new and old.startswith("companest.")}
    name_re = re.compile(
        r"(?<![\w.])(" + "|".join(re.escape(n) for n in sorted(renamed, key=len, reverse=True)) + r")(?![\w])"
    ) if renamed else None
    path_re = re.compile(
        r"(?<![\w./-])(" + "|".join(re.escape(p) for p in sorted(moves, key=len, reverse=True)) + r")(?![\w])"
    ) if moves else None

    changed = 0
    for path in tracked:
        if path in deletes or Path(path).name in SKIP_FILES or Path(path).suffix not in TEXT_SUFFIXES:
            continue
        file = root / path
        try:
            source = file.read_text()
        except UnicodeDecodeError:
            continue
        text = source
        if path.endswith(".py"):
            text = rewrite_imports(path, text)
        if name_re:
            text = name_re.sub(lambda m: renamed[m.group(1)], text)
        if path_re:
            text = path_re.sub(lambda m: moves[m.group(1)], text)
        if text != source:
            changed += 1
            if not dry_run:
                file.write_text(text)

    print(f"moves: {len(moves)}  deletes: {len(deletes)}  files rewritten: {changed}")
    if dry_run:
        return

    for src in deletes:
        git(root, "rm", "-q", src)
    for src, dst in sorted(moves.items()):
        (root / dst).parent.mkdir(parents=True, exist_ok=True)
        git(root, "mv", src, dst)
    for dst in sorted(set(moves.values())):
        if not dst.startswith("companest/"):
            continue
        init = (root / dst).parent / "__init__.py"
        if not init.exists():
            init.write_text("")
            git(root, "add", str(init.relative_to(root)))
            print(f"created {init.relative_to(root)}")
    for directory in sorted({str(Path(s).parent) for s in list(moves) + deletes}, key=len, reverse=True):
        d = root / directory
        if d.is_dir() and not any(p for p in d.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            subprocess.run(["rm", "-rf", str(d)], check=True)
            print(f"removed empty {directory}/")


if __name__ == "__main__":
    main()
