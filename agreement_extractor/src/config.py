"""Typed access to config.yaml.

Every tunable in the pipeline is read through here rather than hard-coded, so a run can be
reproduced from its config alone. Unknown keys are rejected at load time: a typo in a YAML
key would otherwise silently fall back to a default and change results without saying so.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

import yaml

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    """config.yaml is missing a section, or names something the pipeline does not have."""


@dataclass(frozen=True)
class Paths:
    input: Path = ROOT / "../sample_agreements"
    cache: Path = ROOT / "cache"
    output: Path = ROOT / "output"
    page_images: Path = ROOT / "cache/pages"
    golden: Path = ROOT / "tests/golden"


@dataclass(frozen=True)
class Render:
    dpi: int = 200
    format: str = "png"


@dataclass(frozen=True)
class TextLayer:
    min_chars_per_page: int = 200
    max_replacement_ratio: float = 0.1


@dataclass(frozen=True)
class Ocr:
    lang: str = "en"
    disable_onednn: bool = True
    use_doc_orientation_classify: bool = False
    use_doc_unwarping: bool = False
    use_textline_orientation: bool = False
    use_table_recognition: bool = True
    use_seal_recognition: bool = True
    use_formula_recognition: bool = False
    use_chart_recognition: bool = False
    min_confidence: float = 0.80
    # PP-StructureV3 table structure on scanned pages. It is the only way to recover which
    # column a scanned rate sits in, and it costs roughly three minutes a page on this CPU
    # against about fifty seconds for plain OCR -- so it is aimed, not sprayed. The two
    # numbers below are the aim. See `layout.looks_tabular`.
    table_recognition_on_scans: bool = True
    #: A page whose average line is shorter than this looks like cells, not sentences.
    #: Measured on this MSA: prose pages ran 57-83 characters a line, the Schedule II table
    #: 24 and the plan matrix 8.
    table_page_max_mean_line_chars: int = 45
    #: and it must also talk about money this many times, which is what separates a table of
    #: charges from a signature block or a scope-of-work list.
    table_page_min_money_mentions: int = 3


@dataclass(frozen=True)
class Llm:
    endpoint: str = "http://localhost:11434"
    model: str = "qwen3:8b"
    temperature: float = 0.0
    seed: int = 0
    thinking: bool = False
    num_ctx: int = 32768
    # A hard ceiling on how many tokens one answer may contain, and the single thing that
    # stops a runaway. Without it an answer that outgrows the context window makes
    # llama-server discard the oldest tokens -- the instructions and the document -- after
    # which the model generates indefinitely and never emits a stop token. The streaming
    # timeout cannot catch that, because tokens keep arriving. See llm_input.build for the
    # other half: the body is sized so this much room is always left.
    max_output_tokens: int = 6000
    #: Floor for that cap, so a one-page document is still allowed a complete answer.
    min_output_tokens: int = 1500
    # Tokens set aside for the instruction prompt itself, which is prepended to the body.
    instruction_tokens: int = 2000
    # Seconds of *silence* allowed, not total answer time: the reply is streamed, so this
    # fires only if the server stops sending. It must also cover prompt processing, during
    # which nothing is sent at all. See extract.generate.
    request_timeout_seconds: int = 900
    max_retries: int = 3


@dataclass(frozen=True)
class Grounding:
    fuzzy_threshold: int = 85
    min_match_score: int = 85
    same_row_tolerance_px: int = 20


@dataclass(frozen=True)
class Validation:
    rate_tolerance: float = 0.001
    review_on_confidence_below: float = 0.80


@dataclass(frozen=True)
class Run:
    continue_on_error: bool = True
    ocr_workers: int = 2
    llm_batch_size: int = 1
    resume: bool = True


@dataclass(frozen=True)
class Logging:
    level: str = "INFO"
    file: str = "output/run.log"


@dataclass(frozen=True)
class Config:
    paths: Paths = field(default_factory=Paths)
    render: Render = field(default_factory=Render)
    text_layer: TextLayer = field(default_factory=TextLayer)
    ocr: Ocr = field(default_factory=Ocr)
    llm: Llm = field(default_factory=Llm)
    grounding: Grounding = field(default_factory=Grounding)
    validation: Validation = field(default_factory=Validation)
    run: Run = field(default_factory=Run)
    logging: Logging = field(default_factory=Logging)

    @property
    def source(self) -> Path:
        """The config file this was loaded from, recorded in the Run Log."""
        return getattr(self, "_source", ROOT / "config.yaml")


def _resolve(value: Any) -> Path:
    """Paths in the YAML are relative to agreement_extractor/, not the shell's cwd."""
    path = Path(os.path.expandvars(str(value))).expanduser()
    return path if path.is_absolute() else (ROOT / path).resolve()


def _hints(cls) -> dict[str, Any]:
    """Resolve a dataclass's annotations to real types.

    `from __future__ import annotations` stores them as strings, so `Field.type` is the
    text "Path" rather than the class, and both `is_dataclass` and an identity test against
    Path silently fail. get_type_hints evaluates them against this module's namespace.
    """
    return get_type_hints(cls, globalns=globals())


def _build(cls, data: dict, where: str):
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(data).__name__}")
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {sorted(unknown)}. "
            f"Valid keys are {sorted(known)}."
        )
    hints = _hints(cls)
    kwargs = {}
    for name, value in data.items():
        annotation = hints.get(name)
        if annotation is Path:
            kwargs[name] = _resolve(value)
        elif is_dataclass(annotation):
            kwargs[name] = _build(annotation, value, f"{where}.{name}")
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load(path: Path | str | None = None) -> Config:
    """Read config.yaml. Missing sections keep their defaults; unknown keys are an error."""
    path = Path(path) if path else ROOT / "config.yaml"
    if not path.is_file():
        raise ConfigError(f"No config file at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    known = _hints(Config)
    unknown = set(raw) - set(known)
    if unknown:
        raise ConfigError(f"{path}: unknown section(s) {sorted(unknown)}. "
                          f"Valid sections are {sorted(known)}.")

    sections = {name: _build(known[name], raw[name], name) for name in raw}
    config = Config(**sections)
    object.__setattr__(config, "_source", path.resolve())
    return config
