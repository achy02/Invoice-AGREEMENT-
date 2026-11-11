"""S4 -- two focused Qwen3 calls per document, at temperature 0.

Call A: parties, dates, signatures, services, payment/billing/termination terms.
Call B: every rate and charge line, including the ones buried in Terms & Conditions.

Split deliberately. One call asking for both returns a tidy summary and a short rate list;
the golden set has 58 charge lines across four documents and many are in prose rather than
in the commercial table, so recall matters more than brevity and the two jobs compete for
the model's attention.

Determinism is a hard requirement -- the same PDF must extract identically on a re-run --
so temperature is 0 and a seed is fixed. Qwen3's thinking mode is off by default; whether
it earns its cost is a question for `scripts/eval.py`, not for an opinion.

**The model is never asked for a coordinate.** It returns values and verbatim quotes; S5
finds the boxes. A fluent model will produce a plausible box on request, and a plausible
box is indistinguishable from a correct one until someone looks at the page.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import Config
from .models import Document, Extracted

log = logging.getLogger(__name__)

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


class LlmUnavailable(RuntimeError):
    """The configured endpoint is not reachable, or the model is not pulled."""


def load_prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def prompt_digest() -> str:
    """A hash over every prompt, so the S4 cache invalidates when one is edited."""
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(PROMPTS.glob("*.md")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def generate(prompt: str, config: Config, num_ctx: int | None = None,
             num_predict: int | None = None) -> str:
    """One completion from the local model. Deterministic, and never leaves the machine.

    The reply is streamed, which is not about latency -- nothing here consumes a partial
    answer. It is about what `request_timeout_seconds` means. Unstreamed, the server sends
    nothing until the whole answer is built, so a socket timeout is really a cap on total
    answer length: Call B on a document with forty charge lines generates for eight minutes
    at 12 tokens/s, the client cuts the socket at 300s, Ollama logs a 500, and every retry
    starts generation again from scratch and dies at exactly the same point. Streamed, a
    token arrives every eighty milliseconds, so the timeout becomes what it should be -- a
    detector for a server that has gone quiet -- and a long answer is simply a long answer.

    One silence survives streaming: **prompt processing**. Nothing is sent while the server
    ingests the prompt, and a whole offer letter at num_ctx 32768 on a partly-CPU model can
    spend minutes there before the first token. So the timeout has to cover prefill too,
    which is why its default is generous rather than tight. A server that is actually down
    refuses the connection instead and is caught in milliseconds.

    Streaming does not affect sampling, so determinism is unchanged.
    """
    payload = {
        "model": config.llm.model,
        "prompt": prompt,
        "stream": True,
        "format": "json",
        "think": config.llm.thinking,
        "options": {
            "temperature": config.llm.temperature,
            "seed": config.llm.seed,
            # Sized to this prompt rather than to the largest document the pipeline might
            # ever see; see `llm_input.plan_window`. A window three times bigger than the
            # document costs nearly three times the time per token on an 8 GB card.
            "num_ctx": num_ctx or config.llm.num_ctx,
            # The stop that does not depend on the model choosing to stop. See the note in
            # config.yaml: without it, an answer that outgrows the context window costs the
            # server the instructions it was following, and generation never ends.
            "num_predict": num_predict or config.llm.max_output_tokens,
        },
    }
    request = urllib.request.Request(
        config.llm.endpoint.rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    last: Exception | None = None
    for attempt in range(1, config.llm.max_retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=config.llm.request_timeout_seconds) as r:
                return _read_stream(r)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:200]
            if exc.code == 404:
                raise LlmUnavailable(
                    f"{config.llm.model} is not pulled. Run: ollama pull {config.llm.model}"
                ) from exc
            last = RuntimeError(f"HTTP {exc.code}: {body}")
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        if attempt < config.llm.max_retries:
            time.sleep(min(2 ** attempt, 8))
    raise LlmUnavailable(f"{config.llm.endpoint} unreachable after "
                         f"{config.llm.max_retries} attempts: {last}")


def _read_stream(response) -> str:
    """Join an Ollama streaming reply into the one string the caller expects.

    Each line is a JSON object carrying the next fragment under "response"; the last one
    sets "done". A mid-stream "error" is raised rather than silently returning a truncated
    answer, because a half-finished charge list parses perfectly well and would quietly
    drop rows.

    `done_reason` is checked for the same reason. "length" means the answer hit
    `num_predict` rather than finishing, so the JSON stops mid-object and the charge lines
    after the cut are simply gone. That is a far better failure than the unbounded one it
    replaces, but it is still a failure, and it has to say so.
    """
    parts: list[str] = []
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue            # a keep-alive or a blank frame, not an answer fragment
        if chunk.get("error"):
            raise RuntimeError(str(chunk["error"])[:200])
        parts.append(chunk.get("response") or "")
        if chunk.get("done"):
            if chunk.get("done_reason") == "length":
                log.warning(
                    "The model hit the output ceiling and the answer is cut off mid-JSON; "
                    "charge lines after the cut are missing. Raise llm.max_output_tokens "
                    "(and lower the document budget to match) if this recurs.")
            break
    return "".join(parts)


def parse_json(text: str) -> dict:
    """Parse the model's reply, repairing the usual malformations before giving up.

    `format: json` makes Ollama constrain decoding, but a model can still wrap the object
    in prose or stop mid-structure, and a whole document's extraction is too expensive to
    throw away over a trailing comma.
    """
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()

    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start:end + 1]
        for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    log.warning("Could not parse model output (%d chars)", len(text))
    return {}


def _entry(raw: dict, field: str, sheet: str) -> Extracted:
    item = Extracted(
        field=field,
        value=raw.get("value"),
        normalized_value=raw.get("normalized_value"),
        evidence_text=raw.get("evidence_text") or "",
        evidence_ids=[str(i) for i in (raw.get("evidence_ids") or [])],
        page=raw.get("page") if isinstance(raw.get("page"), int) else None,
        section_heading=raw.get("section_heading") or "",
        key_text=raw.get("key_text") or "",
    )
    item.sheet = sheet  # type: ignore[attr-defined]
    return item


def _ask(prompt_name: str, body: str, config: Config) -> dict:
    """One call, in a context window sized to the prompt it is actually sending."""
    from .llm_input import estimate_tokens, plan_window

    prompt = load_prompt(prompt_name) + "\n\n## Document\n\n" + body
    num_ctx, num_predict = plan_window(
        estimate_tokens(prompt), config.llm.num_ctx, config.llm.max_output_tokens,
        config.llm.min_output_tokens)
    log.info("%s: ~%d prompt tokens -> num_ctx %d, answer capped at %d",
             prompt_name, estimate_tokens(prompt), num_ctx, num_predict)
    return parse_json(generate(prompt, config, num_ctx, num_predict))


def call_a(body: str, config: Config) -> list[Extracted]:
    """Parties, dates, signatures, services and terms."""
    reply = _ask("call_a_parties_terms.md", body, config)
    out = []
    for raw in reply.get("fields", []) or []:
        field = str(raw.get("field") or "").strip()
        if field:
            out.append(_entry(raw, field, "Summary"))
    return out


def call_b(body: str, config: Config) -> list[Extracted]:
    """Every rate and charge line."""
    reply = _ask("call_b_commercials.md", body, config)
    out = []
    for index, raw in enumerate(reply.get("charges", []) or [], start=1):
        label = " - ".join(p for p in (raw.get("product"), raw.get("sub_product")) if p)
        item = _entry(raw, f"{label or 'charge'} - Rate", "Commercials")
        item.charge = {                                   # type: ignore[attr-defined]
            key: raw.get(key) for key in
            ("charge_category", "product", "sub_product", "frequency_basis", "rate_basis",
             "rate_inr", "standard_rate_inr", "rate_type", "rate_as_written",
             "rate_as_written_standard", "free_limit", "condition_notes")
        }
        item.value = raw.get("rate_as_written") or raw.get("rate_inr")
        item.normalized_value = raw.get("rate_inr")
        item.row = index                                  # type: ignore[attr-defined]
        out.append(item)
    return out


def extract_document(document: Document, body: str, config: Config) -> list[Extracted]:
    """Both calls for one document. A failure in one does not lose the other."""
    items: list[Extracted] = []
    for name, call in (("A", call_a), ("B", call_b)):
        started = time.perf_counter()
        try:
            got = call(body, config)
        except LlmUnavailable:
            raise
        except Exception as exc:
            log.warning("Call %s failed for %s: %s", name, document.file_name, exc)
            continue
        log.info("Call %s: %d field(s) from %s in %.1fs",
                 name, len(got), document.file_name, time.perf_counter() - started)
        items.extend(got)
    return items


def available(config: Config) -> tuple[bool, str]:
    url = config.llm.endpoint.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            models = [m.get("name", "") for m in
                      json.loads(response.read().decode("utf-8")).get("models", [])]
    except Exception as exc:
        return False, f"{config.llm.endpoint} unreachable ({exc})"
    wanted = config.llm.model
    if any(name == wanted or name.startswith(wanted.split(":")[0]) for name in models):
        return True, f"{wanted} available"
    return False, f"{wanted} not pulled; have: {', '.join(models) or 'none'}"
