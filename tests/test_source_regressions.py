import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"


def _load_tree():
    return ast.parse(APP.read_text(encoding="utf-8"), filename=str(APP))


def _function_parameters(function):
    args = function.args
    names = {
        arg.arg
        for arg in (
            list(args.posonlyargs)
            + list(args.args)
            + list(args.kwonlyargs)
        )
    }
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


class _BareNameVisitor(ast.NodeVisitor):
    def __init__(self, name):
        self.name = name
        self.violations = []

    def visit_Name(self, node):
        if node.id == self.name and isinstance(node.ctx, ast.Load):
            self.violations.append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        # Nested functions have their own scope and are checked separately.
        return

    def visit_AsyncFunctionDef(self, node):
        return

    def visit_Lambda(self, node):
        return


def test_no_bare_cancel_event_reference():
    """cancel_event must be passed explicitly or accessed as self.cancel_event.

    Regression guard for the production crash:
    NameError: name 'cancel_event' is not defined.
    """
    tree = _load_tree()
    violations = []

    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        if "cancel_event" in _function_parameters(function):
            continue

        visitor = _BareNameVisitor("cancel_event")
        for statement in function.body:
            visitor.visit(statement)
        violations.extend(visitor.violations)

    assert not violations, "\n".join(
        f"{APP}:{node.lineno}: bare cancel_event reference"
        for node in violations
    )


def test_callback_methods_exist():
    """Callbacks passed through Tkinter scheduling/bind APIs must exist."""
    tree = _load_tree()
    class_methods = set()

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ParallelBackupApp":
            class_methods = {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            break

    assert class_methods, "ParallelBackupApp class was not found"

    callback_api_names = {"bind", "after", "after_idle", "after_cancel", "protocol"}
    missing = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in callback_api_names
        ):
            continue

        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if (
                isinstance(arg, ast.Attribute)
                and isinstance(arg.value, ast.Name)
                and arg.value.id == "self"
                and arg.attr.startswith("_")
                and arg.attr not in class_methods
            ):
                missing.append(
                    f"{APP}:{arg.lineno}: missing callback method self.{arg.attr}"
                )

    assert not missing, "\n".join(missing)
