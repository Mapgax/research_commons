"""Tests for canonical-file validation and all-or-nothing distribution."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_commons.universe import dist as dist_module
from research_commons.universe.dist import Target, distribute
from research_commons.universe.io import (
    UniverseRow,
    read_canonical,
    stage_consumer_csv,
    write_canonical,
)


def row(ticker: str = "AAPL") -> UniverseRow:
    return UniverseRow(
        company_name="Apple Inc",
        isin="US0378331005",
        bloomberg_ticker="AAPL US Equity",
        ticker=ticker,
        yahoo_symbol=ticker,
        currency="USD",
    )


# --- canonical file validation ----------------------------------------------


def test_missing_required_column_names_file_and_column(tmp_path: Path) -> None:
    path = tmp_path / "ticker_universe.csv"
    path.write_text(
        "ISIN;bloomberg_ticker;ticker\nUS0378331005;AAPL US Equity;AAPL\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        read_canonical(path)
    message = str(excinfo.value)
    assert "company_name" in message
    assert str(path) in message


def test_missing_key_columns_does_not_silently_return_empty(tmp_path: Path) -> None:
    """Without ISIN and ticker every row would be skipped — that must not look empty."""
    path = tmp_path / "ticker_universe.csv"
    path.write_text("company_name;bloomberg_ticker\nApple Inc;AAPL US Equity\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_canonical(path)


def test_canonical_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "ticker_universe.csv"
    write_canonical(path, [row()])
    loaded = read_canonical(path)
    assert len(loaded) == 1
    assert loaded[0].ticker == "AAPL"
    assert loaded[0].isin == "US0378331005"


def test_stage_consumer_csv_leaves_no_temp_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import research_commons.universe.io as io_module

    def boom(_rows: list[UniverseRow]) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(io_module, "render_consumer_csv", boom)
    with pytest.raises(OSError):
        stage_consumer_csv(tmp_path / "tickers.csv", [row()])
    assert list(tmp_path.iterdir()) == []


# --- distribution ------------------------------------------------------------


def _target(tmp_path: Path, name: str) -> Target:
    path = tmp_path / name
    path.mkdir()
    return Target(name=name, path=path, remote="example/repo")


def test_distribution_is_all_or_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staging failure on the second repo must leave the first one untouched."""
    first = _target(tmp_path, "first")
    second = _target(tmp_path, "second")
    (first.path / "tickers.csv").write_text("OLD", encoding="utf-8")
    (second.path / "tickers.csv").write_text("OLD", encoding="utf-8")

    monkeypatch.setattr(dist_module, "check_target", lambda *_args, **_kwargs: "")

    real_stage = dist_module.stage_consumer_csv

    def stage_but_fail_on_second(path: Path, rows: list[UniverseRow]) -> Path:
        if path.parent.name == "second":
            raise OSError("permission denied")
        return real_stage(path, rows)

    monkeypatch.setattr(dist_module, "stage_consumer_csv", stage_but_fail_on_second)

    results = distribute([first, second], [row()], apply=True)

    assert all(not result.ok for result in results)
    assert (first.path / "tickers.csv").read_text(encoding="utf-8") == "OLD"
    assert (second.path / "tickers.csv").read_text(encoding="utf-8") == "OLD"
    # No staged temp files may survive inside a consumer worktree.
    assert [p.name for p in first.path.iterdir()] == ["tickers.csv"]


def test_distribution_writes_every_target_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _target(tmp_path, "first")
    second = _target(tmp_path, "second")
    monkeypatch.setattr(dist_module, "check_target", lambda *_args, **_kwargs: "")

    results = distribute([first, second], [row()], apply=True)

    assert all(result.ok for result in results)
    for target in (first, second):
        content = (target.path / "tickers.csv").read_text(encoding="utf-8")
        assert content.startswith("company_name;ISIN;")
        assert "AAPL" in content
        assert [p.name for p in target.path.iterdir()] == ["tickers.csv"]


def test_failed_check_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _target(tmp_path, "first")
    monkeypatch.setattr(dist_module, "check_target", lambda *_a, **_k: "dirty tickers.csv")

    results = distribute([first], [row()], apply=True)

    assert not results[0].ok
    assert not (first.path / "tickers.csv").exists()
