import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"


def _load_tree():
    return ast.parse(APP.read_text(encoding="utf-8"), filename=str(APP))


def _function_nodes(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def test_no_bare_cancel_event_reference():
    """cancel_event must be passed explicitly or accessed as self.cancel_event.

    This is a regression guard for the production crash:
    NameError: name 'cancel_event' is not defined.
    """
    tree = _load_tree()
    violations = []

    for function in _function_nodes(tree):
        parameter_names = {
            arg.arg
            for arg in (
                list(function.args.posonlyargs)
                + list(function.args.args)
                + list(function.args.kwonlyargs)
            )
        }
        if function.args.vararg:
            parameter_names.add(function.args.vararg.arg)
        if function.args.kwarg:
            parameter_names.add(function.args.kwarg.arg)

        if "cancel_event" in parameter_names:
            continue

        for node in ast.walk(function):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not function:
                # Nested functions are checked independently in their own scope.
                continue
            if (
                isinstance(node, ast.Name)
                and node.id == "cancel_event"
                and isinstance(node.ctx, ast.Load)
            ):
                violations.append(f"{APP}:{node.lineno}: bare cancel_event reference")

    assert not violations, "\n".join(violations)


def test_callback_methods_exist():
    """Callbacks passed through Tkinter scheduling/bind APIs must exist."""
    tree = _load_tree()
    class_methods = {}

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
                missing.append(f"{APP}:{arg.lineno}: missing callback method self.{arg.attr}")

    assert not missing, "\n".join(missing)
