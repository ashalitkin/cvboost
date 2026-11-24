"""The synthetic `example` plugin: one object that exercises every hook of cvboost.plugins."""

from __future__ import annotations

import argparse
from pathlib import Path

from cvboost.plugins import NavItem, Plugin

HERE = Path(__file__).resolve().parent


def register_cli(sub: argparse._SubParsersAction) -> None:
    sub.add_parser("example", help="synthetic plugin command")


def dispatch_cli(settings, args: argparse.Namespace) -> int | None:
    if getattr(args, "command", None) != "example":
        return None
    print("example plugin ran")
    return 0


def register_web(app) -> None:
    from fastapi.responses import PlainTextResponse

    @app.get("/example")
    def example_page():
        return PlainTextResponse("example page")


PLUGIN = Plugin(
    name="example",
    migrations_package="tests.example_plugin.migrations",
    llm_kinds=("ex_classify", "ex_reply"),
    pause_kinds=("ex_classify",),
    trusted_alert_kinds=("ex_reply",),
    nav=(NavItem("example", "/example", "Example", after="crm"),),
    static_dir=HERE / "static",
    register_cli=register_cli,
    dispatch_cli=dispatch_cli,
    register_web=register_web,
)
