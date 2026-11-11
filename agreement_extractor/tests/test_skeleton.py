"""Build-order step 1: config, cache, logging and the data contract."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config as config_module  # noqa: E402
from src.cache import StageCache, bytes_sha256, file_sha256  # noqa: E402
from src.logging_setup import RunLog  # noqa: E402
from src.models import Cell, Table, normalise_bbox, union  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# -- config --------------------------------------------------------------------------------

def test_the_shipped_config_loads():
    config = config_module.load(ROOT / "config.yaml")
    assert config.render.dpi == 200
    assert config.ocr.disable_onednn is True, "oneDNN must stay off; see ocr.py"
    assert config.llm.temperature == 0.0, "re-runs must extract identically"
    assert config.paths.input.is_absolute()


def test_relative_paths_resolve_against_the_project_not_the_shell(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("paths:\n  cache: somewhere/else\n", encoding="utf-8")
    config = config_module.load(path)
    assert config.paths.cache == (ROOT / "somewhere/else").resolve()


def test_a_mistyped_key_is_an_error_not_a_silent_default(tmp_path):
    """A typo would otherwise keep the default and change results without saying so."""
    path = tmp_path / "c.yaml"
    path.write_text("render:\n  dpi: 300\n  formatt: png\n", encoding="utf-8")
    with pytest.raises(config_module.ConfigError) as exc:
        config_module.load(path)
    assert "formatt" in str(exc.value) and "render" in str(exc.value)


def test_a_mistyped_section_is_an_error(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("grouding:\n  fuzzy_threshold: 90\n", encoding="utf-8")
    with pytest.raises(config_module.ConfigError, match="grouding"):
        config_module.load(path)


def test_missing_sections_keep_their_defaults(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("render:\n  dpi: 300\n", encoding="utf-8")
    config = config_module.load(path)
    assert config.render.dpi == 300
    assert config.grounding.fuzzy_threshold == 85


def test_a_missing_config_file_says_so(tmp_path):
    with pytest.raises(config_module.ConfigError, match="No config file"):
        config_module.load(tmp_path / "nope.yaml")


# -- cache ---------------------------------------------------------------------------------

def test_cache_round_trips(tmp_path):
    cache = StageCache(tmp_path, "S2", {"dpi": 200})
    assert cache.get("abc") is None
    cache.put("abc", {"pages": [1, 2, 3]})
    assert cache.get("abc") == {"pages": [1, 2, 3]}


def test_changed_settings_invalidate_the_entry(tmp_path):
    """Boxes cached at 150 dpi must not be reused in a 200 dpi run."""
    StageCache(tmp_path, "S1", {"dpi": 150}).put("abc", {"bbox": [1, 2, 3, 4]})
    assert StageCache(tmp_path, "S1", {"dpi": 200}).get("abc") is None
    assert StageCache(tmp_path, "S1", {"dpi": 150}).get("abc") is not None


def test_a_truncated_entry_is_discarded_rather_than_crashing(tmp_path):
    cache = StageCache(tmp_path, "S0", {})
    cache.put("abc", {"ok": True})
    cache.path("abc").write_text('{"payload": {"ok"', encoding="utf-8")
    assert cache.get("abc") is None


def test_writes_are_atomic(tmp_path):
    """A run interrupted mid-write must not leave a half-file the next run trusts."""
    cache = StageCache(tmp_path, "S0", {})
    cache.put("abc", {"n": 1})
    circular: dict = {}
    circular["self"] = circular  # json.dump raises part-way through writing
    with pytest.raises(ValueError):
        cache.put("def", circular)
    assert not list(cache.dir.glob("*.tmp")), "temp files must be cleaned up"
    assert cache.get("abc") == {"n": 1}
    assert cache.get("def") is None


def test_identical_bytes_hash_the_same(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_bytes(b"%PDF-1.4 same")
    b.write_bytes(b"%PDF-1.4 same")
    assert file_sha256(a) == file_sha256(b) == bytes_sha256(b"%PDF-1.4 same")


# -- the data contract ---------------------------------------------------------------------

@pytest.mark.parametrize("bbox,size,expected", [
    ((0, 0, 100, 50), (1000, 500), (0, 0, 100, 100)),
    ((250, 125, 750, 375), (1000, 500), (250, 250, 750, 750)),
])
def test_normalised_boxes_are_resolution_independent(bbox, size, expected):
    assert normalise_bbox(bbox, *size) == expected


def test_normalising_against_a_zero_size_page_is_refused():
    with pytest.raises(ValueError):
        normalise_bbox((0, 0, 1, 1), 0, 100)


def test_union_is_how_a_multi_token_value_gets_its_box():
    assert union([(10, 20, 30, 40), (35, 18, 60, 44)]) == (10, 18, 60, 44)
    with pytest.raises(ValueError):
        union([])


def test_a_table_renders_with_a_citable_id_per_cell():
    table = Table("p3:T1", (0, 0, 100, 50), 2, 2, [
        Cell("p3:T1:R0C0", 0, 0, "Product", (0, 0, 50, 25)),
        Cell("p3:T1:R0C1", 0, 1, "Charges", (50, 0, 100, 25)),
        Cell("p3:T1:R1C0", 1, 0, "E-sign", (0, 25, 50, 50)),
        Cell("p3:T1:R1C1", 1, 1, "INR 15", (50, 25, 100, 50)),
    ])
    markdown = table.to_markdown()
    assert "INR 15 <p3:T1:R1C1>" in markdown
    assert markdown.splitlines()[1] == "|---|---|"


def test_a_missing_cell_leaves_a_blank_rather_than_shifting_the_row():
    table = Table("p1:T1", (0, 0, 10, 10), 1, 3,
                  [Cell("p1:T1:R0C2", 0, 2, "INR 12", (0, 0, 5, 5))])
    assert table.to_markdown().splitlines()[0] == "|  |  | INR 12 <p1:T1:R0C2> |"


# -- run log -------------------------------------------------------------------------------

def test_the_run_log_counts_outcomes_and_lists_failures():
    run_log = RunLog()
    run_log.add("S2", "a.pdf", "OK", 1.5)
    run_log.add("S2", "b.pdf", "FAILED", 0.2, "corrupt")
    run_log.add("S2", "c.pdf", "CACHED")
    assert [r.file_name for r in run_log.failures()] == ["b.pdf"]
    assert "FAILED 1" in run_log.summary() and "OK 1" in run_log.summary()


# -- preflight -----------------------------------------------------------------------------

def test_preflight_reports_the_missing_golden_set_rather_than_assuming_it():
    from src import preflight

    config = config_module.load(ROOT / "config.yaml")
    checks = {c.name: c for c in preflight.run(config)}
    assert "golden set" in checks
    assert checks["Python"].ok
    # Whatever the state of the machine, every failing check must name a fix.
    for check in checks.values():
        if not check.ok:
            assert check.fix, f"{check.name} failed without telling the user what to do"


def test_every_stage_module_imports():
    """The skeleton's seams must be real modules, not names in a README."""
    import importlib

    for name in ("inventory", "render", "text_layer", "ocr", "layout",
                 "llm_input", "extract", "ground", "validate", "export"):
        module = importlib.import_module(f"src.{name}")
        assert module.__doc__, f"src/{name}.py needs a docstring explaining the stage"
