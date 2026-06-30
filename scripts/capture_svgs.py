#!/usr/bin/env python3
"""Capture discord_builder.py output as SVGs via rich recording."""

import os, sys

scripts_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(scripts_dir)
sys.path.insert(0, project_dir)
os.chdir(project_dir)

import discord_builder as db
from rich.console import Console as RichConsole

OUT = os.path.join(project_dir, "assets")
os.makedirs(OUT, exist_ok=True)

def capture(name, fn, title="Discord Server Builder"):
    c = RichConsole(record=True, width=78, force_terminal=True, color_system="truecolor")
    old = db.console
    db.console = c
    try:
        fn()
    finally:
        db.console = old
    svg = c.export_svg(title=title)
    path = os.path.join(OUT, f"{name}.svg")
    with open(path, "w") as f:
        f.write(svg)
    print(f"  ✓ {path}")

def render_banner():
    db.banner()
    db.console.print("\n\n  [bold]A tool to generate, validate, and apply[/bold]")
    db.console.print("  [bold]Discord server configurations with AI.[/bold]\n")

def render_validate():
    db.cmd_validate("example_config.json")

def render_validate2():
    db.cmd_validate("config.json")

def render_prompt():
    db.cmd_prompt("A community server for open-source developers")

capture("banner", render_banner)
capture("validate", render_validate, title="discord-builder validate")
capture("validate_demo", render_validate2, title="discord-builder validate (demo)")
capture("prompt", render_prompt, title="discord-builder prompt")

print("\nDone — SVGs saved to assets/")
