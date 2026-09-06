$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
uv sync
uv run ebook-to-pdf
