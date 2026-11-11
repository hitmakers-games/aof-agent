"""Keep credentials and individual player records out of diagnostic reports."""
import collections
import re


def redact(text):
    text = re.sub(r"(?i)\b(?:bearer\s+)[^\s,;\"']+", "Bearer [redacted]", text)
    text = re.sub(r"\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"\b\d{7,12}:[A-Za-z0-9_-]{20,}\b", "[redacted]", text)
    text = re.sub(r"(?i)(?:password|secret|token|api[_-]?key)\s*[=:]\s*[^\s,;]+", "[redacted credential]", text)
    text = re.sub(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}\b",
                  "[redacted email]", text)
    text = re.sub(r"\b(?:[0-9a-fA-F]{8}-){1}[0-9a-fA-F-]{27,}\b", "[redacted id]", text)
    return text


def log_summary(text):
    """Return error categories, never raw game log lines or request bodies."""
    lines = text.splitlines()
    errors = collections.Counter()
    for line in lines:
        classes = re.findall(r"\b(?:[a-zA-Z_$][\w$]*\.)*([A-Z][\w$]*(?:Exception|Error))\b", line)
        for name in set(classes):
            errors[name] += 1
        if not classes:
            for needle, category in (("outofmemory", "Out of memory"), ("timed out", "Timeout"),
                                     ("connection refused", "Connection refused"),
                                     ("deadlock", "Deadlock"), ("permission denied", "Permission denied")):
                if needle in line.lower():
                    errors[category] += 1
                    break
    levels = {level: sum(bool(re.search(rf"\b{level}\b", line)) for line in lines)
              for level in ("ERROR", "WARN")}
    report = [f"Examined {len(lines)} recent lines (bounded sample).",
              f"Levels: ERROR={levels['ERROR']}, WARN={levels['WARN']}."]
    report.extend(f"{name}: {count}" for name, count in errors.most_common(12))
    if not errors:
        report.append("No recognized exception categories in this sample; this does not prove the application is healthy.")
    report.append("Raw log messages, request bodies and player records are intentionally omitted.")
    return "\n".join(report)
