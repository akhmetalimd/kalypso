"""Оценка точности извлечения на синтетическом валидационном наборе.

    python -m kalypso.ai.evaluate --extractor rules --n 200
    python -m kalypso.ai.evaluate --extractor claude --n 200 --estimate        # только смета, без вызовов API
    python -m kalypso.ai.evaluate --extractor claude --n 200 --confirm-spend   # живой прогон через Batch API

Живой прогон Claude тратит деньги: без --confirm-spend он не запускается.
Метрики по полю: accuracy = верно / есть в эталоне; precision = верно / предложено;
grounded = доля предложений, чья цитата найдена в тексте.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict

from kalypso.ai.base import Extraction, ExtractionFailed, Extractor
from kalypso.ai.fields import allowed_values, targets_for
from kalypso.ai.pipeline import _ground
from kalypso.ai.synthetic_docs import SyntheticDoc, generate
from kalypso.deid_text import deidentify

# Цены Claude Opus 5.5, $ за 1 млн токенов; Batch API — 50 %.
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ = 4.00, 20.00, 0.20
CHARS_PER_TOKEN = 2.8          # грубо для русского текста
OUTPUT_TOKENS_PER_DOC = 1500   # ответ + рассуждения (оценка сверху)


def prepare(docs: list[SyntheticDoc]) -> list[dict]:
    """Деидентификация как в продуктиве; документы с остатками идентификаторов не идут в AI."""
    out = []
    for i, d in enumerate(docs):
        r = deidentify(d.raw_text)
        out.append({"id": f"doc-{i}", "doc": d, "text": r.text, "clean": r.clean, "report": r.report})
    return out


def score(items: list[dict], extractions: dict[str, Extraction | ExtractionFailed]) -> dict:
    stats = defaultdict(lambda: {"truth": 0, "proposed": 0, "correct": 0, "wrong": 0, "grounded": 0})
    failures = defaultdict(int)
    for it in items:
        res = extractions.get(it["id"])
        if isinstance(res, ExtractionFailed) or res is None:
            failures[getattr(res, "status", "missing")] += 1
            findings = {}
        else:
            findings = {}
            for f in res.findings:
                if f.target in targets_for(it["doc"].doc_type) and f.value in allowed_values(f.target):
                    findings.setdefault(f.target, f)
        for t in targets_for(it["doc"].doc_type):
            s, truth, f = stats[t], it["doc"].truth.get(t), findings.get(t)
            s["truth"] += truth is not None
            if f is not None:
                s["proposed"] += 1
                s["grounded"] += _ground(it["text"], f.evidence)[0] is not None
                if f.value == truth:
                    s["correct"] += 1
                else:
                    s["wrong"] += 1
    table = {}
    for t, s in sorted(stats.items()):
        table[t] = {**s,
                    "accuracy": round(s["correct"] / s["truth"], 3) if s["truth"] else None,
                    "precision": round(s["correct"] / s["proposed"], 3) if s["proposed"] else None,
                    "grounded_rate": round(s["grounded"] / s["proposed"], 3) if s["proposed"] else None}
    return {"fields": table, "failures": dict(failures),
            "deid_clean": sum(it["clean"] for it in items), "docs": len(items)}


def run_local(extractor: Extractor, items: list[dict]) -> dict:
    out = {}
    for it in items:
        if not it["clean"]:
            out[it["id"]] = ExtractionFailed("invalid", "деидентификация не пройдена")
            continue
        try:
            out[it["id"]] = extractor.extract(it["doc"].doc_type, it["text"])
        except ExtractionFailed as e:
            out[it["id"]] = e
    return out


def estimate(items: list[dict]) -> dict:
    from kalypso.ai.claude import SYSTEM_PROMPT
    sys_tokens = len(SYSTEM_PROMPT) / CHARS_PER_TOKEN
    doc_tokens = sum(len(it["text"]) for it in items) / CHARS_PER_TOKEN + 60 * len(items)
    n = len(items)
    # Без кэша (худший случай) и с кэшем системной части; Batch API — половина цены.
    no_cache = (sys_tokens * n + doc_tokens) * PRICE_IN / 1e6 + OUTPUT_TOKENS_PER_DOC * n * PRICE_OUT / 1e6
    return {"docs": n, "input_tokens": round(sys_tokens * n + doc_tokens),
            "output_tokens_upper": OUTPUT_TOKENS_PER_DOC * n,
            "usd_batch_upper": round(no_cache * 0.5, 2), "usd_interactive_upper": round(no_cache, 2)}


def run_claude_batch(items: list[dict], poll: float = 30.0) -> dict:
    import anthropic

    from kalypso.ai.claude import ClaudeExtractor
    ext = ClaudeExtractor()
    client = anthropic.Anthropic()
    requests = [{"custom_id": it["id"], "params": ext.request_params(it["doc"].doc_type, it["text"])}
                for it in items if it["clean"]]
    batch = client.messages.batches.create(requests=requests)
    print(f"batch {batch.id}: {len(requests)} запросов", file=sys.stderr)
    while client.messages.batches.retrieve(batch.id).processing_status != "ended":
        time.sleep(poll)
    out: dict = {}
    for r in client.messages.batches.results(batch.id):
        if r.result.type == "succeeded":
            try:
                out[r.custom_id] = ext.parse_response(r.result.message)
            except ExtractionFailed as e:
                out[r.custom_id] = e
        else:
            out[r.custom_id] = ExtractionFailed("error", r.result.type)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kalypso.ai.evaluate")
    ap.add_argument("--extractor", choices=["rules", "claude"], default="rules")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--estimate", action="store_true", help="только смета стоимости (claude)")
    ap.add_argument("--confirm-spend", action="store_true", help="подтверждаю живой платный прогон (claude)")
    ap.add_argument("--out", help="сохранить отчёт JSON")
    args = ap.parse_args(argv)

    items = prepare(generate(args.n, args.seed))
    if args.extractor == "rules":
        from kalypso.ai.rules import RuleExtractor
        report = score(items, run_local(RuleExtractor(), items))
    elif args.estimate or not args.confirm_spend:
        print(json.dumps({"estimate": estimate(items),
                          "note": "живой прогон: добавьте --confirm-spend (тратит деньги)"},
                         ensure_ascii=False, indent=2))
        return 0
    else:
        report = score(items, run_claude_batch(items))
    report["extractor"] = args.extractor
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
