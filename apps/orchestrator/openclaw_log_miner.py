"""
apps/orchestrator/openclaw_log_miner.py
Routing metrics from OpenClaw's request logs (metadata only — log_responses
stays false in openclaw/openclaw.json; the gateway sees private traffic).

Consumes JSON lines from journald (`journalctl -u assistant-openclaw -o json`)
or any JSONL file (--file, used by tests and manual runs). Emits a markdown
metrics section for the nightly improvement report plus anomaly lines
(fallback rate above threshold, error spikes).

Expected fields per line (missing fields are simply not aggregated):
    tag, model, status ("ok"|"error"), fallback (bool), latency_ms
"""
import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

FALLBACK_ALERT_PCT = 25.0
ERROR_ALERT_PCT = 10.0


def read_journal(since: str = "-24h") -> list[dict]:
    out = subprocess.run(
        ["journalctl", "-u", "assistant-openclaw", "-o", "json", "--since", since],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        return []
    return parse_lines(out.stdout.splitlines())


def parse_lines(lines) -> list[dict]:
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # journald wraps the app's own JSON in MESSAGE
        if "MESSAGE" in obj:
            try:
                obj = json.loads(obj["MESSAGE"])
            except (json.JSONDecodeError, TypeError):
                continue
        if isinstance(obj, dict) and ("tag" in obj or "model" in obj):
            records.append(obj)
    return records


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return round(ordered[idx], 1)


def aggregate(records: list[dict]) -> dict:
    per_tag: dict[str, dict] = defaultdict(
        lambda: {"requests": 0, "errors": 0, "fallbacks": 0, "latencies": []})
    for r in records:
        tag = r.get("tag") or "(untagged)"
        s = per_tag[tag]
        s["requests"] += 1
        if r.get("status") == "error":
            s["errors"] += 1
        if r.get("fallback"):
            s["fallbacks"] += 1
        if isinstance(r.get("latency_ms"), (int, float)):
            s["latencies"].append(float(r["latency_ms"]))

    summary, anomalies = {}, []
    for tag, s in per_tag.items():
        n = s["requests"]
        fallback_pct = round(100.0 * s["fallbacks"] / n, 1) if n else 0.0
        error_pct = round(100.0 * s["errors"] / n, 1) if n else 0.0
        summary[tag] = {
            "requests": n, "error_pct": error_pct,
            "fallback_pct": fallback_pct, "p95_latency_ms": _p95(s["latencies"]),
        }
        if fallback_pct > FALLBACK_ALERT_PCT and n >= 5:
            anomalies.append(
                f"tag:{tag} fell back to the cloud {fallback_pct}% of the time "
                f"({s['fallbacks']}/{n}) — is the GPU box off more than expected?")
        if error_pct > ERROR_ALERT_PCT and n >= 5:
            anomalies.append(
                f"tag:{tag} errored {error_pct}% of the time ({s['errors']}/{n})")
    return {"tags": summary, "anomalies": anomalies}


def to_markdown(agg: dict) -> str:
    lines = ["## OpenClaw routing (last 24h)", ""]
    if not agg["tags"]:
        lines.append("_no gateway traffic recorded_")
    for tag, s in sorted(agg["tags"].items()):
        lines.append(
            f"- `{tag}` — {s['requests']} req, {s['error_pct']}% err, "
            f"{s['fallback_pct']}% fallback, p95 {s['p95_latency_ms']}ms")
    if agg["anomalies"]:
        lines += ["", "### Anomalies"]
        lines += [f"- ⚠ {a}" for a in agg["anomalies"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenClaw log metrics")
    parser.add_argument("--file", help="JSONL file instead of journald")
    parser.add_argument("--since", default="-24h")
    args = parser.parse_args()

    if args.file:
        records = parse_lines(Path(args.file).read_text().splitlines())
    else:
        records = read_journal(since=args.since)
    print(to_markdown(aggregate(records)))


if __name__ == "__main__":
    main()
