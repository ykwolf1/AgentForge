"""内存管理命令实现"""
from pathlib import Path
from rich import get_console
from rich.panel import Panel
from rich.table import Table

from .base_command import BaseCommand, CommandAction, CommandResult


class MemoryCommand(BaseCommand):
    def __init__(self):
        super().__init__("memory", "Commands for interacting with memory.")
        self.console = get_console()

    async def execute(self, context, args: str) -> CommandResult:
        if not args:
            self._show_memory_help()
            return CommandResult(action=CommandAction.HANDLED)

        subcommand = args.split()[0].lower()

        if subcommand == "show":
            self._show_memory()
        elif subcommand == "add":
            self._add_memory(args)
        elif subcommand == "refresh":
            self._refresh_memory()
        else:
            self._show_memory_help()

        return CommandResult(action=CommandAction.HANDLED)

    def _memory_dir(self) -> Path:
        return Path.home() / ".agentforge" / "memory" / "projects" / "default"

    def _show_memory_help(self):
        help_content = """[bold cyan]Memory Commands:[/bold cyan]

[purple]/memory show[/purple] - Show saved memory files
[purple]/memory add <content>[/purple] - Add a note to memory
[purple]/memory refresh[/purple] - Reload memory files"""
        panel = Panel(help_content, title="Memory Help", border_style="blue")
        self.console.print(panel)

    def _show_memory(self):
        """列出记忆目录下的所有 .md 文件"""
        mem_dir = self._memory_dir()
        if not mem_dir.exists():
            self.console.print("[yellow]No memory files found.[/yellow]")
            return

        files = sorted(mem_dir.glob("*.md"))
        if not files:
            self.console.print("[yellow]No memory files found.[/yellow]")
            return

        table = Table(title="Memory Files", border_style="blue")
        table.add_column("File", style="cyan")
        table.add_column("Size", style="green", justify="right")
        for f in files:
            table.add_row(f.name, f"{f.stat().st_size} B")
        self.console.print(table)

    def _add_memory(self, args: str):
        """添加一条记忆到 note.md"""
        content = " ".join(args.split()[1:])
        if not content:
            self.console.print("[red]Usage: /memory add <content>[/red]")
            return

        mem_dir = self._memory_dir()
        mem_dir.mkdir(parents=True, exist_ok=True)
        note_file = mem_dir / "notes.md"

        with open(note_file, "a", encoding="utf-8") as f:
            f.write(f"\n- {content}\n")

        self.console.print(f"[green]Added to memory: {content}[/green]")

    def _refresh_memory(self):
        """重新扫描记忆文件"""
        mem_dir = self._memory_dir()
        if not mem_dir.exists():
            self.console.print("[yellow]No memory directory.[/yellow]")
            return
        files = list(mem_dir.glob("*.md"))
        self.console.print(f"[green]Memory refreshed: {len(files)} files found.[/green]")
