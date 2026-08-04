"""Command line entry point."""

import click

from relduty_deployer import __version__


@click.command()
@click.version_option(__version__)
def main() -> None:
    """Launch the RelEng deploy dashboard."""
    click.echo(f"relduty-deployer {__version__}")


if __name__ == "__main__":
    main()
