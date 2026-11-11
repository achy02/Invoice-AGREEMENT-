"""The agreement pipeline must stay removable in one step.

The requirement is concrete: deleting `agreement_extractor/` must leave a working invoice
extractor behind. That only stays true if nothing leaks in either direction, and leaks are
easy to introduce by accident -- one `from app.pdf_text_extractor import ...` would do it,
and nothing would fail until the folder was deleted a year later.

So the boundary is a test rather than a note in a README.
"""
import ast
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent          # agreement_extractor/
PROJECT = HERE.parent                                  # the invoice project root

sys.path.insert(0, str(HERE))


def imported_modules(path: Path) -> set[str]:
    """Top-level module names imported by one Python file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py")
            if ".venv" not in p.parts and "__pycache__" not in p.parts]


# -- the invoice side must not know this folder exists --------------------------------------

def test_the_invoice_app_imports_nothing_from_here():
    offenders = []
    for folder in ("app", "tools", "tests"):
        for path in python_files(PROJECT / folder):
            leaked = imported_modules(path) & {"agreement_extractor", "src", "scripts"}
            if leaked:
                offenders.append(f"{path.relative_to(PROJECT)} imports {sorted(leaked)}")
    assert offenders == [], "\n".join(offenders)


def test_no_agreement_modules_are_left_in_the_invoice_app():
    stray = [p.name for p in (PROJECT / "app").glob("*.py") if "agreement" in p.name.lower()]
    assert stray == [], f"agreement code still in app/: {stray}"


def test_the_invoice_config_has_no_agreement_settings():
    text = (PROJECT / "app" / "config.py").read_text(encoding="utf-8")
    for name in ("AGREEMENT_", "OCR_DPI", "OCR_CACHE_DIR", "OCR_MIN_CONFIDENCE"):
        assert name not in text, f"app/config.py still defines {name}"


def test_the_invoice_requirements_do_not_pull_in_ocr():
    text = (PROJECT / "requirements.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "paddle" not in line.lower(), f"invoice requirements should not need {line!r}"


# -- this side must not reach into the invoice app ------------------------------------------

def test_this_pipeline_imports_nothing_from_the_invoice_app():
    offenders = []
    for path in python_files(HERE):
        leaked = imported_modules(path) & {"app"}
        if leaked:
            offenders.append(str(path.relative_to(HERE)))
    assert offenders == [], f"these files import the invoice app: {offenders}"


def test_this_pipeline_is_self_contained_on_disk():
    """Nothing here may point outside the folder, or removal would leave dangling paths."""
    from src import config as config_module

    config = config_module.load(HERE / "config.yaml")
    for name in ("input", "cache", "output", "page_images", "golden"):
        path = getattr(config.paths, name)
        assert HERE in path.parents or path == HERE, \
            f"paths.{name} points outside agreement_extractor: {path}"


def test_the_two_pipelines_write_two_different_workbooks():
    """Invoice data and agreement data must never land in the same file.

    The agreed process is: run the invoice extractor, then run this one, and end up with two
    workbooks. They write to different folders with different name prefixes, so neither can
    overwrite or be mistaken for the other.
    """
    from src import config as config_module

    agreement = config_module.load(HERE / "config.yaml")
    invoice_outputs = PROJECT / "outputs"

    assert agreement.paths.output != invoice_outputs
    assert HERE in agreement.paths.output.parents

    export_source = (HERE / "src" / "export.py").read_text(encoding="utf-8")
    assert "Agreement_Extraction_" in export_source

    invoice_source = (PROJECT / "app" / "excel_generator.py").read_text(encoding="utf-8")
    assert "Agreement" not in invoice_source, "the invoice writer must not know about agreements"


def test_the_invoice_pipeline_does_not_need_this_folder_to_run():
    """Import the invoice entry points with this folder absent from sys.path."""
    import subprocess

    code = (
        "import sys;"
        "sys.path = [p for p in sys.path if 'agreement_extractor' not in p];"
        "sys.path.insert(0, r'" + str(PROJECT) + "');"
        "import app.main, app.batch_processor, app.excel_generator;"
        "print('ok')"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(PROJECT))
    assert done.returncode == 0, done.stderr[-2000:]
    assert "ok" in done.stdout


def test_the_two_viewers_cannot_fight_over_a_port():
    """Both web apps are meant to run at once, so their defaults must differ.

    Compared by parsing the defaults rather than by searching the text: the agreement
    launcher's docstring mentions 8000 to explain why it does not use it, and a substring
    check would read that sentence as the bug it is warning about.
    """
    from scripts.serve import parse_args

    invoice = (PROJECT / "app" / "config.py").read_text(encoding="utf-8")
    assert 'INVOICE_PORT", 8000' in invoice, "the invoice default moved; update this test"
    assert parse_args([]).port != 8000


def test_the_agreement_viewer_serves_its_own_page():
    """Its HTML lives here, so deleting this folder takes the whole viewer with it."""
    page = HERE / "web" / "static" / "index.html"
    assert page.is_file(), "the viewer's page must live inside agreement_extractor/"
    text = page.read_text(encoding="utf-8")
    assert "/api/runs" in text, "the page should call this folder's own API"
    assert "/api/batches" not in text, "that is the invoice app's API"


def test_removal_instructions_exist_and_name_the_right_things():
    readme = (HERE / "README.md").read_text(encoding="utf-8")
    assert "Removing this pipeline" in readme
    for token in ("agreement_extractor", ".venv313", ".gitignore"):
        assert token in readme, f"removal instructions do not mention {token}"


def test_the_root_readme_points_here_rather_than_documenting_it():
    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    assert "agreement_extractor" in readme
    # The detail lives in this folder's README, so the root one stays short.
    assert "Agreements — a separate pipeline" in readme


@pytest.mark.parametrize("folder", ["app", "tools"])
def test_the_invoice_side_still_imports_on_its_own(folder):
    """A crude but honest check that we did not break the invoice app while separating."""
    for path in (PROJECT / folder).glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# -- the two apps run as two services, for the same reason -----------------------------------

SERVICE = PROJECT / "service"


def test_each_pipeline_has_its_own_launcher():
    """One shared launcher would make removing a pipeline an edit instead of a deletion."""
    assert (SERVICE / "invoice-server.cmd").is_file()
    assert (SERVICE / "agreement-server.cmd").is_file()


def test_the_invoice_launcher_does_not_mention_the_agreement_pipeline():
    """Deleting agreement_extractor/ must not leave the invoice app pointing at a hole."""
    text = (SERVICE / "invoice-server.cmd").read_text(encoding="utf-8")
    for token in ("agreement_extractor", ".venv313", "8100", "scripts.serve"):
        assert token not in text, f"invoice launcher refers to {token}"


def test_the_agreement_launcher_does_not_borrow_the_invoice_environment():
    """It has its own interpreter: paddle has no wheel for the invoice venv's Python 3.14."""
    text = (SERVICE / "agreement-server.cmd").read_text(encoding="utf-8")
    assert ".venv313" in text
    assert ".venv\\Scripts" not in text and "main.py" not in text


@pytest.mark.parametrize("launcher", ["invoice-server.cmd", "agreement-server.cmd"])
def test_a_launcher_starts_a_server_with_no_console_to_interrupt(launcher):
    """Both apps were killed by a Ctrl+C aimed at an unrelated background command.

    Windows delivers a console control event to every process attached to that console, so
    a `python.exe` server started in the same session accepts it and exits -- the task's
    last result was 0xC000013A, STATUS_CONTROL_C_EXIT, and the log ended in `^C`. pythonw
    has no console and cannot be reached this way.
    """
    text = (SERVICE / launcher).read_text(encoding="utf-8")
    assert "pythonw.exe" in text
    assert "python.exe" not in text.replace("pythonw.exe", "")


def test_the_agreement_launcher_disables_onednn_before_python_starts():
    """paddle reads the flag at import, and with oneDNN on every page OCRs to nothing."""
    text = (SERVICE / "agreement-server.cmd").read_text(encoding="utf-8")
    flag = text.index("FLAGS_use_mkldnn")
    assert flag < text.index(".venv313"), "the flag must be set before python is launched"


@pytest.mark.parametrize("script", ["install.ps1", "uninstall.ps1"])
def test_one_pipeline_can_be_registered_or_removed_without_the_other(script):
    text = (SERVICE / script).read_text(encoding="utf-8")
    assert '[ValidateSet("invoice", "agreement", "both")]' in text
    assert "ExtractionInvoiceApp" in text and "ExtractionAgreementApp" in text
